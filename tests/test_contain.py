from __future__ import annotations

from dataclasses import replace

import pytest
from cpt.domain.contain import MergedBar, merge_contained_bars
from cpt.domain.models import CanonicalBar, make_canonical_bar
from cpt.domain.types import BarLike


def bar(index: int, high: float, low: float, *, open_: float = 10.0) -> CanonicalBar:
    return make_canonical_bar(
        open_time=index * 1000,
        open=open_,
        high=high,
        low=low,
        close=open_,
        close_time=index * 1000 + 999,
        volume=1.0,
        quote_volume=2.0,
        trade_count=3,
        taker_buy_base_volume=0.5,
        taker_buy_quote_volume=1.0,
    )


def test_single_bar_is_mapped_and_satisfies_barlike() -> None:
    result = merge_contained_bars([bar(0, 12, 8)])
    assert len(result) == 1
    assert isinstance(result[0], MergedBar)
    assert isinstance(result[0], BarLike)
    assert result[0].source_indices == (0,)
    assert result[0].high == 12
    assert result[0].low == 8


def test_upward_containment_uses_high_and_low_maxima_and_accumulates() -> None:
    result = merge_contained_bars([bar(0, 12, 7), bar(1, 10, 8), bar(2, 11, 9)])
    assert len(result) == 1
    merged = result[0]
    assert (merged.high, merged.low) == (12, 9)
    assert merged.source_indices == (0, 1, 2)
    assert (merged.volume, merged.quote_volume, merged.trade_count) == (3.0, 6.0, 9)
    assert merged.close_time == 2999


def test_downward_containment_uses_high_and_low_minima() -> None:
    result = merge_contained_bars(
        [bar(0, 12, 8), bar(1, 11, 9), bar(2, 10, 8.5), bar(3, 9, 8)],
        direction="backward",
    )
    assert len(result) == 1
    assert (result[0].high, result[0].low) == (9, 8)
    assert result[0].source_indices == (0, 1, 2, 3)


def test_non_contained_bars_are_preserved_and_next_trend_is_inferred() -> None:
    result = merge_contained_bars([bar(0, 12, 7), bar(1, 10, 8), bar(2, 14, 9), bar(3, 13, 10)])
    assert len(result) == 2
    assert result[0].source_indices == (0, 1)
    assert (result[0].high, result[0].low) == (12, 8)
    assert result[1].source_indices == (2, 3)


def test_backward_tie_break_changes_first_contained_pair() -> None:
    bars = [bar(0, 10, 5), bar(1, 9, 6)]
    forward = merge_contained_bars(bars, direction="forward")[0]
    backward = merge_contained_bars(bars, direction="backward")[0]
    assert (forward.high, forward.low) == (10, 6)
    assert (backward.high, backward.low) == (9, 5)


def test_equal_boundary_is_containment_and_direction_is_validated() -> None:
    result = merge_contained_bars([bar(0, 10, 5), bar(1, 10, 5)])
    assert len(result) == 1
    with pytest.raises(ValueError, match="direction"):
        merge_contained_bars([], direction="sideways")


def test_structure_element_is_mapped_without_fabricating_volume() -> None:
    class Element:
        open_time = 0
        close_time = 1000
        high = 12.0
        low = 8.0
        direction = 1

    result = merge_contained_bars([Element()])
    assert result[0].open == result[0].close == 10.0
    assert result[0].volume == 0.0
    assert result[0].source_indices == (0,)


def test_is_closed_and_last_close_are_taken_from_last_component() -> None:
    first = bar(0, 10, 5, open_=7.0)
    second = replace(bar(1, 9, 6, open_=8.0), is_closed=False)
    result = merge_contained_bars([first, second])
    assert result[0].open == first.open
    assert result[0].close == second.close
    assert result[0].is_closed is False
