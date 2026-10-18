#!/usr/bin/env python3
"""关注列表收盘汇总（R4/C2 ④）：每天收盘后固定播报一次。

与 `cron_daily_report.py` 的分工：

- 那个只在**有故障**时推（静默日 print 一行然后 ``return 0``，不发飞书）；
- 这个**每天都推**，因为它的价值恰恰是「没事也让人看一眼」—— 所以它不看
  有没有问题，也不走 `signal_notify.maybe_notify` 的 24h 节流。

数据全部来自 CPT 自己的表（`cpt.storage.track_store.latest_active_snapshots`
读 `cpt_track` + `cpt_track_snapshot`），渲染交给
`cpt.application.signal_notify.build_watchlist_summary` —— 快照里已经带了
参考买卖点与「说人话」，这里只做拼装，不重算结构。

退出码：

- ``0``：播报完成（含「关注列表为空，不发」）
- ``3``：渲染出内容但飞书没送达（含 ``CPT_FEISHU_WEBHOOK`` 未配置）
- ``4``：读库失败 —— 拿不到关注列表，**不能让「读失败」看起来像「没有内容」**
- ``5``：**汇总已送达，但快照刷新全量失败**（``refreshed == 0``）—— 正文里已插入
  警示行，同时用非零码让 cron 日志能发现（R67/审计 P2-15）

用法::

    PYTHONPATH=. .venv/bin/python scripts/watchlist_daily_summary.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Final

import psycopg
from cpt.adapters._dbconfig import connection_kwargs
from cpt.adapters.feishu import notify, webhook_configured
from cpt.application import signal_notify
from cpt.storage import track_store

_LOG = logging.getLogger("watchlist_daily_summary")

#: 播报完成（也包含「关注列表为空，不发」）。
EXIT_OK: Final[int] = 0
#: 有内容但飞书没送达（含未配置 webhook）。
EXIT_UNDELIVERED: Final[int] = 3
#: 读库失败 —— 与 ``track_store`` 的纪律一致：不用空列表冒充「没人追踪」。
EXIT_STORE: Final[int] = 4
#: 汇总送达了，但快照刷新**全量失败** ⇒ 推的可能是昨天的价位（R67/审计 P2-15）。
#:
#: 为什么不直接返 ``EXIT_UNDELIVERED``：那会让 ``deploy/cron`` 的封装打印
#: 「当天没有汇总推送」，而实际上推了 —— 日志不许撒谎。这里单独一个码，
#: 让封装能说清「推的是**过期**价位」。
EXIT_STALE: Final[int] = 5

#: loopback 刷新端点（与 ``deploy/env/cpt-dashboard.env`` 的 ``CPT_PORT`` 一致）。
DEFAULT_BASE_URL: Final[str] = "http://127.0.0.1:8010"
#: 单只刷新超时。advice 里的「说人话」只读缓存、不现造 LLM，所以够用。
REFRESH_TIMEOUT: Final[float] = 30.0
#: 单次最多刷新几对 —— 关注列表异常膨胀时别把 cron 拖过 ``timeout``。
MAX_REFRESH: Final[int] = 200


def read_rows() -> list[dict[str, object]]:
    """读关注列表的最新快照；失败抛（由 :func:`main` 折成退出码）。"""
    with psycopg.connect(**connection_kwargs()) as conn:
        return track_store.latest_active_snapshots(conn)


def read_pairs() -> list[tuple[str, str]]:
    """读全部活跃追踪的 ``(user_id, code)`` 配对；失败抛。"""
    with psycopg.connect(**connection_kwargs()) as conn:
        return track_store.active_pairs(conn)


def refresh_snapshots(pairs: list[tuple[str, str]], *, base_url: str = DEFAULT_BASE_URL) -> int:
    """逐对打 loopback advice 端点，把 ``cpt_track_snapshot`` 刷到当天。

    ``cpt_track_snapshot`` 只在「有人打开追踪页 / 主动拉建议」时才写。收盘汇总
    固定跑在北京 18:10，那时可能一整天都没人看过页面 —— 不刷的话汇总拿到的是
    **昨天甚至更早**的价位，看着像真的却没有参考价值（比不推更糟）。

    逐条 best-effort：某一只失败只 warning，不影响其它只、也不改退出码 ——
    宁可少一只，也不能因为一只票把整条汇总丢掉。

    :returns: 刷新成功的配对数（用于日志，不参与退出码）。
    """
    import urllib.error  # noqa: PLC0415 — 仅此函数用得到
    import urllib.request  # noqa: PLC0415

    ok = 0
    for user_id, code in pairs[:MAX_REFRESH]:
        url = f"{base_url}/api/dashboard/track/{code}/advice"
        request = urllib.request.Request(  # noqa: S310 — 地址由本文件写死 + 6 位代码
            url, headers={"X-CPT-User": user_id}
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 — 同上，只打 127.0.0.1
                request, timeout=REFRESH_TIMEOUT
            ) as response:
                response.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _LOG.warning("刷新快照失败 user=%s code=%s: %s", user_id, code, exc)
            continue
        ok += 1
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="关注列表收盘汇总（飞书，每个交易日一次）")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印不发送，也不校验 webhook",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        pairs = read_pairs()
    except Exception as exc:  # noqa: BLE001 — 读失败要变成非零退出码，不能静默
        _LOG.error("读取关注列表失败：%s: %s", type(exc).__name__, exc)
        return EXIT_STORE

    # 先刷新再读快照。`--dry-run` 不写库，所以跳过刷新（否则「只打印」也会
    # 往 cpt_track_snapshot 里加行）。
    refreshed: int | None = None
    if pairs and not args.dry_run:
        refreshed = refresh_snapshots(pairs)
        _LOG.info("快照刷新完成：%d/%d 对", refreshed, len(pairs))
        if refreshed == 0:
            # R67（审计 P2-15）：**全量**刷新失败（仪表盘挂着）= 下面的价位可能
            # 是昨天甚至更早，而正文里没有任何日期 ⇒ 收件人看不出来。
            # 本文件 `refresh_snapshots` 的 docstring 自己写着「看着像真的却没有
            # 参考价值（比不推更糟）」，那就不能照发不误。
            _LOG.error("快照刷新全量失败（0/%d 对）—— 汇总里的价位可能已过期", len(pairs))

    try:
        rows = read_rows()
    except Exception as exc:  # noqa: BLE001 — 读失败要变成非零退出码，不能静默
        _LOG.error("读取关注列表快照失败：%s: %s", type(exc).__name__, exc)
        return EXIT_STORE

    summary = signal_notify.build_watchlist_summary(rows)
    if summary is None:
        print(f"关注列表没有可用快照（读到 {len(rows)} 行）—— 不发飞书。")
        return EXIT_OK

    title, lines = summary
    if refreshed == 0:
        # 警示行放在**最前面**：飞书正文里它紧跟标题，收件人第一眼就看到。
        lines = [
            f"⚠️ 快照刷新全部失败（0/{len(pairs)} 对）—— 下面的价位可能不是今天的",
            *lines,
        ]
    for line in lines:
        print(f"· {line}")

    if args.dry_run:
        print(f"[dry-run] 不发送：{title}")
        return EXIT_OK

    if not webhook_configured():
        print("CPT_FEISHU_WEBHOOK 未设置 —— 汇总无法送达。", file=sys.stderr)
        return EXIT_UNDELIVERED

    if not notify(title, lines):
        print("飞书未送达。", file=sys.stderr)
        return EXIT_UNDELIVERED

    print(f"已发送：{title}")
    if refreshed == 0:
        print("汇总已送达，但快照刷新全量失败 —— 推的是可能过期的价位。", file=sys.stderr)
        return EXIT_STALE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
