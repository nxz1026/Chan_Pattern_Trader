#!/usr/bin/env python3
"""回测命令行：把 R70 那套 A/B 验证变成可重复执行的一条命令。

用法::

    # 全市场、默认窗口（最近 800 自然日）
    .venv/bin/python scripts/backtest_run.py

    # 指定窗口 + 只跑前 200 只（调试用）
    .venv/bin/python scripts/backtest_run.py --end 2026-10-19 --days 800 --limit 200

    # 写 JSON 报告
    .venv/bin/python scripts/backtest_run.py --json /tmp/bt.json

## 与 R70 探针的区别

| | R70 探针 | 本脚本 |
|---|---|---|
| 位置 | ``/tmp`` 一次性 | 仓内，可复用 |
| 涨跌停 | 自己按 0.098/0.198 算 | 读 ``derived_bar``（CPT 从不算涨跌停） |
| 价格口径 | 后复权（来自适配层） | 后复权（``ref_adjust_factor``） |
| 统计单元 | 全量 K 线当基线 | 事件样本（**不配对**，见 R88） |
| 结论 | 人工看日志 | ``stats.compare`` 判四级，噪声不许报差异 |

**跑一次约 20–40 分钟**（5,131 只票 × 527 根），用 ``--limit`` 先小规模验证。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cpt.backtest.panel import HORIZONS, load_panel  # noqa: E402
from cpt.backtest.runner import A_STATUS, B_STATUS, run_backtest  # noqa: E402
from cpt.backtest.signals import (  # noqa: E402
    DEFAULT_MIN_BI_LEN,
    DEFAULT_WARMUP,
    structure_signals,
    trace_summary,  # noqa: E402
)


def _parse_date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.3f}%"


def _win(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _watchlist_codes() -> set[str]:
    """关注列表里 A 股的代码集合。

    读不到（文件不存在/格式坏）就返回空集合 —— 调用方会因此跑 0 只，
    这比悄悄退回全市场诚实。
    """
    from cpt.adapters.a_share_pool import WatchlistStore
    from cpt.web.a_share_routes import watchlist_path_for

    return {e.code for e in WatchlistStore(watchlist_path_for()).list() if e.market == "A"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="跑一次 A/B 回测（structure_ready vs confirmed）")
    p.add_argument(
        "--end",
        type=_parse_date,
        default=dt.date(2026, 10, 19),
        help="回测截止交易日（含），默认 2026-10-19 —— R70 用的窗口，便于对账",
    )
    p.add_argument("--days", type=int, default=800, help="往前取多少个自然日（默认 800）")
    p.add_argument("--min-bars", type=int, default=DEFAULT_WARMUP, help="少于这么多根的票丢掉")
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP, help="结构冷启动根数")
    p.add_argument("--min-bi-len", type=int, default=DEFAULT_MIN_BI_LEN, help="笔的最短根数")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 只（0 = 全部）")
    p.add_argument(
        "--only",
        choices=("watchlist",),
        default=None,
        help="只跑关注列表里的 A 股（--limit 仍然生效）",
    )
    p.add_argument("--tradable-only", action="store_true", default=True)
    p.add_argument(
        "--include-limit-up",
        dest="tradable_only",
        action="store_false",
        help="**含涨停日**。对账用；正常分析别开 —— 那些样本买不进去",
    )
    p.add_argument("--json", type=Path, default=None, help="把结果写成 JSON")
    p.add_argument("--quiet", action="store_true", help="不打进度")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    import psycopg
    from cpt.adapters.a_share_local import AShareLocalClient, connection_kwargs
    from cpt.adapters.native_chanlun import NativeChanlunBackend
    from cpt.adapters.reference_chanlun import ReferenceChanlunConfig

    start = args.end - dt.timedelta(days=args.days)
    started = time.monotonic()

    conn = psycopg.connect(**connection_kwargs())
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code, count(*) FROM public.daily_bar "
                "GROUP BY code HAVING count(*) >= %s ORDER BY code",
                (args.min_bars,),
            )
            codes = [r[0] for r in cur.fetchall()]
        if args.only == "watchlist":
            watched = _watchlist_codes()
            codes = [c for c in codes if c in watched]
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

    if not args.quiet:
        print(f"票池 {len(codes)} 只 → 面板 {len(panels)} 只", flush=True)

    client = AShareLocalClient()
    backend = NativeChanlunBackend(min_bi_len=args.min_bi_len)
    config = ReferenceChanlunConfig()
    start_ms = int(dt.datetime.combine(start, dt.time(), dt.UTC).timestamp() * 1000)
    # 多取 40 个自然日，保证末 20 根还能算 h20 —— 与 load_panel 的 +30 天同向。
    end_ms = int(
        dt.datetime.combine(args.end + dt.timedelta(days=40), dt.time(), dt.UTC).timestamp() * 1000
    )

    traces = {}
    dropped = 0
    # **失败原因必须报出来。** 第一版这里是裸 ``except Exception: dropped += 1``，
    # 全市场跑完 ``dropped=5032``，日志里一个字的原因都没有 —— 五小时的跑批
    # 只换来一句「5023 只被丢了」，等于白跑。分类计数 + 首个样例。
    reasons: dict[str, int] = {}
    first_error: tuple[str, str] | None = None

    def _drop(kind: str, code: str, detail: str = "") -> None:
        nonlocal dropped, first_error
        dropped += 1
        reasons[kind] = reasons.get(kind, 0) + 1
        if first_error is None:
            first_error = (code, detail or kind)

    for i, code in enumerate(sorted(panels)):
        try:
            bars = list(client.fetch_validated_klines(code, start_ms, end_ms).bars)
        except Exception as exc:  # noqa: BLE001 - 单只票失败不该中断整轮
            _drop(type(exc).__name__, code, f"{type(exc).__name__}: {exc}")
            continue
        if len(bars) < args.min_bars:
            _drop("TOO_SHORT", code, f"只有 {len(bars)} 根，少于 {args.min_bars}")
            continue
        traces[code] = structure_signals(
            bars,
            code=code,
            backend=backend,
            config=config,
            warmup=args.warmup,
        )
        if reasons and first_error is not None and (i + 1) == 1:
            print(f"  ⚠️ 首只失败样例：{first_error[0]} — {first_error[1]}", flush=True)
        if not args.quiet and (i + 1) % 200 == 0:
            elapsed = time.monotonic() - started
            done = i + 1
            eta = elapsed / done * (len(panels) - done)
            print(
                f"  .. {done}/{len(panels)}  已用 {elapsed / 60:.1f} 分"
                f"  预计剩余 {eta / 60:.1f} 分",
                flush=True,
            )

    result = run_backtest(panels=panels, traces=traces)
    _report(args, result, traces, start, dropped, time.monotonic() - started, reasons, first_error)
    return 0


def _report(  # noqa: PLR0913 - 报告层的入参就是这一轮的全部产物，拆开反而更难读
    args: argparse.Namespace,
    result: Any,
    traces: dict[str, Any],
    start: dt.date,
    dropped: int,
    elapsed: float,
    reasons: dict[str, int] | None = None,
    first_error: tuple[str, str] | None = None,
) -> None:
    print()
    print("=" * 78)
    print(f"窗口 {start} ~ {args.end}   票数 {result.codes}   耗时 {elapsed / 60:.1f} 分")
    print(f"剔除（取数失败/太短）{dropped} 只   算不出结构的 bar {result.skipped_bars:,} 根")
    if reasons:
        print("剔除原因分类：" + "、".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    if first_error is not None:
        print(f"首例：{first_error[0]} — {first_error[1][:200]}")
    print(f"涨跌停口径：{'排除涨停日（可成交）' if args.tradable_only else '含涨停日'}")
    print("=" * 78)

    summary = trace_summary(list(traces.values()))
    print("\n【信号计数】两个数不是一回事，别混着读：")
    print(
        f"  逐日状态（同一结构每天一行，状态不变也记）"
        f"  合计 {sum(summary['status_days'].values()):,}"
    )
    for status, cnt in sorted(summary["status_days"].items()):
        print(f"    {status:<18} {cnt:>8,}")
    print(
        f"  状态变化（真正的事件，本脚本的样本量）"
        f"      合计 {sum(summary['transitions'].values()):,}"
    )
    for status, cnt in sorted(summary["transitions"].items()):
        print(f"    {status:<18} {cnt:>8,}")
    print(f"  算不出结构的 bar  {summary['skipped_bars']:,} 根（笔为空或数据不足）")

    print("\n【各状态单独当买点】（**不依赖未来**，每个事件都收）")
    header = f"  {'状态':<18}" + "".join(f"{f'h{h}':>12}" for h in HORIZONS)
    print(header)
    print(f"  {'':<18}" + "".join(f"{'均值/胜率':>12}" for _ in HORIZONS))
    for status in (A_STATUS, B_STATUS, "invalidated"):
        got = result.status.get(status)
        if got is None:
            continue
        cells = []
        for h in HORIZONS:
            s = got.summary(h)
            cells.append(f"{_pct(s.mean)}/{_win(s.win_rate)}" if s else "—")
        print(f"  {status:<18}" + "".join(f"{c:>12}" for c in cells))
        print(f"  {'  (因涨停丢掉)':<16}" + "".join(f"{got.not_tradable:>12,}" for _ in HORIZONS))
        print(f"  {'  (窗口不足)':<16}" + "".join(f"{got.no_window:>12,}" for _ in HORIZONS))

    if args.json:
        payload = {
            "window": {"start": start.isoformat(), "end": args.end.isoformat()},
            "codes": result.codes,
            "dropped": dropped,
            "tradable_only": bool(args.tradable_only),
            "elapsed_minutes": round(elapsed / 60, 2),
            "trace_summary": summary,
            "status": {
                s: {
                    str(h): {
                        "n": len(got.by_horizon.get(h, ())),
                        "mean": _mean([x.ret for x in got.by_horizon.get(h, ())]),
                        "win_rate": _win_rate([x.ret for x in got.by_horizon.get(h, ())]),
                    }
                    for h in HORIZONS
                }
                for s, got in result.status.items()
            },
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON 已写入 {args.json}")


def _mean(values: Iterable[float]) -> float | None:
    vals = list(values)
    return sum(vals) / len(vals) if vals else None


def _win_rate(values: Iterable[float]) -> float | None:
    vals = list(values)
    return sum(1 for v in vals if v > 0) / len(vals) if vals else None


if __name__ == "__main__":
    raise SystemExit(main())
