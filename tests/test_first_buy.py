"""一买 / 一卖结构谓词（``emotion_cpt.structure.first_buy`` 的 CPT 侧契约）。

## 这个文件曾经是空的

R96（``3b569e3b4``）删按需因子链时，把本文件的用例一起删了、只留下 import ——
43 行、**0 个 test 函数**，docstring 却仍声称覆盖三类判据。pytest 收集 0 个用例
⇒ 永远绿 ⇒ 一买/一卖谓词在本仓**零覆盖**（2026-10-29 审计实测）。

现在按判据的五个门槛重写，全部是纯函数、零 IO：

1. 笔数必须为奇数、首尾同向；
2. 一买末笔向下、一卖末笔向上；
3. 整段最高点必须落在首笔（一买）/ 末笔（一卖）；
4. 力度度量未填充（``length <= 0``）必须**响亮报错**，不许静默返回 False；
5. 背驰：价格力度变小 **且**（量或长度至少一个变小）。

与 czsc 的逐条对账由 ``tests/test_czsc_backend.py`` 在装了 ``chan`` extra 时做；
本文件只钉「口径本身」，不依赖 czsc。
"""

from __future__ import annotations

import pytest
from cpt.domain.first_buy import (
    check_first_buy,
    check_first_sell,
    mean,
    round_to_2_digit,
)
from cpt.domain.models import Bi

#: 笔方向（与 ``cpt.domain.first_buy`` 内部常量同值）。
_UP = 1
_DOWN = -1


def _bi(
    direction: int,
    *,
    high: float,
    low: float,
    power_price: float,
    power_volume: float,
    length: int,
    index: int = 0,
) -> Bi:
    """造一笔。时间戳只要单调，判据不读它。"""
    return Bi(
        level=1,
        direction=direction,
        start_time=index * 1000,
        end_time=index * 1000 + 500,
        high=high,
        low=low,
        source_ids=(f"bar-{index}",),
        power_price=power_price,
        power_volume=power_volume,
        length=length,
    )


def _downtrend_divergent() -> list[Bi]:
    """5 笔下跌，末笔价格与量都缩小 ⇒ 一买成立。"""
    return [
        _bi(_DOWN, high=100.0, low=90.0, power_price=10.0, power_volume=1000.0, length=10, index=0),
        _bi(_UP, high=97.0, low=90.0, power_price=7.0, power_volume=500.0, length=8, index=1),
        _bi(_DOWN, high=97.0, low=85.0, power_price=12.0, power_volume=1200.0, length=12, index=2),
        _bi(_UP, high=92.0, low=85.0, power_price=7.0, power_volume=400.0, length=7, index=3),
        _bi(_DOWN, high=92.0, low=80.0, power_price=9.0, power_volume=300.0, length=5, index=4),
    ]


def _uptrend_divergent() -> list[Bi]:
    """5 笔上涨，末笔价格与量都缩小 ⇒ 一卖成立（一买的镜像）。"""
    return [
        _bi(_UP, high=100.0, low=90.0, power_price=10.0, power_volume=1000.0, length=10, index=0),
        _bi(_DOWN, high=100.0, low=93.0, power_price=7.0, power_volume=500.0, length=8, index=1),
        _bi(_UP, high=108.0, low=93.0, power_price=15.0, power_volume=1200.0, length=12, index=2),
        _bi(_DOWN, high=108.0, low=101.0, power_price=7.0, power_volume=400.0, length=7, index=3),
        _bi(_UP, high=112.0, low=101.0, power_price=9.0, power_volume=300.0, length=5, index=4),
    ]


# ── 工具函数 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.125, 1.13),
        (-1.125, -1.13),
        (0.005, 0.01),
        (3.1, 3.1),
        (0.0, 0.0),
    ],
)
def test_round_to_2_digit_rounds_half_away_from_zero(value: float, expected: float) -> None:
    """.5 边界必须**远离零**（Python 内建 round 走银行家舍入，会与 czsc 分叉）。"""
    assert round_to_2_digit(value) == expected


def test_mean_of_empty_is_zero() -> None:
    """空序列返回 0.0 —— 背驰比较会拿它参与运算，抛异常会让空 key_bis 变成故障。"""
    assert mean([]) == 0.0
    assert mean([1.0, 2.0]) == 1.5


