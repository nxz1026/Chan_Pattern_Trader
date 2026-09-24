"""一买/一卖结构谓词的测试（R14-4）。

分三层：

1. **人工构造案例** —— 逐条覆盖 `check_first_buy` / `check_first_sell` 的每个
   门槛，正例反例都有；
2. **工具函数** —— `round_to_2_digit` / `mean` 的边界；
3. **与 czsc 交叉验证** —— 用 czsc 自己的 `cxt_first_buy_V221126` /
   `cxt_first_sell_V221126` 信号模板（内部就是调 Rust 的 `check_first_buy`）
   在 3 个真实 fixture 上逐 n 比对，这是本移植最强的正确性证据。
"""

from __future__ import annotations

import csv
import pathlib
from datetime import UTC, datetime

import pytest
from cpt.adapters.czsc_chanlun import _bi_direction
from cpt.domain.first_buy import (
    check_first_buy,
    check_first_sell,
    mean,
    round_to_2_digit,
)
from cpt.domain.models import Bi

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "oracle"

#: czsc 信号模板尝试的笔数序列（降序，见 cxt.rs 的 cxt_first_buy_v221126）。
_NS = [21, 19, 17, 15, 13, 11, 9, 7, 5]


def bi(
    direction: int,
    high: float,
    low: float,
    *,
    power_volume: float = 1000.0,
    length: int = 10,
) -> Bi:
    """构造一条笔；``power_price`` 按 ``round_to_2_digit(high - low)`` 自动算。"""
    return Bi(
        level=0,
        direction=direction,
        start_time=0,
        end_time=1,
        high=high,
        low=low,
        source_ids=("bi:0",),
        power_price=round_to_2_digit(high - low),
        power_volume=power_volume,
        length=length,
    )


def _first_buy_case() -> list[Bi]:
    """标准一买：5 笔、末笔向下、逐级创新低、末笔力度三指标全面衰竭。"""
    return [
        bi(-1, 100.0, 80.0, power_volume=1000.0, length=10),  # pp=20
        bi(1, 95.0, 80.0, power_volume=800.0, length=8),  # pp=15
        bi(-1, 95.0, 70.0, power_volume=1000.0, length=10),  # pp=25
        bi(1, 85.0, 70.0, power_volume=800.0, length=8),  # pp=15
        bi(-1, 85.0, 65.0, power_volume=400.0, length=6),  # pp=20 < max(25, 22.5)
    ]


def _first_sell_case() -> list[Bi]:
    """标准一卖：``_first_buy_case`` 的镜像。"""
    return [
        bi(1, 100.0, 80.0, power_volume=1000.0, length=10),  # pp=20
        bi(-1, 100.0, 85.0, power_volume=800.0, length=8),  # pp=15
        bi(1, 110.0, 85.0, power_volume=1000.0, length=10),  # pp=25
        bi(-1, 110.0, 95.0, power_volume=800.0, length=8),  # pp=15
        bi(1, 115.0, 95.0, power_volume=400.0, length=6),  # pp=20 < max(25, 22.5)
    ]


def test_handcrafted_first_buy_and_sell_are_detected() -> None:
    assert check_first_buy(_first_buy_case()) is True
    assert check_first_sell(_first_sell_case()) is True


def test_first_buy_and_sell_do_not_accept_the_mirror() -> None:
    """一买谓词不接受一卖形态，反之亦然。"""
    assert check_first_buy(_first_sell_case()) is False
    assert check_first_sell(_first_buy_case()) is False


def test_empty_and_even_length_sequences_are_rejected() -> None:
    assert check_first_buy([]) is False
    assert check_first_sell([]) is False
    # 偶数笔：首尾必然反向，不构成一买/一卖
    assert check_first_buy(_first_buy_case()[:4]) is False
    assert check_first_sell(_first_sell_case()[:4]) is False


def test_wrong_last_direction_is_rejected() -> None:
    """一买要求末笔向下；把末笔改成向上即失败。"""
    case = _first_buy_case()
    case[-1] = bi(1, 85.0, 65.0, power_volume=400.0, length=6)
    assert check_first_buy(case) is False


