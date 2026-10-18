"""研究模式逐根检查与结构反向引用服务。

该服务是 B3（``cpt.domain.containment_trace.trace_containment``）的生产接线点：
``inspect_bar`` 在前端传入 bar 索引后，从原始 K 线重建包含关系，再调用
``trace_containment`` 产出确定性 containment 决策作为 ``containment_chain``。
任何对单根 K 线的研究面板（前端 R2 检查器）都应当经 HTTP 端点
``/api/dashboard/inspect`` 调用本函数，避免在前端重复实现 containment 决策。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from cpt.domain.contain import merge_contained_bars
from cpt.domain.containment_trace import trace_containment
from cpt.domain.models import Bi, CanonicalBar, Fractal, ZhongShu

#: 结构 ``source_ids`` 有**两套并存**的词表，反向引用必须同时认它们 —— 只认一套时
#: 看板的结构反向引用在另一条路径上恒为空列表（2026-10-18 实测：只认 ``bar:`` 时
#: 生产路径 referenced bis/zhongshus 恒为 0）：
#:
#: * 领域路径（fixture / ``detect_fractals`` + ``build_bis`` 直算）：
#:   ``cpt.domain.fractal._source_ids`` 发 ``merged:{缠论K线下标}`` +
#:   ``bar:{原始K线下标}``，笔的 ``source_ids`` 是两端分型 ``source_ids`` 的拼接
#:   （``cpt.domain.bi._merge_ids``）。
#: * 生产路径（``cpt.adapters.reference_pipeline.compute_domain_structures`` —— A 股
#:   与 ``cpt.web.__main__`` 两个入口都走它）：``map_fractal`` 发 ``b:{原始K线下标}``、
#:   ``map_bi`` 发 ``fx:{起点原始K线下标}`` / ``fx:{终点原始K线下标}``。分型匹配走
#:   ``Fractal.bar_index``，故 ``b:`` 不参与本模块的匹配。
#:
#: 中枢 ``bi_ids`` 的词表同样分两套：生产路径 ``map_zhongshu`` 发
#: ``bi:{在笔序列中的下标}``，领域路径 ``cpt.domain.zhongshu._initial_ids`` 发三笔各自
#: 首个 ``source_id``（真实 id，不是下标）。故中枢匹配要同时试「id 交集」与「下标交集」。
_BAR_PREFIX = "bar:"
_FX_PREFIX = "fx:"
_BI_INDEX_PREFIX = "bi:"


def _bi_index(token: str) -> int | None:
    """``bi:{下标}`` → 下标；其它形状（领域路径的真实 id）返回 ``None``。

    用 ``isascii() and isdigit()`` 而不是裸 ``isdigit()``：``'²'.isdigit()`` 是
    ``True`` 而 ``int('²')`` 抛 ``ValueError``（同 P1-2 的 ``history_days`` 坑）。
    """
    if not token.startswith(_BI_INDEX_PREFIX):
        return None
    suffix = token[len(_BI_INDEX_PREFIX) :]
    if not suffix.isascii() or not suffix.isdigit():
        return None
    return int(suffix)


def inspect_bar(
    bars: Sequence[CanonicalBar],
    fractals: Sequence[Fractal],
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    bar_index: int,
) -> dict[str, Any]:
    """返回指定 raw bar 的稳定 JSON-compatible 检查结果。

    包含关系重建与解释委托给 :func:`merge_contained_bars` +
    :func:`trace_containment`；本函数只做不变量替换与 JSON 序列化。

    反向引用（``referenced_structures``）同时认领域与生产两套 ``source_ids`` 词表，
    判据见模块顶部 ``_BAR_PREFIX`` 一段。
    """
    if not 0 <= bar_index < len(bars):
        raise IndexError(f"bar_index out of range: {bar_index}")
    raw = bars[bar_index]
    merged_bars = merge_contained_bars(bars)
    merged = next((item for item in merged_bars if bar_index in item.source_indices), None)
    merged_ids = set(merged.source_indices) if merged is not None else set()
    matched_fractals = [item for item in fractals if item.bar_index in merged_ids]
    # 生产路径的笔只带 ``fx:{原始K线下标}``；领域路径带 ``bar:{原始K线下标}``
    # （领域分型的 ``source_ids`` 一定把成分原始下标带成 ``bar:``，故 ``merged:``
    # 不必进词表 —— 它是**缠论K线下标**，与这里的原始下标同域不同义，混进来会把
    # 第 7 根的笔误配到第 6 根上）。
    source_tokens = {f"{_BAR_PREFIX}{i}" for i in merged_ids}
    source_tokens |= {f"{_FX_PREFIX}{i}" for i in merged_ids}
    matched_bis = [
        item for item in bis if any(item_id in source_tokens for item_id in item.source_ids)
    ]
    bi_tokens = {item_id for bi in matched_bis for item_id in bi.source_ids}
    # 中枢匹配两套词表：id 交集（领域路径的真实 id）+ 下标交集（生产路径的 bi:{下标}）。
    # ``matched_bis`` 是 ``bis`` 的子序列，但生产路径的 ``bi:{下标}`` 用的是**全序列**
    # 下标，子序列下标对不上 —— 故按 ``bis`` 全序列算被命中的下标集合。
    matched_bi_indices = {
        index
        for index, bi in enumerate(bis)
        if any(item_id in source_tokens for item_id in bi.source_ids)
    }
    matched_zs = [
        item
        for item in zhongshus
        if bi_tokens.intersection(item.bi_ids)
        or matched_bi_indices.intersection(
            index for index in (_bi_index(token) for token in item.bi_ids) if index is not None
        )
    ]
    containment_chain: list[dict[str, Any]] = []
    if merged is not None:
        decisions = trace_containment(tuple(bars), tuple(merged_bars))
        for decision in decisions:
            if decision.source_bar_index not in merged_ids:
                continue
            containment_chain.append(
                {
                    "source_index": decision.source_bar_index,
                    "target_source_indices": list(decision.target_source_indices),
                    "decision": decision.decision,
                    "resulting_high": decision.resulting_high,
                    "resulting_low": decision.resulting_low,
                    "direction": decision.direction,
                }
            )
    return {
        "bar_index": bar_index,
        "raw_bar": {**asdict(raw), "direction": raw.direction},
        "merged_bar": asdict(merged) | {"direction": merged.direction} if merged else None,
        "containment_chain": containment_chain,
        "referenced_structures": {
            "fractals": [asdict(item) for item in matched_fractals],
            "bis": [asdict(item) for item in matched_bis],
            "zhongshus": [asdict(item) for item in matched_zs],
        },
    }
