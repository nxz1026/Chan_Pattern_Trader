"""研究模式逐根检查与结构反向引用服务。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from cpt.domain.contain import MergedBar
from cpt.domain.models import Bi, CanonicalBar, Fractal, ZhongShu


def inspect_bar(
    bars: Sequence[CanonicalBar],
    merged_bars: Sequence[MergedBar],
    fractals: Sequence[Fractal],
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    bar_index: int,
) -> dict[str, Any]:
    """返回指定 raw bar 的稳定 JSON-compatible 检查结果。"""
    if not 0 <= bar_index < len(bars):
        raise IndexError(f"bar_index out of range: {bar_index}")
    raw = bars[bar_index]
    merged = next((item for item in merged_bars if bar_index in item.source_indices), None)
    merged_ids = set(merged.source_indices) if merged is not None else set()
    matched_fractals = [item for item in fractals if item.bar_index in merged_ids]
    source_tokens = {f"bar:{i}" for i in merged_ids}
    matched_bis = [
        item for item in bis if any(item_id in source_tokens for item_id in item.source_ids)
    ]
    bi_tokens = {item_id for bi in matched_bis for item_id in bi.source_ids}
    matched_zs = [item for item in zhongshus if bi_tokens.intersection(item.bi_ids)]
    return {
        "bar_index": bar_index,
        "raw_bar": {**asdict(raw), "direction": raw.direction},
        "merged_bar": asdict(merged) | {"direction": merged.direction} if merged else None,
        "containment_chain": [
            {
                "source_index": index,
                "target_source_indices": list(merged.source_indices),
                "decision": "merged",
            }
            for index in sorted(merged_ids)
        ]
        if merged
        else [],
        "referenced_structures": {
            "fractals": [asdict(item) for item in matched_fractals],
            "bis": [asdict(item) for item in matched_bis],
            "zhongshus": [asdict(item) for item in matched_zs],
        },
    }
