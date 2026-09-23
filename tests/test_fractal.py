from __future__ import annotations

import pytest
from cpt.domain.contain import merge_contained_bars
from cpt.domain.fractal import detect_fractals
from cpt.domain.models import make_canonical_bar


def bar(index: int, high: float, low: float):
    return make_canonical_bar(
        open_time=index * 1000,
        open=10.0,
        high=high,
        low=low,
        close=10.0,
        close_time=index * 1000 + 999,
    )


def test_strict_top_and_bottom_use_middle_bar_range() -> None:
    bars = [bar(0, 10, 5), bar(1, 12, 7), bar(2, 11, 6)]
    top = detect_fractals(bars, level=2)
    assert len(top) == 1
    assert top[0].kind == "top"
    assert top[0].level == 2
    assert (top[0].bar_index, top[0].start_time, top[0].end_time) == (1, 1000, 1999)
    assert (top[0].high, top[0].low) == (12, 7)
    assert top[0].source_ids == ("merged:1", "bar:1")

    bottom = detect_fractals([bar(0, 12, 7), bar(1, 10, 4), bar(2, 11, 5)])
    assert bottom[0].kind == "bottom"
    assert (bottom[0].high, bottom[0].low) == (10, 4)


def test_equal_high_or_low_does_not_form_fractal() -> None:
    assert detect_fractals([bar(0, 10, 5), bar(1, 12, 7), bar(2, 12, 6)]) == ()
    assert detect_fractals([bar(0, 10, 5), bar(1, 12, 7), bar(2, 11, 7)]) == ()


def test_short_input_and_invalid_level() -> None:
    assert detect_fractals([]) == ()
    assert detect_fractals([bar(0, 10, 5), bar(1, 11, 6)]) == ()
    with pytest.raises(ValueError, match="level"):
        detect_fractals([], level=-1)


def test_merged_source_indices_are_preserved() -> None:
    merged = merge_contained_bars(
        [
            bar(0, 12, 7),
            bar(1, 10, 8),
            bar(2, 14, 9),
            bar(3, 13, 10),
            bar(4, 13, 8),
        ]
    )
    fractals = detect_fractals(merged)
    assert len(fractals) == 1
    assert fractals[0].kind == "top"
    assert fractals[0].bar_index == 2
    assert fractals[0].source_ids == ("merged:1", "bar:2", "bar:3")


def test_custom_barlike_without_source_indices_uses_middle_index() -> None:
    class Element:
        def __init__(self, index: int, high: float, low: float) -> None:
            self.open_time = index * 1000
            self.close_time = index * 1000 + 999
            self.high = high
            self.low = low
            self.direction = 0

    result = detect_fractals([Element(0, 10, 5), Element(1, 12, 7), Element(2, 11, 6)])
    assert result[0].bar_index == 1
    assert result[0].source_ids == ("merged:1", "bar:1")
