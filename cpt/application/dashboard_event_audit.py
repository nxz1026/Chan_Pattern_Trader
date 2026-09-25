"""Read-only event audit and before/after diff projections.

**状态：待接线（pending-wiring，2026-09-25 审核 P0-2）**——生产代码零导入，尚无
调用方；对应 `docs/dashboard-product-roadmap.md` Phase 5 P1「事件前后状态对比」，
故保留。改动前请先读 `docs/pending-wiring.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cpt.domain.models import StructureEvent


def event_audit(events: Sequence[StructureEvent]) -> tuple[dict[str, Any], ...]:
    """Return events ordered by occurrence with payload field diffs when available."""
    ordered = sorted(
        events, key=lambda event: (event.occurred_at, event.revision, event.structure_id)
    )
    rows: list[dict[str, Any]] = []
    previous_by_structure: dict[str, dict[str, object]] = {}
    for event in ordered:
        previous = previous_by_structure.get(event.structure_id, {})
        changed = tuple(
            key
            for key in sorted(set(previous) | set(event.payload))
            if previous.get(key) != event.payload.get(key)
        )
        rows.append(
            {
                "event_type": event.event_type,
                "structure_id": event.structure_id,
                "revision": event.revision,
                "occurred_at": event.occurred_at,
                "before": previous,
                "after": event.payload,
                "changed_fields": changed,
            }
        )
        previous_by_structure[event.structure_id] = dict(event.payload)
    return tuple(rows)
