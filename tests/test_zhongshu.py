from __future__ import annotations

import pytest
from cpt.domain.models import Bi
from cpt.domain.zhongshu import build_zhongshus


def bi(index: int, direction: int, high: float, low: float, level: int = 0) -> Bi:
    return Bi(
        level=level,
        direction=direction,
        start_time=index * 1000,
        end_time=index * 1000 + 999,
        high=high,
        low=low,
        source_ids=(f"bi:{index}",),
    )


def test_three_alternating_bis_with_common_overlap_form_zhongshu() -> None:
    result = build_zhongshus([bi(0, 1, 12, 6), bi(1, -1, 11, 5), bi(2, 1, 10, 7)])
    assert result == (result[0],)
    assert (result[0].high, result[0].low) == (10, 7)
    assert (result[0].start_time, result[0].end_time) == (0, 2999)
    assert result[0].bi_ids == ("bi:0", "bi:1", "bi:2")


def test_overlapping_followup_bi_extends_and_shrinks_zone() -> None:
    result = build_zhongshus(
        [
            bi(0, 1, 12, 6),
            bi(1, -1, 11, 5),
            bi(2, 1, 10, 7),
            bi(3, -1, 9.5, 7.5),
        ]
    )
    assert len(result) == 1
    assert (result[0].high, result[0].low) == (9.5, 7.5)
    assert result[0].end_time == 3999
    assert result[0].bi_ids == ("bi:0", "bi:1", "bi:2", "bi:3")


def test_non_overlapping_zone_starts_scan_from_ending_bi() -> None:
    result = build_zhongshus(
        [
            bi(0, 1, 12, 6),
            bi(1, -1, 11, 5),
            bi(2, 1, 10, 7),
            bi(3, -1, 20, 15),
            bi(4, 1, 19, 14),
            bi(5, -1, 18, 16),
        ]
    )
    assert len(result) == 2
    assert result[0].bi_ids == ("bi:0", "bi:1", "bi:2")
    assert result[1].bi_ids == ("bi:3", "bi:4", "bi:5")


def test_touching_or_non_alternating_bis_do_not_form_zone() -> None:
    assert build_zhongshus([bi(0, 1, 10, 5), bi(1, -1, 5, 0), bi(2, 1, 8, 4)]) == ()
    assert build_zhongshus([bi(0, 1, 10, 5), bi(1, 1, 9, 4), bi(2, -1, 8, 3)]) == ()


def test_direction_and_level_are_validated() -> None:
    with pytest.raises(ValueError, match="direction"):
        build_zhongshus([bi(0, 0, 10, 5), bi(1, -1, 9, 4), bi(2, 1, 8, 3)])
    with pytest.raises(ValueError, match="level"):
        build_zhongshus([], level=-1)
    with pytest.raises(ValueError, match="level"):
        build_zhongshus([bi(0, 1, 10, 5, 0), bi(1, -1, 9, 4, 1), bi(2, 1, 8, 3, 0)])
    assert (
        build_zhongshus([bi(0, 1, 10, 5, 0), bi(1, -1, 9, 4, 1), bi(2, 1, 8, 3, 0)], level=2)[
            0
        ].level
        == 2
    )
