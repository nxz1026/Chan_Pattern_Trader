"""cpt.domain —— **转发层**，判据本体已迁至 Emotion-CPT。

**判据唯一真源**：``emotion_cpt.structure``（``emotion_cpt/src/emotion_cpt/structure/``）。

本包把 17 个子模块**直接别名**到真源模块对象，因此::

    import cpt.domain.market_time as mt
    assert mt is emotion_cpt.structure.market_time      # True

CPT 现有 **117 个** 引用 ``cpt.domain`` 的调用点**一行都不用改**。

为什么用「模块别名」而不是「逐个 re-export 的壳」
------------------------------------------------
壳方案（``from emotion_cpt.structure.X import (a, b, c)``）有三个**实测**缺陷：

1. **monkeypatch 打在壳上，真源不受影响** ⇒ 行为测试假失败
   （``test_seconds_to_close_defaults_to_market_now`` 实测因此红）；
2. **属性访问式引用会缺符号** ⇒ 壳只导出 ``__all__`` ∪ ``from-import`` 名字，
   而 ``ir.SHARES_PER_LOT`` 这种「模块.属性」访问**扫不出来**
   （``test_matches_emotion_core_implementation`` 实测因此 AttributeError）；
3. 壳与真源是**两个不同的模块对象**，模块级状态会分叉。

别名方案让 ``cpt.domain.X`` **就是** ``emotion_cpt.structure.X``，上述三条全部消失。

迁移日期：2026-10-23　｜　迁移脚本：``Emotion-CPT/scripts/migrate_structure.py``
"""

from __future__ import annotations

import sys

from emotion_cpt import structure as _structure

#: 需要别名的子模块清单（与 emotion_cpt.structure 一一对应）
_SUBMODULES = (
    "a_share_rules",
    "bi",
    "config",
    "contain",
    "containment_trace",
    "first_buy",
    "fractal",
    "intraday_rules",
    "levels",
    "market_time",
    "models",
    "recursion",
    "signal",
    "structure_events",
    "trend_type",
    "types",
    "zhongshu",
)

for _name in _SUBMODULES:
    _mod = getattr(_structure, _name, None)
    if _mod is None:  # pragma: no cover - 搬迁不完整时的显式失败
        raise ImportError(
            f"emotion_cpt.structure 缺少子模块 {_name!r} —— 搬迁不完整？"
        )
    sys.modules[f"{__name__}.{_name}"] = _mod

__all__ = _SUBMODULES
