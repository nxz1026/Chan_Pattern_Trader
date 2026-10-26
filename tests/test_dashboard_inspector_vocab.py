"""P2-3 回归：看板「结构反向引用」必须认出**生产路径**的 ``source_ids`` 词表。

原实现只认 ``bar:{原始K线下标}``（领域路径 ``detect_fractals`` 的词表），而生产两条
入口（A 股快照 / ``cpt.web.__main__``）都走
``cpt.application.replay.compute_domain_structures``：

* ``map_fractal`` 发 ``b:{原始K线下标}``
* ``map_bi`` 发 ``fx:{起点原始K线下标}`` / ``fx:{终点原始K线下标}``
* ``map_zhongshu`` 发 ``bi:{在笔序列中的下标}``

⇒ 生产路径的 ``referenced_structures.bis`` / ``.zhongshus`` 恒为空列表，看板反向引用
永远点不出任何笔与中枢。本文件把两条路径都钉住：改词表就必须两边一起过。
"""

from __future__ import annotations

import math

from cpt.adapters.native_chanlun import NativeChanlunBackend
from cpt.application.replay import compute_domain_structures  # was reference_pipeline (deleted in R96)
from cpt.application.dashboard_inspector import inspect_bar
from cpt.domain.bi import build_bis
from cpt.domain.config import RulesConfig
from cpt.domain.contain import merge_contained_bars
from cpt.domain.fractal import detect_fractals
from cpt.domain.models import Bi, ZhongShu, make_canonical_bar
from cpt.domain.zhongshu import build_zhongshus

#: 生产路径用的规则口径：A 股侧传 ``min_bi_len_for("1d")``（=6）。
_MIN_BI_LEN_1D = 6


def _wave_bars(count: int = 400, *, interval_ms: int = 60_000) -> tuple:
    """确定性震荡序列：能产出 32 个分型 / 31 条笔 / 1 个中枢（实测）。"""
    return tuple(
        make_canonical_bar(
            open_time=index * interval_ms,
            close_time=index * interval_ms + interval_ms - 1,
            open=100 + 10 * math.sin(index / 4.0),
            high=101 + 10 * math.sin(index / 4.0),
            low=99 + 10 * math.sin(index / 4.0),
            close=100.5 + 10 * math.sin(index / 4.0),
        )
        for index in range(count)
    )


def _production_structures(bars: tuple):
    """生产同款结构：``compute_domain_structures`` + ``NativeChanlunBackend``。"""
    backend = NativeChanlunBackend(min_bi_len=_MIN_BI_LEN_1D)
    return compute_domain_structures(bars, RulesConfig(), backend)


def test_production_path_bis_and_zhongshus_are_referenced() -> None:
    """⚠️ 核心：生产路径下反向引用**非空** —— 原实现这里恒为空。"""
    bars = _wave_bars()
    fractals, bis, zhongshus = _production_structures(bars)
    assert len(bis) > 1 and zhongshus, "夹具本身要产出笔与中枢，否则本测试无意义"

    referenced_bis = 0
    referenced_zs = 0
    for bar_index in range(len(bars)):
        refs = inspect_bar(bars, fractals, bis, zhongshus, bar_index)["referenced_structures"]
        referenced_bis += len(refs["bis"])
        referenced_zs += len(refs["zhongshus"])
    assert referenced_bis > 0, "生产路径的笔一条都反向引用不到（fx: 词表未认）"
    assert referenced_zs > 0, "生产路径的中枢一个都反向引用不到（bi:{下标} 词表未认）"


def test_production_path_bi_is_the_one_starting_at_that_bar() -> None:
    """命中的必须是**起点落在该根**的那条笔，不是碰巧同下标的邻笔。

    用 ``fx:{下标}`` 匹配时最容易出的错是拿**缠论K线下标**（``merged:``）当原始下标，
    那样第 7 根会配到第 6 根起的笔上。这里直接核对笔的两端时间锚。
    """
    bars = _wave_bars()
    fractals, bis, zhongshus = _production_structures(bars)
    start_bar = int(bis[0].source_ids[0].split(":", 1)[1])
    refs = inspect_bar(bars, fractals, bis, zhongshus, start_bar)["referenced_structures"]
    assert refs["bis"], f"bar {start_bar} 上应当命中起于该根的笔"
    assert any(bi["start_time"] == bis[0].start_time for bi in refs["bis"])
    assert all(item["start_time"] <= bars[start_bar].close_time for item in refs["bis"]), (
        "命中的笔不应晚于该根 bar"
    )


