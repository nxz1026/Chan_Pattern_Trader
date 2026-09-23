"""Read-only level recursion projection for research mode."""

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
