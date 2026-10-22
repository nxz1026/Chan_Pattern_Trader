"""封板存活回顾的命令行入口（R90）。

收盘后跑一次，回答「今天冲到板的票，封住了几只、开过几次板」。

    .venv/bin/python scripts/intraday_review.py                 # 今天
    .venv/bin/python scripts/intraday_review.py --date 2026-10-22
    .venv/bin/python scripts/intraday_review.py --json          # 给脚本/飞书用

**只读快照表，不采行情、不判涨停** —— 判据只在采集端有一个实现。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.application.intraday_review import session_summary, track_summary  # noqa: E402

_MARKET_TZ: Final[dt.tzinfo] = dt.timezone(dt.timedelta(hours=8))


def _parse_date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="盘中封板存活回顾（只读快照表）")
    p.add_argument("--date", type=_parse_date, default=None, help="看哪一天（默认今天，北京时间）")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--codes", action="store_true", help="同时列出每只票的小结")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    day = args.date or dt.datetime.now(_MARKET_TZ).date()
    start = dt.datetime(day.year, day.month, day.day, tzinfo=_MARKET_TZ)
    end = start + dt.timedelta(days=1)

    import psycopg
    from cpt.adapters.a_share_local import connection_kwargs
    from cpt.storage.intraday_seal_store import load_session_rows

    with psycopg.connect(**connection_kwargs()) as conn:
        rows = load_session_rows(conn, start=start, end=end)

    summary = session_summary(rows)
    payload: dict[str, Any] = {"date": day.isoformat(), **summary}
    if args.codes and rows:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row["code"], []).append(row)
        payload["tracks"] = [track_summary(t) for t in groups.values()]

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return 0

    print(f"=== {day} 盘中封板回顾 ===")
    print(summary["headline"] if summary["codes"] else f"{day} 没有封板快照。")
    if summary["rounds"]:
        print(
            f"样本：{summary['rounds']} 轮 · 每只票 {summary['rounds_min']}~"
            f"{summary['rounds_max']} 轮"
        )
    if args.codes:
        print()
        for t in payload.get("tracks", []):
            flag = (
                "封到底"
                if t["sealed_now"] and not t["opened_board"]
                else (f"开板 {t['open_board_count']} 次" if t["opened_board"] else "没封住")
            )
            lots = "—" if t["max_bid1_lots"] is None else f"{t['max_bid1_lots']:.0f}"
            print(f"  {t['code']}  {t['rounds']:>3} 轮  封单峰值 {lots:>10} 手  {flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
