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

from cpt.domain.config import RulesConfig
from cpt.domain.models import (
    Bi,
    CanonicalBar,
    Fractal,
    Signal,
    StructureEvent,
    ZhongShu,
)

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


def _bar_to_dict(bar: CanonicalBar) -> dict[str, Any]:
    """``CanonicalBar`` → schema v1 bar 对象。

    ``direction`` 是 ``CanonicalBar`` 的派生属性，不参与 ``asdict``，
    这里显式补上，排在末尾。
    """
    data: dict[str, Any] = asdict(bar)
    data["direction"] = bar.direction
    return data


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
    """把一次计算结果打包为 schema v1 的 ``dict``。"""
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "schema_url": EXPORT_SCHEMA_URL,
        "config": config.to_dict(),
        "metadata": dict(metadata) if metadata is not None else {},
        "data": {
            "bars": [_bar_to_dict(b) for b in bars],
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
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2)


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

    用 ``sort_keys=True, ensure_ascii=False`` 做规范化序列化，因此同一
    组输入（无论序列顺序、元数据差异）产出同一哈希。
    """
    canonical = json.dumps(
        payload["data"], sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
