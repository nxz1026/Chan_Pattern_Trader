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


def test_overlapping_followup_bi_extends_without_shrinking_zone() -> None:
    """延伸只延长中枢，不收缩 ``zg`` / ``zd``（缠论原文口径）。

    2026-09-24 之前这里断言的是收缩到 ``(9.5, 7.5)``。收缩会把一个中枢切成多个
    窄区间、并产出宽度趋零的畸形中枢；改为不收缩后与 czsc ``get_zs_seq`` 逐项一致
    （区间宽与纳入笔数全对上）。
    """
    result = build_zhongshus(
        [
            bi(0, 1, 12, 6),
            bi(1, -1, 11, 5),
            bi(2, 1, 10, 7),
            bi(3, -1, 9.5, 7.5),
        ]
    )
    assert len(result) == 1
    assert (result[0].high, result[0].low) == (10, 7), "区间必须由建枢前三笔固定"
    assert result[0].end_time == 3999
    assert result[0].bi_ids == ("bi:0", "bi:1", "bi:2", "bi:3")


def test_zone_range_is_immutable_under_long_extension() -> None:
    """延伸几十笔也不改变区间——区间只由建枢前三笔决定。"""
    bis = [bi(0, 1, 12, 6), bi(1, -1, 11, 5), bi(2, 1, 10, 7)]
    for i in range(3, 40):
        bis.append(bi(i, 1 if i % 2 == 1 else -1, 9.9, 7.1))
    result = build_zhongshus(bis)
    assert len(result) == 1
    assert (result[0].high, result[0].low) == (10, 7)
    assert len(result[0].bi_ids) == len(bis)


def test_extension_never_produces_degenerate_zero_width_zone() -> None:
    """收缩口径的典型病症：区间被后续笔逐步压到宽度趋零。

    旧口径下 ``[12,6] [11,5] [10,7]`` 建枢后再来几笔窄幅笔，区间会一路收缩；
    新口径下区间恒为 ``(10, 7)``。
    """
    bis = [bi(0, 1, 12, 6), bi(1, -1, 11, 5), bi(2, 1, 10, 7)]
    for i, (high, low) in enumerate([(10.0, 7.9), (9.8, 7.8), (9.6, 7.7)], start=3):
        bis.append(bi(i, 1 if i % 2 == 1 else -1, high, low))
    result = build_zhongshus(bis)
    assert len(result) == 1
    assert (result[0].high, result[0].low) == (10, 7)
    assert result[0].high - result[0].low == 3.0


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
