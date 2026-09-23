from __future__ import annotations

import pytest
from cpt.domain.models import TrendType
from cpt.domain.recursion import StructureElement, map_trend_types
from cpt.domain.types import BarLike


def trend(index: int, direction: int, kind: str = "consolidation") -> TrendType:
    return TrendType(
        level=0,
        kind=kind,
        direction=direction,
        start_time=index * 1000,
        end_time=index * 1000 + 999,
        high=10.0 + index,
        low=5.0 + index,
        source_ids=(f"trend:{index}",),
    )


def test_confirmed_trends_map_to_barlike_structure_elements() -> None:
    result = map_trend_types([trend(0, 1), trend(1, -1)], target_level=1)
    assert len(result) == 2
    assert all(isinstance(item, BarLike) for item in result)
    assert all(isinstance(item, StructureElement) for item in result)
    assert result[0].level == 1
    assert result[0].open_time == result[0].start_time == 0
    assert result[0].source_ids == ("trend:0",)
    assert result[0].source_revision == 0


def test_direction_zero_and_overlapping_elements_are_filtered() -> None:
    result = map_trend_types([trend(0, 1), trend(1, 0), trend(2, -1), trend(3, 1)], target_level=2)
    assert [item.source_ids for item in result] == [("trend:0",), ("trend:2",), ("trend:3",)]


def test_status_is_inherited_and_min_elements_is_validation_only() -> None:
    result = map_trend_types([trend(0, 1, "open_end")], target_level=1, min_elements=5)
    assert result[0].status == "open_end"
    with pytest.raises(ValueError, match="target_level"):
        map_trend_types([], target_level=-1)
    with pytest.raises(ValueError, match="min_elements"):
        map_trend_types([], target_level=1, min_elements=0)


def test_empty_input_returns_empty() -> None:
    assert map_trend_types([], target_level=1) == ()
