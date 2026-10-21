"""A 股批量快照入口（每日触发一次，写 cpt_signal_event）。

## 为什么需要
oracle 上 cpt-dashboard 服务只跑加密行情（``--mode realtime --symbol BTCUSDT``），
不调 ``record_signal_event``，导致 ``public.cpt_signal_event`` 表存在但为空，
``/api/dashboard/signal-stats`` 长期 ``total:0``。

本脚本**只在 oracle 上由 systemd timer 触发**（不在主服务启动路径），对一组
A 股代码循环调 :func:`cpt.application.a_share_snapshot.build_ashare_snapshot`，
里面会读 ``public.cpt_signal_event`` 拿到上一次状态、推进状态机、status 变化时
INSERT 一条事件。失败一只不影响其他。

## 代码清单
``hot_pool`` Top N（``hot_rank`` ∪ ``ladder_day``，默认 N=50）∪ 服务端 watchlist
（``cpt.web.a_share_routes.DEFAULT_WATCHLIST_PATH``），去重保序。

## 用法
直接当脚本跑（CWD = 仓库根）::

    python scripts/snapshot_a_share_batch.py --limit 50
    python scripts/snapshot_a_share_batch.py --only watchlist   # R80：只跑关注列表

或 ``--dry-run`` 只打印代码清单。

## R80：``--only watchlist`` —— 用户加了新关注，几十分钟内就能看到

每天一次的定时批次（``cpt-dashboard-ashare.timer``）覆盖「热池前 50 + 关注」。
问题在**关注列表的票新加进来要等到第二天**才有行 —— 用户 2026-10-21 提的就是这个。

``--only watchlist`` 只取服务端关注列表（不含热池），由
``deploy/cron/watchlist-snapshot.sh`` 每 15 分钟跑一次。为什么与热池分开：
热池 97 只 × 每 15 分钟 = 一天近万次缠论计算，纯浪费；关注列表通常个位数，
跑一次一两秒。

跑完把**这一轮的结果**写到 ``~/.cache/cpt/watchlist_batch_status.json``
（见 :func:`_write_status`）。⚠️ 这个文件只是**调度侧的自述**，界面上「数据跑没跑」
的事实判据仍是 ``cpt_signal_event`` 里有没有行（见
:func:`cpt.application.signal_overview.watchlist_status`）—— 文件丢了界面也不会骗人。

## 跟 factor_backfill.py 的区别
- factor_backfill 是**回填历史因子**到 ``public.a_share_factor_adj``；
  本脚本是**每日评估一买结构**写到 ``public.cpt_signal_event``。
- factor_backfill 走腾讯/HTTP；本脚本走本地 A 股行情 + 缠论后端，无外网依赖。
- factor_backfill 用 ``scripts/factor_backfill.py``；本脚本用
  ``scripts/snapshot_a_share_batch.py``（同样无 ``__init__`` 当脚本跑）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cpt.adapters.a_share_local import AShareLocalClient
from cpt.adapters.a_share_pool import WatchlistStore, fetch_hot_pool
from cpt.application.a_share_snapshot import (
    build_ashare_snapshot,
    factor_ensurer_from_env,
)
from cpt.web.a_share_routes import DEFAULT_WATCHLIST_PATH

_LOG = logging.getLogger("cpt.snapshot_a_share_batch")


def collect_codes(*, hot_limit: int, only: str | None = None) -> list[str]:
    """合并 hot_pool Top N + 服务端 watchlist（A 股），去重保序。

    :param only: ``"watchlist"`` 时**不取热池**，只返回关注列表（R80）。
        ``None``（默认）保持原行为，既有调用方零改动。
    """
    if only == "watchlist":
        hot: list[str] = []
    else:
        client = AShareLocalClient()
        try:
            hot = [e.code for e in fetch_hot_pool(client._get_conn(), limit=hot_limit)]
        finally:
            client.close()
    watch = [e.code for e in WatchlistStore(DEFAULT_WATCHLIST_PATH).list() if e.market == "A"]
    seen: set[str] = set()
    out: list[str] = []
    for c in hot + watch:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _run_one(
    code: str, *, client: AShareLocalClient, ensure_factors: Any
) -> tuple[str, str | None]:
    """单只快照。返回 (status, reason)：status ∈ {ok, skip, err}。"""
    try:
        snap = build_ashare_snapshot(code, client=client, ensure_factors=ensure_factors)
    except Exception as exc:  # noqa: BLE001 — 单只失败隔离
        _LOG.warning("snapshot %s 异常: %s: %s", code, type(exc).__name__, exc)
        return ("err", f"{type(exc).__name__}: {exc}")
    dq = (snap or {}).get("data_quality") or {}
    reason = dq.get("reason") or ""
    if reason and reason not in {"no_error", ""}:
        return ("skip", str(reason))
    return ("ok", None)


#: R80：关注批次的自述状态文件。只给运维/排障看，**界面的判据是事件表本身**
#: （见 :func:`cpt.application.signal_overview.watchlist_status`）。
STATUS_PATH = Path(
    os.environ.get("CPT_WATCHLIST_STATUS")
    or Path.home() / ".cache" / "cpt" / "watchlist_batch_status.json"
)


def _write_status(
    *, codes: list[str], counts: dict[str, int], failed: list[str], duration_s: float
) -> None:
    """把这轮结果原子地写到 :data:`STATUS_PATH`。**失败只记日志，绝不让作业失败**。

    用临时文件 + ``os.replace`` 而非直接覆写：cron 每 15 分钟跑一次，界面/排障
    随时可能在读 —— 覆写到一半被读到会得到一个残缺 JSON。
    """
    payload = {
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "updated_at_ms": int(time.time() * 1000),
        "codes": codes,
        "total": len(codes),
        "counts": counts,
        # 失败代码单独列：``counts["err"]`` 只有数量，排障时要名字。
        "failed": failed,
        "duration_s": round(duration_s, 1),
    }
    try:
        STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATUS_PATH)
    except OSError as exc:
        _LOG.warning("写关注批次状态失败（不影响作业）：%s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=50, help="hot_pool 取前 N（默认 50）")
    parser.add_argument(
        "--only",
        choices=("watchlist",),
        default=None,
        help="只跑关注列表、不取热池（R80，由 deploy/cron/watchlist-snapshot.sh 调用）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印代码清单不评估")
    parser.add_argument("--quiet", action="store_true", help="抑制 INFO 日志")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    codes = collect_codes(hot_limit=args.limit, only=args.only)
    _LOG.info(
        "代码清单 %d 只: %s", len(codes), ",".join(codes[:20]) + ("..." if len(codes) > 20 else "")
    )
    if args.dry_run:
        for c in codes:
            print(c)
        return 0

    ensure_factors = factor_ensurer_from_env(default=True)
    client = AShareLocalClient()
    counts = {"ok": 0, "skip": 0, "err": 0}
    failed: list[str] = []
    t0 = time.time()
    try:
        for code in codes:
            status, reason = _run_one(code, client=client, ensure_factors=ensure_factors)
            counts[status] += 1
            if status == "skip":
                _LOG.info("snapshot %s degraded: %s", code, reason)
            elif status == "err":
                failed.append(code)
                _LOG.warning("snapshot %s 失败: %s", code, reason)
    finally:
        client.close()
    dur = time.time() - t0
    if args.only == "watchlist":
        _write_status(codes=codes, counts=counts, failed=failed, duration_s=dur)
    _LOG.info(
        "完成 — total=%d ok=%d skip=%d err=%d dur=%.1fs",
        len(codes),
        counts["ok"],
        counts["skip"],
        counts["err"],
        dur,
    )
    return 0 if counts["err"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
