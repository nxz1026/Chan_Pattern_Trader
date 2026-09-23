from __future__ import annotations

from typing import Any

from cpt.application.dashboard_inspector import inspect_bar
from cpt.domain.bi import build_bis
from cpt.domain.contain import merge_contained_bars
from cpt.domain.fractal import detect_fractals
from cpt.domain.models import make_canonical_bar
from cpt.domain.zhongshu import build_zhongshus


def _containment_bars() -> tuple[Any, ...]:
    """构造含包含关系的 K 线序列：bar 0/1 合并 (bar 1 被 bar 0 包含)。"""
    return (
        make_canonical_bar(open_time=0, close_time=299999, open=10, high=20, low=0, close=15),
        make_canonical_bar(open_time=300000, close_time=599999, open=11, high=15, low=8, close=12),
        make_canonical_bar(open_time=600000, close_time=899999, open=12, high=18, low=6, close=14),
        make_canonical_bar(
            open_time=900000, close_time=1199999, open=13, high=22, low=12, close=20
        ),
    )


def test_inspect_bar_returns_raw_and_merged_provenance() -> None:
    bars = _containment_bars()
    merged = merge_contained_bars(bars)
    fractals = detect_fractals(merged)
    bis = build_bis(fractals)
    zhongshus = build_zhongshus(bis)
    result = inspect_bar(bars, fractals, bis, zhongshus, 1)
    assert result["bar_index"] == 1
    assert result["raw_bar"]["open_time"] == bars[1].open_time
    assert result["merged_bar"] is not None
    assert set(result["merged_bar"]["source_indices"]) == {0, 1}
    assert result["containment_chain"]
    chain = result["containment_chain"]
    assert all("decision" in entry for entry in chain)
    assert all(entry["decision"] in {"contained", "merged"} for entry in chain)


def test_inspect_bar_uses_trace_containment_for_decision() -> None:
    """B3 验收：containment 决策由 trace_containment 产出，非手工常量。"""
    from cpt.domain.containment_trace import trace_containment

    bars = _containment_bars()
    merged = merge_contained_bars(bars)
    fractals = detect_fractals(merged)
    bis = build_bis(fractals)
    zhongshus = build_zhongshus(bis)
    result = inspect_bar(bars, fractals, bis, zhongshus, 1)
    expected = trace_containment(tuple(bars), tuple(merged))
    merged_ids = set(result["merged_bar"]["source_indices"])
    matched = [d for d in expected if d.source_bar_index in merged_ids]
    assert len(result["containment_chain"]) == len(matched) >= 1
    first = result["containment_chain"][0]
    assert first["decision"] == matched[0].decision
    assert first["resulting_high"] == matched[0].resulting_high
    assert first["resulting_low"] == matched[0].resulting_low
