from __future__ import annotations

import pytest
from cpt.domain.bi import build_bis
from cpt.domain.models import Fractal


def fx(kind: str, index: int, high: float, low: float, level: int = 0) -> Fractal:
    return Fractal(
        kind=kind,
        level=level,
        bar_index=index,
        start_time=index * 1000,
        end_time=index * 1000 + 999,
        high=high,
        low=low,
        source_ids=(f"merged:{index}",),
    )


def test_alternating_fractals_build_up_and_down_bis() -> None:
    result = build_bis([fx("bottom", 1, 9, 4), fx("top", 3, 14, 7), fx("bottom", 5, 10, 3)])
    assert len(result) == 2
    assert (result[0].direction, result[0].start_time, result[0].end_time) == (1, 1000, 3999)
    assert (result[0].high, result[0].low) == (14, 4)
    assert result[1].direction == -1
    assert result[1].source_ids == ("merged:3", "merged:5")


def test_same_kind_keeps_more_extreme_endpoint() -> None:
    result = build_bis(
        [
            fx("bottom", 1, 10, 5),
            fx("bottom", 2, 11, 4),
            fx("top", 4, 15, 8),
            fx("top", 5, 16, 9),
            fx("bottom", 7, 12, 3),
        ]
    )
    assert [(bi.direction, bi.start_time, bi.end_time) for bi in result] == [
        (1, 2000, 5999),
        (-1, 5000, 7999),
    ]
    assert result[0].source_ids == ("merged:2", "merged:5")


def test_equal_extreme_keeps_earlier_endpoint() -> None:
    result = build_bis([fx("bottom", 1, 10, 5), fx("bottom", 2, 11, 5), fx("top", 4, 15, 8)])
    assert result[0].source_ids == ("merged:1", "merged:4")


def test_kind_and_level_are_validated() -> None:
    with pytest.raises(ValueError, match="kind"):
        build_bis([fx("side", 1, 10, 5), fx("top", 2, 12, 7)])
    with pytest.raises(ValueError, match="level"):
        build_bis([], level=-1)
    with pytest.raises(ValueError, match="level"):
        build_bis([fx("bottom", 1, 10, 5, 0), fx("top", 2, 12, 7, 1)])
    assert build_bis([fx("bottom", 1, 10, 5, 0), fx("top", 2, 12, 7, 1)], level=3)[0].level == 3


def test_empty_and_single_fractal_return_no_bi() -> None:
    assert build_bis([]) == ()
    assert build_bis([fx("top", 1, 12, 7)]) == ()