# ── 形状门槛 ─────────────────────────────────────────────────────────


def test_empty_sequence_is_not_first_buy() -> None:
    assert check_first_buy([]) is False
    assert check_first_sell([]) is False


def test_even_count_is_rejected() -> None:
    """笔数必须为奇数（首尾同向、中间两两交替）。"""
    assert check_first_buy(_downtrend_divergent()[:4]) is False
    assert check_first_sell(_uptrend_divergent()[:4]) is False


def test_last_direction_must_point_down_for_first_buy() -> None:
    bis = _downtrend_divergent()
    bis[-1] = _bi(_UP, high=92.0, low=80.0, power_price=9.0, power_volume=300.0, length=5, index=4)
    assert check_first_buy(bis) is False


def test_last_direction_must_point_up_for_first_sell() -> None:
    bis = _uptrend_divergent()
    bis[-1] = _bi(
        _DOWN, high=112.0, low=101.0, power_price=9.0, power_volume=300.0, length=5, index=4
    )
    assert check_first_sell(bis) is False


def test_first_and_last_must_share_direction() -> None:
    """首笔向上、末笔向下 ⇒ 不是一段下跌，直接否掉。"""
    bis = _downtrend_divergent()
    bis[0] = _bi(
        _UP, high=100.0, low=90.0, power_price=10.0, power_volume=1000.0, length=10, index=0
    )
    assert check_first_buy(bis) is False


def test_high_must_be_on_the_first_bi() -> None:
    """下跌途中创出更高点 ⇒ 「整段最高在首笔」不成立。"""
    bis = _downtrend_divergent()
    bis[2] = _bi(
        _DOWN, high=101.0, low=85.0, power_price=12.0, power_volume=1200.0, length=12, index=2
    )
    assert check_first_buy(bis) is False


def test_low_must_be_on_the_last_bi() -> None:
    """末笔没有创新低 ⇒ 不是「不断创出新低」的下跌段。"""
    bis = _downtrend_divergent()
    bis[4] = _bi(_DOWN, high=92.0, low=86.0, power_price=9.0, power_volume=300.0, length=5, index=4)
    assert check_first_buy(bis) is False


# ── 力度度量未填充必须报错 ───────────────────────────────────────────


def test_missing_power_metrics_raise_instead_of_returning_false() -> None:
    """``length=0`` 是「未填充」标记；静默 False 会让背驰结论变成「不背驰」。"""
    bis = _downtrend_divergent()
    bis[-1] = _bi(
        _DOWN, high=92.0, low=80.0, power_price=9.0, power_volume=300.0, length=0, index=4
    )
    with pytest.raises(ValueError, match="力度度量未填充"):
        check_first_buy(bis)


# ── 背驰 ─────────────────────────────────────────────────────────────


def test_divergent_downtrend_is_first_buy() -> None:
    assert check_first_buy(_downtrend_divergent()) is True


def test_divergent_uptrend_is_first_sell() -> None:
    assert check_first_sell(_uptrend_divergent()) is True


def test_equal_price_power_is_not_divergence() -> None:
    """价格力度必须**严格**变小；相等不算背驰（边界不能用 <= 放宽）。"""
    bis = _downtrend_divergent()
    bis[-1] = _bi(
        _DOWN, high=92.0, low=80.0, power_price=12.0, power_volume=300.0, length=5, index=4
    )
    assert check_first_buy(bis) is False


def test_price_alone_is_not_enough() -> None:
    """价格变小但量、长度都不小 ⇒ 不构成背驰。"""
    bis = _downtrend_divergent()
    bis[-1] = _bi(
        _DOWN, high=92.0, low=80.0, power_price=9.0, power_volume=9999.0, length=99, index=4
    )
    assert check_first_buy(bis) is False


def test_first_buy_and_sell_are_mirrors() -> None:
    """同一段行情翻成镜像后，一买为真则一卖为假。"""
    assert check_first_buy(_downtrend_divergent()) is True
    assert check_first_sell(_downtrend_divergent()) is False
    assert check_first_sell(_uptrend_divergent()) is True
    assert check_first_buy(_uptrend_divergent()) is False
