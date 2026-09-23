from __future__ import annotations

import pytest
from cpt.adapters.validators import DataGapError, DataValidationError, validate_canonical_bars
from cpt.domain.models import make_canonical_bar


def bar(index: int, *, high: float = 12.0, low: float = 8.0):
    return make_canonical_bar(
        open_time=index * 300000,
        open=10.0,
        high=high,
        low=low,
        close=10.0,
        close_time=index * 300000 + 299999,
    )


def test_validation_deduplicates_and_returns_ordered_bars() -> None:
    result = validate_canonical_bars([bar(0), bar(1), bar(1)])
    assert [item.open_time for item in result] == [0, 300000]
    assert result[1].high == 12.0
    with pytest.raises(DataValidationError, match="严格大于"):
        validate_canonical_bars([bar(1), bar(0)])


def test_gap_is_explicitly_rejected() -> None:
    with pytest.raises(DataGapError, match="缺口"):
        validate_canonical_bars([bar(0), bar(2)])


def test_bad_ohlc_and_close_boundary_are_rejected() -> None:
    invalid = make_canonical_bar(0, 10, 12, 8, 10, 299999)
    object.__setattr__(invalid, "high", 7.0)
    with pytest.raises(DataValidationError, match="OHLC"):
        validate_canonical_bars([invalid])
    with pytest.raises(DataValidationError, match="close_time"):
        validate_canonical_bars([make_canonical_bar(0, 10, 12, 8, 10, 300000)])


def test_empty_input_is_allowed() -> None:
    assert validate_canonical_bars([]) == ()
