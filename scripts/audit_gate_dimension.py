"""笔门槛量纲对照：native ``min_bi_len`` vs czsc ``check_bi.min_bi_len``。

## 为什么要量这个

``docs/rules.md`` §9.8 给 ``min_bi_len = 6`` 的依据是两条：

1. 「与 czsc ``check_bi`` 的门槛对齐」（``crates/czsc-core/src/analyze/utils.rs``）；
2. 「实测在 4–7 区间笔数几乎不敏感（3 个 oracle fixture 恒 ~50 笔）」。

本脚本独立复量这两条。所有跨度统一折算到 **native ``merged_index`` 空间**
（去包含后 K 线下标），再换算出缠论原文的「独立 K 线数」：

    中心距 = merged跨度 − 1
    独立K  = 中心距 − 3 = merged跨度 − 4

## 两侧判据的源码位置

- native ``cpt/domain/bi.py:183``：
  ``int(end.merged_index) - int(start.merged_index) + 1 >= min_bi_len``
  ⇒ ``min_bi_len = merged跨度``
- czsc ``crates/czsc-core/src/analyze/utils.rs:413-417``：
  ``!ab_include && bars_a.len() >= min_bi_len``，其中 ``bars_a`` 从
  ``fx_a.elements[0].dt`` 量到 ``fx_b.elements[2].dt``（两个 3 根分型窗口的完整跨度）
  ⇒ ``min_bi_len = merged跨度 + 2``

## 截断陷阱

``czsc.CZSC(..., max_bi_num=0)`` **不是**「不限」：内核
``crates/czsc-core/src/analyze/mod.rs:88`` 的 ``resolve_max_bi_num`` 把 0 当
「未指定」，回落默认 **50**，并连 ``bars_raw`` 一起从头砍掉。故本脚本一律用
:data:`MAX_BI_NUM_UNLIMITED`。

运行：``.venv/bin/python scripts/audit_gate_dimension.py``
"""

from __future__ import annotations

import csv
import pathlib
import statistics
import sys
from datetime import UTC
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
# ⚠️ 必须先于 `import cpt`：本仓 `cpt` 的 editable 安装指向**另一个 checkout**，
# 不修正 sys.path 会静默对着另一份代码运算（详见 ``scripts/check_doc_drift.py:108``）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import czsc  # noqa: E402
from cpt.adapters.czsc_chanlun import MAX_BI_NUM_UNLIMITED, _to_naive_utc  # noqa: E402
from cpt.domain.bi import build_bis  # noqa: E402
from cpt.domain.contain import merge_contained_bars  # noqa: E402
from cpt.domain.fractal import detect_fractals  # noqa: E402
from cpt.domain.models import CanonicalBar  # noqa: E402

ORACLE: pathlib.Path = ROOT / "tests" / "fixtures" / "oracle"
FIXTURES: tuple[str, ...] = (
    "btcusdt_5m_2024-02-01.csv",
    "btcusdt_5m_2024-09-01.csv",
    "btcusdt_5m_2025-04-01.csv",
)
GATES: tuple[int, ...] = (4, 5, 6, 7, 8)


