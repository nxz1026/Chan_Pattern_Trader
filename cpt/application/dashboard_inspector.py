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
    """
    if not 0 <= bar_index < len(bars):
        raise IndexError(f"bar_index out of range: {bar_index}")
    raw = bars[bar_index]
    merged_bars = merge_contained_bars(bars)
    merged = next((item for item in merged_bars if bar_index in item.source_indices), None)
    merged_ids = set(merged.source_indices) if merged is not None else set()
    matched_fractals = [item for item in fractals if item.bar_index in merged_ids]
    source_tokens = {f"bar:{i}" for i in merged_ids}
    matched_bis = [
        item for item in bis if any(item_id in source_tokens for item_id in item.source_ids)
    ]
    bi_tokens = {item_id for bi in matched_bis for item_id in bi.source_ids}
    matched_zs = [item for item in zhongshus if bi_tokens.intersection(item.bi_ids)]
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
