#!/usr/bin/env python
"""形状扫描 CLI：把「某个价格形状之后 N 日收益多少」用同一把尺子量出来。

## 为什么有它

产品文案里凡是「X 之后平均涨 Y%」这类句子，都必须能用一条命令复跑出来。
复跑不出来的就不许写（R88 的「20 日新低后收阳 +0.81%」就是这么被作废的——
它复跑出来是 −0.25%，比基线还差）。

## 用法

    # 全市场，默认四个形状、h1/h5/h10/h20
    PYTHONPATH=. .venv/bin/python scripts/shape_scan.py --end 2026-10-22

    # 只跑一个形状、只看 20 日
    PYTHONPATH=. .venv/bin/python scripts/shape_scan.py --shape drop_4pct_up --horizons 20

    # 写 JSON 给文档/页面抄数
    PYTHONPATH=. .venv/bin/python scripts/shape_scan.py --json /tmp/shapes.json

## 口径（写死，不许在命令行改）

- 预热 250 根（``--warmup`` 可调，但报告里一定打出来）。
- 收益 = ``close[t+h] / close[t] - 1``，复权价；缺因子的位置不算样本。
- 基线 = **同一批面板、同一段、同一个 warmup** 的每根 bar，样本单元与形状一致。
- 涨跌停：``--tradable-only``（默认开）剔掉信号日涨停 —— 那天买不进去。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # pragma: no cover - 只在直接执行时生效
    sys.path.insert(0, str(_ROOT))

from cpt.backtest.panel import load_panel  # noqa: E402
from cpt.backtest.shapes import DEFAULT_WARMUP, SHAPES, describe, scan  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="扫描价格形状之后的前视收益")
    p.add_argument("--end", type=dt.date.fromisoformat, default=dt.date(2026, 10, 22))
    p.add_argument("--days", type=int, default=800, help="往前取多少个自然日（默认 800）")
    p.add_argument("--min-bars", type=int, default=DEFAULT_WARMUP)
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP, help="预热根数（默认 250）")
    p.add_argument(
        "--shape",
        action="append",
        default=None,
        help=f"只跑这些形状（可重复）。可选：{', '.join(s.name for s in SHAPES)}",
    )
    p.add_argument("--horizons", default="1,5,10,20", help="逗号分隔的前视窗口")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 只（0 = 全部）")
    p.add_argument(
        "--allow-limit-up",
        dest="tradable_only",
        action="store_false",
        default=True,
        help="不剔信号日涨停（默认剔）",
    )
    p.add_argument("--json", type=Path, default=None, help="把结果写成 JSON")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    wanted = [s for s in SHAPES if args.shape is None or s.name in set(args.shape)]
    unknown = set(args.shape or ()) - {s.name for s in SHAPES}
    if unknown:
        print(f"❌ 未知形状：{', '.join(sorted(unknown))}", file=sys.stderr)
        print(f"   可选：{', '.join(s.name for s in SHAPES)}", file=sys.stderr)
        return 2
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())

    import psycopg
    from cpt.adapters.a_share_local import connection_kwargs

    start = args.end - dt.timedelta(days=args.days)
    began = time.monotonic()

    conn = psycopg.connect(**connection_kwargs())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code, count(*) FROM public.daily_bar "
                "GROUP BY code HAVING count(*) >= %s ORDER BY code",
                (args.min_bars,),
            )
            codes = [r[0] for r in cur.fetchall()]
        if args.limit:
            codes = codes[: args.limit]
        panels = load_panel(
            conn,
            start=start,
            end=args.end,
            codes=codes,
            min_bars=args.min_bars,
            tradable_only=args.tradable_only,
        )
    finally:
        conn.close()

    rows = scan(panels, shapes=wanted, horizons=horizons, warmup=args.warmup)
    elapsed = time.monotonic() - began

    if not args.quiet:
        n_bars = sum(len(p) for p in panels.values())
        print(
            f"面板 {len(panels)} 只 / {n_bars:,} 根 · 窗口 {start} ~ {args.end}"
            f" · 预热 {args.warmup} 根 · 剔涨停 {args.tradable_only}"
            f" · 用时 {elapsed:.1f}s"
        )
        print()
        print(
            f"{'形状':<18}{'窗口':>4} {'n':>9} {'均值':>9} {'胜率':>7} {'基线均值':>10} {'差':>8}"
        )
        print("-" * 72)
        for row in rows:
            mean = row["mean"]
            base_mean = row["base_mean"]
            win = row["win_rate"]
            if not isinstance(mean, (int, float)) or not isinstance(win, (int, float)):
                print(f"{str(row['shape']):<18}{'h' + str(row['horizon']):>4} 样本不足")
                continue
            diff = (
                f"{(mean - base_mean) * 100:+.2f}pp" if isinstance(base_mean, (int, float)) else "—"
            )
            base = f"{base_mean * 100:+.2f}%" if isinstance(base_mean, (int, float)) else "—"
            print(
                f"{str(row['shape']):<18}{'h' + str(row['horizon']):>4} {row['n']:>9,} "
                f"{mean * 100:>+8.2f}% {win * 100:>6.1f}% {base:>10} {diff:>8}"
            )
        print()
        for row in rows:
            if row["horizon"] == max(horizons):
                print("  " + describe(row))
        print()
        print(f"基线 n = {rows[0]['base_n']:,}" if rows else "无可用形状")
        print("  人话：基线 = 同一批票随便挑一天买的 20 日均收益。形状要赢在胜率上，")
        print("        均值被少数极端样本拉高时中位数会先露馅（R87 踩过）。")

    if args.json:
        payload: dict[str, Any] = {
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
            "end": args.end.isoformat(),
            "start": start.isoformat(),
            "warmup": args.warmup,
            "tradable_only": args.tradable_only,
            "codes": len(panels),
            "rows": rows,
        }
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        if not args.quiet:
            print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
