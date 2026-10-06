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
        # 这个夹具里 bar_index 与 merged_index 同值（没有包含合并），
        # 所以下面断言跨度时可以直接拿 start_time 换算。
        merged_index=index,
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


# --------------------------------------------------------------------------- #
# min_bi_len 跨度门槛（2026-10-06 接线，此前只对 czsc 生效、native 静默丢弃）
# --------------------------------------------------------------------------- #


def _idx(bi: object) -> int:
    """从笔的 start_time 反推端点下标（本夹具 start_time == merged_index * 1000）。"""
    return int(bi.start_time) // 1000  # type: ignore[attr-defined]


def test_no_gate_is_the_v0_behaviour() -> None:
    """``min_bi_len=None`` 必须是 v0 的原样行为 —— 门槛是可选增强，不是默认收紧。"""
    seq = [fx("bottom", 0, 9, 4), fx("top", 1, 14, 7), fx("bottom", 2, 10, 3)]
    assert len(build_bis(seq)) == len(build_bis(seq, min_bi_len=None)) == 2


def test_short_bi_is_merged_not_dropped() -> None:
    """跨度不足时**合并端点**（终点顺延），不是丢掉那一笔。

    丢掉会让笔序列在时间轴上出空洞，后续中枢与背驰的分母就错了 —— 所以这里
    断言的是「笔数不变、只是变长」。
    """
    # 端点 0 / 1 / 2：0→1 跨度 2、1→2 跨度 2，门槛 3 时两笔都不够
    seq = [fx("bottom", 0, 9, 4), fx("top", 1, 14, 7), fx("bottom", 2, 10, 3)]
    ungated = build_bis(seq)
    gated = build_bis(seq, min_bi_len=3)
    # 0→2 跨度 3，够门槛 ⇒ 一笔，且覆盖了原来两笔的全部时间
    assert len(gated) == 1
    assert (_idx(gated[0]), int(gated[0].end_time) // 1000) == (0, 2)
    # 关键：没有出现「中间被挖掉」—— 首尾时间范围与不设门槛时一致
    assert (int(ungated[0].start_time), int(ungated[-1].end_time)) == (
        int(gated[0].start_time),
        int(gated[-1].end_time),
    )


def test_gate_keeps_long_bi_and_only_merges_short_ones() -> None:
    """只有跨度不足的笔被合并，够长的原样保留。"""
    seq = [
        fx("bottom", 0, 9, 4),
        fx("top", 10, 20, 8),  # 0→10 跨度 11，够门槛 5
        fx("bottom", 11, 12, 3),  # 10→11 跨度 2，不够
        fx("top", 30, 25, 9),  # 11→30 跨度 20（吸收 11）
    ]
    gated = build_bis(seq, min_bi_len=5)
    assert len(gated) == 2
    assert (_idx(gated[0]), int(gated[0].end_time) // 1000) == (0, 10)
    # 第二笔从 10 顺延到 30（吸收了不够格的 11），方向仍是向下
    assert (_idx(gated[1]), int(gated[1].end_time) // 1000) == (10, 30)
    assert gated[1].direction == -1


def test_gate_uses_merged_index_not_bar_index() -> None:
    """量纲必须是**去包含后**的下标，不能是 ``bar_index``（原始下标）。

    构造一个两者不同的分型：``bar_index=100``（原始序列里的第 100 根）、
    ``merged_index=1``（去包含后只剩第 1 根）。门槛 3 时：
    - 按 ``merged_index`` 算，1→2 跨度 2 仍不够；
    - 若误用 ``bar_index``，100→101 跨度 2 也是不够 —— 换个跨度更大的例子才看得出。
    这里用 1→5：按 merged_index 跨度 5 够门槛；按 bar_index（100→104）也是 5。
    真正能区分的是「包含合并让原始下标稀疏」的情形：把 bar_index 拉大到跨度 50，
    按原始下标算会**误判为够门槛**，按去包含后算才正确地合并。
    """
    def fx_split(kind: str, raw: int, merged: int, high: float, low: float) -> Fractal:
        return Fractal(
            kind=kind, level=0, bar_index=raw,
            start_time=merged * 1000, end_time=merged * 1000 + 999,
            high=high, low=low, source_ids=(f"merged:{merged}",), merged_index=merged,
        )

    seq = [fx_split("bottom", 100, 0, 9, 4), fx_split("top", 150, 1, 20, 8)]
    # 去包含后 0→1 跨度 2 < 3 ⇒ 合并掉，不产笔
    assert build_bis(seq, min_bi_len=3) == ()
    # 原始下标 100→150 跨度 51，若误用 bar_index 会错误地产出一笔
    assert build_bis(seq, min_bi_len=60) == ()


def test_gate_raises_when_merged_index_unknown() -> None:
    """``merged_index`` 缺失时**直接抛**，绝不拿 ``bar_index`` 静默量错单位。

    量纲错了笔数就会和参照后端对不上，而这种偏差在图表上看起来完全正常 ——
    比报错难查得多。
    """
    def fx_no_merged(kind: str, index: int) -> Fractal:
        return Fractal(
            kind=kind, level=0, bar_index=index,
            start_time=index * 1000, end_time=index * 1000 + 999,
            high=1.0, low=1.0, source_ids=(f"m:{index}",),
        )

    seq = [fx_no_merged("bottom", 0), fx_no_merged("top", 9)]
    with pytest.raises(ValueError, match="merged_index"):
        build_bis(seq, min_bi_len=3)
    # 不设门槛时 merged_index 缺不缺都无所谓（v0 行为）
    assert len(build_bis(seq)) == 1


def test_gate_value_is_validated() -> None:
    with pytest.raises(ValueError, match="min_bi_len"):
        build_bis([], min_bi_len=0)
    with pytest.raises(ValueError, match="min_bi_len"):
        build_bis([], min_bi_len=-3)


def test_gate_of_one_is_a_no_op() -> None:
    """门槛 1 等于不设门槛（跨度恒 >= 1）。"""
    seq = [fx("bottom", 0, 9, 4), fx("top", 1, 14, 7), fx("bottom", 2, 10, 3)]
    assert len(build_bis(seq, min_bi_len=1)) == 2
