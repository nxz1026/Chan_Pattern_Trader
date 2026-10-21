#!/usr/bin/env python
"""盘中封板探针（命令行）。**手动跑，不接进服务、不进 cron。**

用法::

    PYTHONPATH=. .venv/bin/python scripts/intraday_probe.py
    PYTHONPATH=. .venv/bin/python scripts/intraday_probe.py --main-board-only --json

为什么是脚本而不是 API：ADR 0003 §E2 定的分工是「原始采集归 CPT」，
但触发判据要等 emotion-core 那边接。在那之前这里只是**验证采集链路**

+ 给人看今天封板什么样子。别把它挂进 systemd timer —— 每轮 2 个 HTTP
请求、30 秒一轮，挂上去就是一直跑。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from cpt.adapters.a_share_public import SinaQuoteClient
from cpt.adapters.intraday_probe import IntradayCollector, IntradayError
from cpt.application.intraday_analysis import board_health, detect_weakening, seal_board


def _render(board: dict[str, Any], health: dict[str, Any], weak: list[dict[str, Any]]) -> str:
    c = board["counts"]
    lines = [
        (
            f"候选 {c['candidates']} 只 · 已封 {c['sealed']}"
            f" · 逼近 {c['approaching']} · 判不了 {c['unavailable']}"
        ),
        f"封板强度：{health['label']} —— {health['explain']}",
    ]
    if board["sealed"]:
        lines.append("")
        lines.append("已封板（按封单厚薄）：")
        for row in board["sealed"][:15]:
            lots = row["bid1_lots"]
            lots_s = "取不到" if lots is None else f"{lots:,.0f} 手"
            lines.append(f"  {row['code']} {row['name'] or '':<8} 封单 {lots_s}")
    if board["approaching"]:
        lines.append("")
        lines.append("逼近涨停（还没封）：")
        for row in board["approaching"][:10]:
            lines.append(
                f"  {row['code']} {row['name'] or '':<8} 距涨停 {row['distance_pct']:.2f}%"
            )
    if weak:
        lines.append("")
        lines.append("封单偏薄（可能松动）：")
        for row in weak[:10]:
            lines.append(f"  {row['code']} {row['name'] or '':<8} {row['reason']}")
    if board["unavailable"]:
        lines.append("")
        codes = " ".join(r["code"] for r in board["unavailable"])
        lines.append(f"五档取不到（数据缺失，不是没封）：{codes}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="盘中封板探针（不接进服务）")
    ap.add_argument("--limit", type=int, default=40, help="最多采几只（默认 40）")
    ap.add_argument("--main-board-only", action="store_true", help="只要主板")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是人话")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args(argv)

    collector = IntradayCollector(
        quote_client=SinaQuoteClient(timeout=args.timeout),
        rank_timeout=args.timeout,
    )
    try:
        snapshot = collector.scan(main_board_only=args.main_board_only, limit=args.limit)
    except (IntradayError, Exception) as exc:  # noqa: BLE001 - 命令行要打成人话
        print(f"采集失败：{exc}", file=sys.stderr)
        return 1

    board = seal_board(snapshot)
    health = board_health(board)
    weak = detect_weakening(board)

    if args.json:
        print(
            json.dumps(
                {"board": board, "health": health, "weakening": weak}, ensure_ascii=False, indent=2
            )
        )
    else:
        print(_render(board, health, weak))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
