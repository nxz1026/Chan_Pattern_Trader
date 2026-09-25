"""JSON 导出（schema v1 冻结）。

把一次完整的缠论计算结果（原始 K 线 + 分型/笔/中枢 + 结构事件 + 信号）
序列化为 schema v1 的 ``dict``，并可落盘为 JSON 文件。哈希只针对
``payload["data"]`` 子树，保证同一输入产出同一哈希。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cpt.application._bar_dict import bar_to_dict
from cpt.domain.config import RulesConfig
from cpt.domain.models import (
    Bi,
    CanonicalBar,
    Fractal,
    Signal,
    StructureEvent,
    ZhongShu,
)
from cpt.domain.types import PLACEHOLDER_TIME

__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "EXPORT_SCHEMA_URL",
    "export_dataset",
    "export_to_json_string",
    "export_to_file",
    "dataset_hash",
]

#: 导出 schema 版本号，写入每个 JSON 导出的顶层字段。
EXPORT_SCHEMA_VERSION: str = "v1"

#: 导出 schema 文档 URL。（TODO: 待 M6 写完整文档）
EXPORT_SCHEMA_URL: str = (
    "https://github.com/nxz1026/Chan_Pattern_Trader/blob/main/docs/export-schema-v1.md"
)

#: 占位时间戳哨兵:``cpt.adapters.reference_chanlun.PLACEHOLDER_TIME``。
#: 任何结构对象的 ``start_time`` / ``end_time`` 等于此值时, ``export_dataset``
#: 拒绝导出,防止占位语义污染已冻结的 schema v1。
_EXPORT_PLACEHOLDER_TIME: int = PLACEHOLDER_TIME


def _reject_placeholders(
    name: str,
    items: Sequence[object],
    fields: tuple[str, ...] = ("start_time", "end_time"),
) -> None:
    """检查序列中任何对象的指定字段是否含 PLACEHOLDER_TIME, 有则 raise。"""
    for idx, obj in enumerate(items):
        for field in fields:
            if getattr(obj, field, None) == _EXPORT_PLACEHOLDER_TIME:
                raise ValueError(
                    f"{name}[{idx}].{field} 是占位时间戳 ({PLACEHOLDER_TIME}); "
                    f"schema v1 不接受占位语义。请在反腐层传入 bars 参数解析。"
                )


def export_dataset(
    *,
    config: RulesConfig,
    bars: Sequence[CanonicalBar],
    fractals: Sequence[Fractal],
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    events: Sequence[StructureEvent],
    signals: Sequence[Signal],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把一次计算结果打包为 schema v1 的 ``dict``。

    拒绝任何 ``start_time`` / ``end_time`` 等于占位哨兵 (-1) 的结构对象,
    防止占位语义污染已冻结格式。调用方应在反腐层 ``map_*`` 时传入 bars。
    """
    _reject_placeholders("fractals", fractals)
    _reject_placeholders("bis", bis)
    _reject_placeholders("zhongshus", zhongshus)
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "schema_url": EXPORT_SCHEMA_URL,
        "config": config.to_dict(),
        "metadata": dict(metadata) if metadata is not None else {},
        "data": {
            "bars": [bar_to_dict(b) for b in bars],
            "fractals": [asdict(f) for f in fractals],
            "bis": [asdict(b) for b in bis],
            "zhongshus": [asdict(z) for z in zhongshus],
            "events": [asdict(e) for e in events],
            "signals": [asdict(s) for s in signals],
        },
    }


def export_to_json_string(
    *,
    config: RulesConfig,
    bars: Sequence[CanonicalBar],
    fractals: Sequence[Fractal],
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    events: Sequence[StructureEvent],
    signals: Sequence[Signal],
    metadata: dict[str, Any] | None = None,
) -> str:
    """序列化为 JSON 字符串（键排序、不转义非 ASCII、2 空格缩进）。"""
    payload = export_dataset(
        config=config,
        bars=bars,
        fractals=fractals,
        bis=bis,
        zhongshus=zhongshus,
        events=events,
        signals=signals,
        metadata=metadata,
    )
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)


def export_to_file(
    path: str | Path,
    *,
    config: RulesConfig,
    bars: Sequence[CanonicalBar],
    fractals: Sequence[Fractal],
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    events: Sequence[StructureEvent],
    signals: Sequence[Signal],
    metadata: dict[str, Any] | None = None,
) -> None:
    """把结果写成 UTF-8 JSON 文件。"""
    content = export_to_json_string(
        config=config,
        bars=bars,
        fractals=fractals,
        bis=bis,
        zhongshus=zhongshus,
        events=events,
        signals=signals,
        metadata=metadata,
    )
    Path(path).write_text(content, encoding="utf-8")


def dataset_hash(payload: dict[str, Any]) -> str:
    """对 ``payload["data"]`` 子树计算稳定 sha256。

    用 ``sort_keys=True, ensure_ascii=False, separators=(",", ":")`` 做规范化
    序列化——字典键按字母序, 数组顺序保留, 空白一致。

    **序列顺序参与哈希**：bars/fractals/bis/zhongshus/events/signals 等
    列表的元素顺序变化会产生不同哈希。调用方须保证输入序列稳定,
    若想"忽略顺序"应在调用前对序列排序并固定排序键。
    """
    canonical = json.dumps(
        payload["data"],
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