def _handmade_structures() -> tuple[tuple, tuple, tuple, tuple]:
    """5 根互不包含的 bar + 4 条生产词表的笔 + 1 个只含下标 2/3 的中枢。

    中枢刻意**不含**下标 0/1：这样「被命中笔在 ``bis`` 里的真实下标」与「它在
    ``matched_bis`` 子序列里的位置」才会分叉，从而钉住实现用的是哪一个。
    """
    bars = tuple(
        make_canonical_bar(
            open_time=index * 60_000,
            close_time=index * 60_000 + 59_999,
            open=10.0 + index,
            high=11.0 + index,
            low=9.0 + index,
            close=10.5 + index,
        )
        for index in range(5)
    )
    bis = tuple(
        Bi(
            level=0,
            direction=1 if index % 2 == 0 else -1,
            start_time=bars[index].open_time,
            end_time=bars[index + 1].close_time,
            high=11.0 + index,
            low=9.0 + index,
            source_ids=(f"fx:{index}", f"fx:{index + 1}"),
        )
        for index in range(4)
    )
    fractals: tuple = ()
    zhongshus = (
        ZhongShu(
            level=0,
            start_time=bis[2].start_time,
            end_time=bis[3].end_time,
            high=12.0,
            low=10.0,
            bi_ids=("bi:2", "bi:3"),
        ),
    )
    return bars, fractals, bis, zhongshus


def test_zhongshu_match_uses_full_sequence_index_not_subsequence() -> None:
    """中枢的 ``bi:{下标}`` 是**全笔序列**下标 —— 子序列位置会错位。

    bar 2 上命中的笔是下标 1 与 2；它们在 ``matched_bis`` 子序列里的位置是 0 与 1。
    只有用**全序列**下标才会与中枢的 ``('bi:2', 'bi:3')`` 相交。
    """
    bars, fractals, bis, zhongshus = _handmade_structures()
    refs = inspect_bar(bars, fractals, bis, zhongshus, 2)["referenced_structures"]
    assert {bi["source_ids"][0] for bi in refs["bis"]} == {"fx:1", "fx:2"}, "先确认命中的笔"
    assert refs["zhongshus"], "bar 2 命中的笔含下标 2，必须引用该中枢（子序列位置法会漏）"

    refs_last = inspect_bar(bars, fractals, bis, zhongshus, 4)["referenced_structures"]
    assert refs_last["zhongshus"], "bar 4 命中的作用下标 3，也必须引用该中枢"

    refs_zero = inspect_bar(bars, fractals, bis, zhongshus, 0)["referenced_structures"]
    assert refs_zero["bis"], "bar 0 应当命中下标 0 的笔"
    assert not refs_zero["zhongshus"], "下标 0 不在中枢成员里，不得引用该中枢"


def test_domain_path_still_references_bis_and_zhongshus() -> None:
    """领域路径（``detect_fractals`` + ``build_bis``）不能被这次修改打瘸。"""
    bars = _wave_bars()
    merged = merge_contained_bars(bars)
    fractals = detect_fractals(merged)
    bis = build_bis(fractals, min_bi_len=_MIN_BI_LEN_1D)
    zhongshus = build_zhongshus(bis)
    assert bis, "领域路径夹具要产出笔"

    referenced = 0
    for bar_index in range(len(bars)):
        refs = inspect_bar(bars, fractals, bis, zhongshus, bar_index)["referenced_structures"]
        referenced += len(refs["bis"])
    assert referenced > 0, "领域路径（bar: 词表）的笔反向引用不能再被打断"
