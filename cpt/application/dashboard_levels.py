"""Read-only level recursion projection for research mode.

**状态：已接线（R22，2026-09-30）**——落到 `snapshot.level_tree`
（`dashboard_snapshot_v2.py::_level_tree_payload`，三条活路径都汇聚到 v2），
另经 `GET /api/dashboard/levels`（`cpt/web/app.py`）直接暴露；对应
`docs/archive/plans-and-acceptance.md` Phase 5 P1「级别递归树」。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def level_tree(structures: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Group structures by level and expose deterministic parent time ranges."""
    levels: dict[int, list[dict[str, Any]]] = {}
    for structure in structures:
        level = structure.get("level")
        if isinstance(level, int):
            levels.setdefault(level, []).append(structure)
    rows: list[dict[str, Any]] = []
    ordered_levels = sorted(levels)
    for index, level in enumerate(ordered_levels):
        parent_level = ordered_levels[index + 1] if index + 1 < len(ordered_levels) else None
        rows.append(
            {
                "level": level,
                "parent_level": parent_level,
                "elements": tuple(
                    sorted(
                        levels[level],
                        key=lambda item: (
                            item.get("start_time", item.get("open_time", 0)),
                            str(item.get("id", item.get("structure_id", ""))),
                        ),
                    )
                ),
            }
        )
    return tuple(rows)
