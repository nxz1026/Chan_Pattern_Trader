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


# --------------------------------------------------------------------------- #
# 非负校验（R59 审计 L10）
#
# 改前只查 ``math.isfinite`` + OHLC 顺序：``close=-1``、``volume=-500`` 都是
# 「数学合法、语义荒谬」，能一路进分型/笔/中枢且零报错。
# --------------------------------------------------------------------------- #


def test_negative_price_is_rejected() -> None:
    """负价必须在校验层就拦下 —— 不能等它变成结构里的 high/low。"""
    invalid = make_canonical_bar(0, -1.0, 12.0, -2.0, -1.0, 299999)
    with pytest.raises(DataValidationError, match="为负数"):
        validate_canonical_bars([invalid])


def test_negative_volume_is_rejected() -> None:
    """量/额同理：``volume=-500`` 会让背驰力度度量变成无意义的负数。"""
    invalid = make_canonical_bar(0, 10.0, 12.0, 8.0, 10.0, 299999)
    object.__setattr__(invalid, "volume", -500.0)
    with pytest.raises(DataValidationError, match="volume"):
        validate_canonical_bars([invalid])


def test_zero_price_stays_allowed_by_the_validator_on_purpose() -> None:
    """0 价**刻意**不在校验层拒绝（与 R52 的逐行占位行处理配套）。

    判据若从 ``< 0`` 改成 ``<= 0``，一根脏行就能把**整只票**降级成
    ``DataValidationError``：见 ``tests/test_a_share_local.py`` 的
    ``test_zero_price_bar_would_have_passed_the_validator``。这条用例把那个
    取舍钉在这里，提醒后人别把 0 与负数混为一谈。
    """
    zero = make_canonical_bar(0, 0.0, 0.0, 0.0, 0.0, 299999)
    assert len(validate_canonical_bars([zero])) == 1