def test_sequence_that_does_not_make_new_low_is_rejected() -> None:
    """末笔低点不是整段最低点（没创新低）→ 不是一买。"""
    case = _first_buy_case()
    case[-1] = bi(-1, 85.0, 75.0, power_volume=400.0, length=6)  # low=75 > bi2.low=70
    assert check_first_buy(case) is False


def test_sequence_whose_high_is_not_the_first_bi_is_rejected() -> None:
    """整段最高点不在首笔 → 不是一买。"""
    case = _first_buy_case()
    case[2] = bi(-1, 105.0, 70.0, power_volume=1000.0, length=10)  # high=105 > bi0.high=100
    assert check_first_buy(case) is False


def test_no_price_divergence_is_rejected() -> None:
    """末笔价差力度没有变小 → 不背驰。"""
    case = _first_buy_case()
    case[-1] = bi(-1, 95.0, 65.0, power_volume=400.0, length=6)  # pp=30 > max(25, 22.5)
    assert check_first_buy(case) is False


def test_price_divergence_without_volume_or_length_shrink_is_rejected() -> None:
    """价差变小但量与长度都没变小 → 不背驰（``and`` 的第二半）。"""
    case = _first_buy_case()
    case[-1] = bi(-1, 85.0, 65.0, power_volume=2000.0, length=20)  # pp=20 合格，但量、长都更大
    assert check_first_buy(case) is False


def test_unpopulated_power_metrics_raise_instead_of_silently_failing() -> None:
    """``length == 0`` 是"未填充"标记，必须报错而不是静默返回 ``False``。

    否则背驰比较会拿 0 参与运算，得出"不背驰"的错误结论。
    """
    case = _first_buy_case()
    case[-1] = Bi(
        level=0,
        direction=-1,
        start_time=0,
        end_time=1,
        high=85.0,
        low=65.0,
        source_ids=("bi:0",),
    )
    with pytest.raises(ValueError, match="力度度量未填充"):
        check_first_buy(case)

    # 一卖同理：末笔方向必须正确，否则会先被方向门槛拦掉、测不到度量校验
    sell_case = _first_sell_case()
    sell_case[-1] = Bi(
        level=0,
        direction=1,
        start_time=0,
        end_time=1,
        high=115.0,
        low=95.0,
        source_ids=("bi:0",),
    )
    with pytest.raises(ValueError, match="力度度量未填充"):
        check_first_sell(sell_case)


def test_round_to_2_digit_rounds_half_away_from_zero() -> None:
    """对齐 Rust ``f64::round``（半数远离零），而不是 Python 的银行家舍入。

    ``0.125`` / ``0.625`` 是二进制精确值，乘 100 后正好落在 ``.5``，能真正区分
    两种舍入：Python 的 ``round`` 会得到 ``0.12`` / ``0.62``（向偶数），
    Rust 的 ``f64::round`` 得到 ``0.13`` / ``0.63``（远离零）。
    """
    assert round_to_2_digit(0.125) == 0.13  # Python round(12.5) == 12
    assert round_to_2_digit(0.625) == 0.63  # Python round(62.5) == 62
    assert round_to_2_digit(-0.125) == -0.13
    assert round_to_2_digit(267.89999999999998) == 267.9
    assert round_to_2_digit(0.0) == 0.0


def test_round_to_2_digit_matches_rust_on_inexact_decimals() -> None:
    """十进制不精确值的表现必须与 Rust 一致（不是"看起来更对"）。

    ``1.005`` 在 f64 里是 ``1.00499999999999989...``，乘 100 得
    ``100.49999999999999``，Rust 的 ``.round()`` 也给 ``100`` → ``1.0``。
    若这里断言 ``1.01``，就等于自造了一个与 czsc 不一致的口径。
    ``2.675`` 反过来略大于 ``2.675``，乘 100 得 ``267.50000000000006``，
    所以进位到 ``2.68``——两个方向都跟随 f64 实际值，而不是十进制直觉。
    """
    assert round_to_2_digit(1.005) == 1.0
    assert round_to_2_digit(2.675) == 2.68


