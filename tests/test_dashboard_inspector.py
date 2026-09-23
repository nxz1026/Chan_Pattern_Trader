from __future__ import annotations

from cpt.application.dashboard_inspector import inspect_bar
from cpt.domain.bi import build_bis
from cpt.domain.contain import merge_contained_bars
from cpt.domain.fractal import detect_fractals
from cpt.domain.models import make_canonical_bar
from cpt.domain.zhongshu import build_zhongshus


def test_inspect_bar_returns_raw_and_merged_provenance() -> None:
    bars = tuple(
        make_canonical_bar(
            open_time=i * 300000,
            close_time=i * 300000 + 299999,
            open=10 + i,
            high=12 + i,
            low=8 + i,
            close=11 + i,
        )
        for i in range(5)
    )
    merged = merge_contained_bars(bars)
    result = inspect_bar(
        bars,
        merged,
        detect_fractals(merged),
        build_bis(detect_fractals(merged)),
        build_zhongshus(build_bis(detect_fractals(merged))),
        2,
    )
    assert result["bar_index"] == 2
    assert result["raw_bar"]["open_time"] == bars[2].open_time
    assert result["merged_bar"] is not None
    assert result["containment_chain"]
