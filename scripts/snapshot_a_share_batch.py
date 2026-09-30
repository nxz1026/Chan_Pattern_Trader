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

或 ``--dry-run`` 只打印代码清单。

## 跟 factor_backfill.py 的区别
- factor_backfill 是**回填历史因子**到 ``public.a_share_factor_adj``；
  本脚本是**每日评估一买结构**写到 ``public.cpt_signal_event``。
- factor_backfill 走腾讯/HTTP；本脚本走本地 A 股行情 + 缠论后端，无外网依赖。
- factor_backfill 用 ``scripts/factor_backfill.py``；本脚本用
  ``scripts/snapshot_a_share_batch.py``（同样无 ``__init__`` 当脚本跑）。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

from cpt.adapters.a_share_local import AShareLocalClient
from cpt.adapters.a_share_pool import WatchlistStore, fetch_hot_pool
from cpt.application.a_share_snapshot import (
    build_ashare_snapshot,
    factor_ensurer_from_env,
)
from cpt.web.a_share_routes import DEFAULT_WATCHLIST_PATH

_LOG = logging.getLogger("cpt.snapshot_a_share_batch")


def collect_codes(*, hot_limit: int) -> list[str]:
    """合并 hot_pool Top N + 服务端 watchlist（A 股），去重保序。"""
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--limit", type=int, default=50, help="hot_pool 取前 N（默认 50）")
    parser.add_argument("--dry-run", action="store_true", help="只打印代码清单不评估")
    parser.add_argument("--quiet", action="store_true", help="抑制 INFO 日志")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    codes = collect_codes(hot_limit=args.limit)
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
    t0 = time.time()
    try:
        for code in codes:
            status, reason = _run_one(code, client=client, ensure_factors=ensure_factors)
            counts[status] += 1
            if status == "skip":
                _LOG.info("snapshot %s degraded: %s", code, reason)
            elif status == "err":
                _LOG.warning("snapshot %s 失败: %s", code, reason)
    finally:
        client.close()
    dur = time.time() - t0
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