def test_mean_of_empty_is_zero() -> None:
    """对齐 czsc ``utils::math::mean``：空序列返回 0。"""
    assert mean([]) == 0.0
    assert mean([1.0, 2.0, 3.0]) == 2.0


# --------------------------------------------------------------------------- #
# 与 czsc 交叉验证
# --------------------------------------------------------------------------- #


def _analyzer(path: pathlib.Path) -> object:
    """按与适配器相同的口径构造 czsc 分析器。"""
    from czsc._native import CZSC, Freq, RawBar  # noqa: PLC0415

    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    raw = [
        RawBar(
            symbol="CPT",
            id=i,
            dt=datetime.fromtimestamp(int(r["open_time"]) / 1000, tz=UTC).replace(tzinfo=None),
            freq=Freq.F5,
            open=float(r["open"]),
            close=float(r["close"]),
            high=float(r["high"]),
            low=float(r["low"]),
            vol=float(r["volume"]),
            amount=float(r["quote_volume"]),
        )
        for i, r in enumerate(rows)
    ]
    return CZSC(raw, max_bi_num=0, min_bi_len=6)


def _to_cpt_bis(czsc_bis: object) -> list[Bi]:
    """czsc ``BI`` → CPT ``Bi``（含力度度量）。"""
    return [
        Bi(
            level=0,
            direction=_bi_direction(b.direction),
            start_time=0,
            end_time=1,
            high=float(b.high),
            low=float(b.low),
            source_ids=(f"bi:{i}",),
            power_price=float(b.power_price),
            power_volume=float(b.power_volume),
            length=int(b.length),
        )
        for i, b in enumerate(czsc_bis)
    ]


def _czsc_hit_n(analyzer: object, kind: str) -> int | None:
    """czsc 信号模板命中的笔数（``None`` = 未命中）。"""
    from czsc._native import call_signal  # noqa: PLC0415

    out = call_signal(f"cxt_first_{kind}_V221126", analyzer, {"di": 1})
    text = str(out[0]) if out else ""
    return next((n for n in _NS if f"{n}笔" in text), None)


def _our_hit_n(bis: list[Bi], predicate: object) -> int | None:
    """本实现在同一 n 序列上命中的笔数（``None`` = 未命中）。"""
    return next((n for n in _NS if len(bis) >= n and predicate(bis[-n:])), None)


@pytest.mark.parametrize("kind", ["buy", "sell"])
@pytest.mark.parametrize(
    "fixture",
    [
        "btcusdt_5m_2024-02-01.csv",
        "btcusdt_5m_2024-09-01.csv",
        "btcusdt_5m_2025-04-01.csv",
    ],
)
def test_matches_czsc_signal_template(fixture: str, kind: str) -> None:
    """逐 n 比对 czsc 的 Rust ``check_first_buy`` / ``check_first_sell``。

    czsc 的 ``cxt_first_{buy,sell}_V221126`` 模板按 n 降序 [21,19,…,5] 逐段调用
    Rust 的 ``check_first_buy`` 并返回首个命中的 n。这里复现同一循环，要求命中
    的 n 完全一致。fixture3 的 ``sell`` 会命中 21 笔，因此正例也被覆盖。
    """
    pytest.importorskip("czsc")
    analyzer = _analyzer(FIXTURE_DIR / fixture)
    bis = _to_cpt_bis(analyzer.bi_list)
    predicate = check_first_buy if kind == "buy" else check_first_sell
    assert _our_hit_n(bis, predicate) == _czsc_hit_n(analyzer, kind)


def test_cross_validation_covers_a_positive_hit() -> None:
    """确认交叉验证里确实有一个**正例**，而不是两边都空对空。"""
    pytest.importorskip("czsc")
    analyzer = _analyzer(FIXTURE_DIR / "btcusdt_5m_2025-04-01.csv")
    assert _czsc_hit_n(analyzer, "sell") == 21
    bis = _to_cpt_bis(analyzer.bi_list)
    assert check_first_sell(bis[-21:]) is True
