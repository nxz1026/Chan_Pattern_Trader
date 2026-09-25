"""``CanonicalBar`` → bar 对象（导出 schema v1 与看板 candle 共用的唯一实现）。

.. warning::

   这里产出的就是**已冻结的 schema v1 bar 对象**，不是"一个恰好同形的字典"。
   ``cpt.application.export.export_dataset`` 的 ``data.bars[]`` 直接用它的输出，
   而 ``data`` 子树参与 ``dataset_hash``；改这里的键集合（增 / 删 / 改名 / 改顺序）
   **等于改导出格式**，必须同时：

   1. 升 ``cpt.application.export.EXPORT_SCHEMA_VERSION``；
   2. 同步 ``docs/export-schema-v1.md``；
   3. 更新 ``tests/test_dataset_hashes.py`` 里钉住键集合的那条测试。

   看板（``cpt.application.dashboard``）当前复用本函数，只是为了让 UI 载荷与导出
   载荷**今天**同形 —— 两者**不是**同一个契约。看板若要单独加字段，请回到
   ``dashboard.py`` 里自己实现（或给本函数加显式的 ``extra`` 参数），
   **不要**直接在这里加：那会静默改掉导出格式，且可能不被任何测试拦住。
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from cpt.domain.models import CanonicalBar

__all__ = ["bar_to_dict"]


def bar_to_dict(bar: CanonicalBar) -> dict[str, Any]:
    """``CanonicalBar`` → schema v1 bar 对象。

    ``direction`` 是 ``CanonicalBar`` 的派生属性，不参与 ``asdict``，
    这里显式补上，排在末尾。
    """
    data: dict[str, Any] = asdict(bar)
    data["direction"] = bar.direction
    return data
