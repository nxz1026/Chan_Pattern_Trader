from __future__ import annotations

import pytest
from cpt.domain.models import Bi, ZhongShu
from cpt.domain.trend_type import classify_trend


def bi(index: int, direction: int, high: float, low: float, level: int = 0) -> Bi:
    return Bi(level, direction, index * 1000, index * 1000 + 999, high, low, (f"bi:{index}",))


def zs(index: int, high: float, low: float, level: int = 0) -> ZhongShu:
    return ZhongShu(
        level,
        index * 3000,
        index * 3000 + 1999,
        high,
        low,
        (f"bi:{index}", f"bi:{index + 1}", f"bi:{index + 2}"),
    )


def test_single_zone_with_continuing_overlap_is_consolidation() -> None:
    result = classify_trend(
        [bi(0, 1, 12, 6), bi(1, -1, 11, 5), bi(2, 1, 10, 7), bi(3, -1, 11, 8)],
        [zs(0, 10, 7)],
    )
    assert len(result) == 1
    assert result[0].kind == "open_end"
    assert result[0].direction == 0
    assert result[0].source_ids == ("bi:0", "bi:1", "bi:2", "bi:3")


def test_two_displaced_zones_with_leave_bi_form_trend() -> None:
    result = classify_trend(
        [
            bi(0, 1, 12, 6),
            bi(1, -1, 11, 5),
            bi(2, 1, 10, 7),
            bi(3, -1, 15, 11),
            bi(4, 1, 16, 12),
            bi(5, -1, 15, 10),
            bi(6, 1, 18, 14),
        ],
        [zs(0, 10, 7), zs(1, 15, 11)],
    )
    assert len(result) == 1
    assert result[0].kind == "open_end"
    assert result[0].direction == 1
    assert result[0].start_time == 0


def test_one_zone_without_continuation_is_forming_and_boundary_is_open_end() -> None:
    result = classify_trend(
        [bi(0, 1, 12, 6), bi(1, -1, 11, 5), bi(2, 1, 10, 7)],
        [zs(0, 10, 7)],
    )
    assert result[0].kind == "open_end"


def test_invalid_direction_level_and_cross_level_are_rejected() -> None:
    with pytest.raises(ValueError, match="direction"):
        classify_trend([bi(0, 0, 10, 5)], [])
    with pytest.raises(ValueError, match="level"):
        classify_trend([], [], level=-1)
    with pytest.raises(ValueError, match="level"):
        classify_trend([bi(0, 1, 10, 5, 0)], [zs(0, 8, 4, 1)])
    assert classify_trend([bi(0, 1, 10, 5, 0)], [zs(0, 8, 4, 1)], level=2)[0].level == 2


def test_empty_inputs_return_empty() -> None:
    assert classify_trend([], []) == ()
