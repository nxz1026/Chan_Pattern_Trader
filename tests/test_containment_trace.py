from __future__ import annotations

from cpt.domain.contain import merge_contained_bars
from cpt.domain.containment_trace import trace_containment
from cpt.domain.models import make_canonical_bar


def test_trace_containment_explains_multi_bar_merged_group() -> None:
    bars = tuple(
        make_canonical_bar(
            open_time=index * 300000,
            close_time=index * 300000 + 299999,
            open=10.0 + index,
            high=12.0 + index,
            low=8.0 + index,
            close=11.0 + index,
        )
        for index in range(4)
    )
    merged = merge_contained_bars(bars)
    traces = trace_containment(bars, merged)
    assert all(trace.target_source_indices for trace in traces)
    assert all(trace.decision in {"contained", "merged"} for trace in traces)