def load(path: pathlib.Path) -> list[CanonicalBar]:
    """读 oracle CSV 为 ``CanonicalBar`` 列表。"""
    with path.open(encoding="utf-8", newline="") as fh:
        return [
            CanonicalBar(
                open_time=int(r["open_time"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
                close_time=int(r["close_time"]),
                quote_volume=float(r["quote_volume"]),
                trade_count=int(r["trade_count"]),
                taker_buy_base_volume=float(r["taker_buy_base_volume"]),
                taker_buy_quote_volume=float(r["taker_buy_quote_volume"]),
                is_closed=True,
            )
            for r in csv.DictReader(fh)
        ]


def _ms(dt: Any) -> int:
    """czsc 的 tz-naive UTC ``datetime`` → 毫秒时间戳。"""
    return int(dt.replace(tzinfo=UTC).timestamp() * 1000)


def _orig_to_merged(bars: list[CanonicalBar]) -> tuple[list[int], int]:
    """原始 K 线下标 → 合并K线下标 的映射表，以及合并后根数。"""
    merged = merge_contained_bars(bars)
    table = [0] * len(bars)
    for mi, m in enumerate(merged):
        for oi in m.source_indices:
            table[int(oi)] = mi
    return table, len(merged)


def _stat(spans: list[int]) -> tuple[int, int, int, float]:
    """(笔数, min, max, 中位) —— 空序列返回全零。"""
    if not spans:
        return 0, 0, 0, 0.0
    return len(spans), min(spans), max(spans), float(statistics.median(spans))


def native_stats(bars: list[CanonicalBar], gate: int | None) -> dict[str, Any]:
    """native 路径：merge → fractals → build_bis(min_bi_len=gate)。"""
    merged = merge_contained_bars(bars)
    fxs = detect_fractals(merged)
    bis = build_bis(fxs, min_bi_len=gate)
    mi = [int(f.merged_index) for f in fxs]
    st = {f.start_time: i for i, f in enumerate(fxs)}
    et = {f.end_time: i for i, f in enumerate(fxs)}
    spans = [mi[et[b.end_time]] - mi[st[b.start_time]] + 1 for b in bis]
    n, lo, hi, med = _stat(spans)
    return {
        "n_fx": len(fxs),
        "n_bi": len(bis),
        "span_min": lo,
        "span_max": hi,
        "span_p50": med,
    }


def czsc_stats(bars: list[CanonicalBar], gate: int) -> dict[str, Any]:
    """czsc 路径：``max_bi_num`` 真正放开，跨度折算到 native merged 空间。"""
    table, n_merged = _orig_to_merged(bars)
    ms_to_orig = {int(b.open_time): i for i, b in enumerate(bars)}
    freq = czsc.Freq.F5
    raw_bars = [
        czsc.RawBar(
            symbol="CPT",
            id=i,
            dt=_to_naive_utc(b.open_time),
            freq=freq,
            open=float(b.open),
            close=float(b.close),
            high=float(b.high),
            low=float(b.low),
            vol=float(b.volume),
            amount=float(b.quote_volume),
        )
        for i, b in enumerate(bars)
    ]
    analyzer = czsc.CZSC(raw_bars, max_bi_num=MAX_BI_NUM_UNLIMITED, min_bi_len=gate)
    spans: list[int] = []
    for bi in analyzer.bi_list:
        a = ms_to_orig.get(_ms(bi.fx_a.dt))
        z = ms_to_orig.get(_ms(bi.fx_b.dt))
        if a is None or z is None:
            continue
        spans.append(table[z] - table[a] + 1)
    n, lo, hi, med = _stat(spans)
    return {
        "n_fx": len(analyzer.fx_list),
        "n_bi": len(analyzer.bi_list),
        "n_merged": n_merged,
        "span_min": lo,
        "span_max": hi,
        "span_p50": med,
    }


def main() -> int:
    """打印对照表。"""
    for name in FIXTURES:
        bars = load(ORACLE / name)
        print(f"\n### {name}  （原始 {len(bars)} 根）")
        nat0 = native_stats(bars, None)
        print(f"  native  fractals={nat0['n_fx']}  merged={len(merge_contained_bars(bars))}")
        header = "  gate | native笔 | min跨度 | czsc笔 | min跨度 | czsc独立K | native独立K"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for g in GATES:
            nat = native_stats(bars, g)
            cz = czsc_stats(bars, g)
            print(
                f"  {g:4d} | {nat['n_bi']:7d} | {nat['span_min']:7d} | "
                f"{cz['n_bi']:6d} | {cz['span_min']:7d} | "
                f"{cz['span_min'] - 4:9d} | {nat['span_min'] - 4:11d}"
            )
        cz_ungated = czsc_stats(bars, 3)
        print(
            f"  czsc  fractals={cz_ungated['n_fx']}  "
            f"merged={cz_ungated['n_merged']}  (max_bi_num 放开)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
