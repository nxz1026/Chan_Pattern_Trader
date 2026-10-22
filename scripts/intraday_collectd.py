#!/usr/bin/env python
"""盘中采集守护（R89）：**只采集 + 报健康，不做任何触发/判定。**

## 这个脚本为什么存在

ADR 0003 §E2 定的分工是「**原始采集归 CPT，触发判据归 emotion-core**」。
R86 已经把采集链路写好了（``cpt/adapters/intraday_probe.py``），但那时刻意
**不接线** —— ``tests/test_intraday_probe.py`` 里有一条测试钉住了它。
代价是 emotion-core 的 §D10 观察期条件②（「盘中采集任务无失败」）
**无对象可观测**：没人在跑，也就没有「跑得通不通」这回事。

本脚本就是那个对象。

## 边界（改之前先看这一节）

| 做 | 不做 |
|---|---|
| 按轮询间隔采新浪涨幅榜 + 五档 | 判「该不该触发买点」 |
| 把每一轮的结果写进日志（JSON 行） | **建表**（``intraday_quote`` 归 emotion-core） |
| 只读 DB 看交易日历 / 今日是不是该跑 | 写任何数据库 |
| 连续失败 N 轮才报警 | 单轮失败就报警（新浪抖一下很正常） |
| 收盘后**立即退出** | 常驻空转等第二天 |

**为什么不做判定**：同一个「封板」如果 CPT 与 emotion-core 各判一次，两个口径
迟早分叉 —— 这正是 R88 里 ``board_of`` 顺序之争的来源。CPT 只提供**原始材料**
（价格/量/五档），判定归 emotion-core 一家。

## 「无失败」怎么算（emotion-core §D10 条件② 的可观测口径）

- 一轮 = 一次 ``IntradayCollector.scan()``；
- 单轮结果只有三种：``ok``（拿到候选与报价）/ ``empty``（榜单空，非交易时段
  或真的没票接近涨停）/ ``error``（抛异常）；
- **``error`` 连续 ``--max-failures`` 轮**才算「采集挂了」并报警；``empty`` 不算失败
  （午休、竞价前两分钟都会 empty）；
- 进程退出时把本次运行的统计写成一行总结，字段固定，方便 emotion-core 侧做
  外部核对（它不依赖这份日志，日志只是给人看的）。

## 用法

    # 手动跑一轮，看今天封板什么样子
    .venv/bin/python scripts/intraday_collectd.py --once

    # 手动跑 3 轮（每 30 秒），中途 Ctrl-C
    .venv/bin/python scripts/intraday_collectd.py --rounds 3

    # 生产：由 systemd timer 在交易时段每 2 分钟拉起一次，跑一轮就退
    systemctl --user status cpt-intraday-collect.timer

**生产不用常驻进程**：定时拉起、跑一轮、退出，比常驻更好——常驻要自己处理
跨日、进程假活、日志轮转；跑一轮就退，失败由 systemd 与飞书看见。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from cpt.adapters.intraday_probe import IntradayCollector, IntradayError
from cpt.application.intraday_analysis import board_health, seal_board

#: 市场时区。交易日与盘中时段一律按北京时间算，不按机器时区。
_MARKET_TZ: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")

#: 盘中时段（含收盘集合竞价到 15:00）。9:15 前新浪报价还没开始刷新，
#: 采到的全是昨收 —— 那种数据写进日志只会让人误判。
SESSION_START: Final[dtime] = dtime(9, 30)
SESSION_END: Final[dtime] = dtime(15, 0)

#: 连续这么多轮 error 才报警。单轮失败（新浪限流、偶发 502）不该叫醒人。
MAX_FAILURES_DEFAULT: Final[int] = 3

#: 日志路径。轮询结果写成 JSON 行，一行一轮。
LOG_PATH: Final[Path] = Path(
    os.environ.get("CPT_INTRADAY_LOG") or (Path.home() / "logs" / "intraday-collect.log")
)


def now_market() -> datetime:
    """北京时间当前时刻。"""
    return datetime.now(_MARKET_TZ)


def in_session(now: datetime) -> bool:
    """现在是不是该采（盘中时段）。

    **不看交易日历**：这一步是纯时间判断，交易日由调用方（``trading_day``）
    叠加。分开是因为前者无 IO、可测，后者要读库。
    """
    return SESSION_START <= now.timetz().replace(tzinfo=None) <= SESSION_END


def trading_day(now: datetime) -> str | None:
    """今天是不是交易日。是则返回 ``YYYY-MM-DD``，否则 ``None``。

    读 ``public.daily_bar`` 最近一个有数据的交易日 —— **不猜节假日**。
    停牌一天造成的日历错位在这里是安全的：只要库里当天有行就算交易日，
    没有就跳过（宁可少采一轮，不要在休市日采出一堆昨收数据）。

    **读库失败时选择「跑」而不是「跳过」**：漏采一天会让 emotion-core 的
    观察期计数中断，采到一轮无用的数据只是多写一行日志。故障方向不对称。
    """
    try:
        import psycopg
        from cpt.adapters.a_share_local import connection_kwargs

        conn = psycopg.connect(**connection_kwargs())
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT max(date) FROM public.daily_bar")
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 读不到日历不该拦住采集
        return now.strftime("%Y-%m-%d")
    if not row or row[0] is None:
        return now.strftime("%Y-%m-%d")
    latest = row[0]
    today = now.date()
    # 库里最新一行是今天 ⇒ 交易日；比今天旧 ⇒ 今天还没行情（未开盘或休市）。
    if latest >= today:
        return today.isoformat()
    return None


def run_round(*, limit: int, main_board_only: bool) -> dict[str, Any]:
    """采一轮。**任何异常都收敛成返回值**，不往上抛（常驻循环要能继续）。"""
    started = time.monotonic()
    base = {"at": now_market().isoformat(timespec="seconds")}
    try:
        snap = IntradayCollector().scan(limit=limit, main_board_only=main_board_only)
    except IntradayError as exc:
        return {**base, "outcome": "error", "error": str(exc), "elapsed_s": _elapsed(started)}
    except Exception as exc:  # noqa: BLE001 - 采集层意外异常也要变成一次「失败轮」
        return {
            **base,
            "outcome": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_s": _elapsed(started),
        }

    board = seal_board(snap)
    if board["counts"]["candidates"] == 0:
        return {
            **base,
            "outcome": "empty",
            "rank_size": snap["rank_size"],
            "elapsed_s": _elapsed(started),
        }
    health = board_health(board)
    return {
        **base,
        "outcome": "ok",
        "rank_size": snap["rank_size"],
        "candidates": board["counts"]["candidates"],
        "sealed": board["counts"]["sealed"],
        "approaching": board["counts"]["approaching"],
        "unavailable": board["counts"]["unavailable"],
        "median_bid_lots": board["median_bid_lots"],
        "board_label": health["label"],
        "elapsed_s": _elapsed(started),
    }


def _elapsed(started: float) -> float:
    return round(time.monotonic() - started, 2)


def _append(record: dict[str, Any]) -> None:
    """写一行 JSON。日志写失败只打 stderr，不影响 rc —— 采集本身是主职。"""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[warn] 日志写不进去（{LOG_PATH}）：{exc}", file=sys.stderr)


def summarise(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """一次运行的总结。**字段固定** —— emotion-core 侧可以按这些字段做外部核对。"""
    ok = sum(1 for r in rounds if r["outcome"] == "ok")
    empty = sum(1 for r in rounds if r["outcome"] == "empty")
    errors = [r for r in rounds if r["outcome"] == "error"]
    return {
        "rounds": len(rounds),
        "ok": ok,
        "empty": empty,
        "errors": len(errors),
        "first_error": errors[0]["error"] if errors else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="盘中采集守护（只采集，不判定）")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    ap.add_argument("--rounds", type=int, default=1, help="跑几轮（默认 1）")
    ap.add_argument("--interval", type=float, default=30.0, help="轮询间隔秒（默认 30）")
    ap.add_argument("--limit", type=int, default=40, help="最多采多少只候选（默认 40）")
    ap.add_argument("--main-board-only", action="store_true", help="只采主板")
    ap.add_argument("--max-failures", type=int, default=MAX_FAILURES_DEFAULT)
    ap.add_argument("--ignore-session", action="store_true", help="不在盘中时段也照样采（排障用）")
    ap.add_argument("--json", action="store_true", help="把总结打成 JSON")
    args = ap.parse_args(argv)

    now = now_market()
    if not args.ignore_session and not in_session(now):
        record = {
            "at": now.isoformat(timespec="seconds"),
            "outcome": "skipped",
            "reason": f"不在盘中时段（{SESSION_START:%H:%M}-{SESSION_END:%H:%M} 北京时间）",
        }
        _append(record)
        print(json.dumps(record, ensure_ascii=False) if args.json else record["reason"])
        return 0

    day = trading_day(now)
    if day is None and not args.ignore_session:
        record = {
            "at": now.isoformat(timespec="seconds"),
            "outcome": "skipped",
            "reason": "库里的最新交易日比今天旧 —— 今天休市",
        }
        _append(record)
        print(json.dumps(record, ensure_ascii=False) if args.json else record["reason"])
        return 0

    total = 1 if args.once else max(1, args.rounds)
    rounds: list[dict[str, Any]] = []
    streak = 0
    for i in range(total):
        rec = run_round(limit=args.limit, main_board_only=args.main_board_only)
        rec["round"] = i + 1
        _append(rec)
        rounds.append(rec)
        if rec["outcome"] == "ok":
            streak = 0
        elif rec["outcome"] == "error":
            streak += 1
            if streak >= args.max_failures:
                summary = summarise(rounds)
                msg = f"盘中采集连续 {streak} 轮失败（阈值 {args.max_failures}）：{rec['error']}"
                print(f"!!!!! {msg} !!!!!", file=sys.stderr)
                if args.json:
                    print(json.dumps({**summary, "alert": msg}, ensure_ascii=False))
                return 1
        if i + 1 < total:
            time.sleep(args.interval)

    summary = summarise(rounds)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(
            f"跑了 {summary['rounds']} 轮：ok {summary['ok']} · empty {summary['empty']}"
            f" · error {summary['errors']}"
            + (f"（首个错误：{summary['first_error']}）" if summary["first_error"] else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
