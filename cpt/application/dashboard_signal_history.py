"""Read-only signal history and state-transition projections.

**状态：待接线（pending-wiring，2026-09-25 审核 P0-2）**——生产代码零导入，尚无
调用方；对应 `docs/dashboard-product-roadmap.md` Phase 4 P1「信号历史列表」与
Phase 6 P2「alert→confirmed 转化率」「invalidated 原因分布」，故保留。
改动前请先读 `docs/pending-wiring.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cpt.domain.models import Signal, StructureEvent


def signal_history(
    signals: Sequence[Signal], events: Sequence[StructureEvent] = ()
) -> tuple[dict[str, Any], ...]:
    """Return deterministic signal rows with related lifecycle events."""
    rows: list[dict[str, Any]] = []
    for signal in signals:
        lifecycle = [
            {
                "event_type": event.event_type,
                "revision": event.revision,
                "occurred_at": event.occurred_at,
            }
            for event in events
            if event.structure_id == signal.structure_id
        ]
        rows.append(
            {
                "signal_id": signal.signal_id,
                "structure_id": signal.structure_id,
                "level": signal.level,
                "status": signal.status,
                "divergence_status": signal.divergence_status,
                "price": signal.price,
                "alert_time": signal.alert_time,
                "candidate_time": signal.candidate_time,
                "confirmed_time": signal.confirmed_time,
                "invalidated_time": signal.invalidated_time,
                "lifecycle": tuple(lifecycle),
            }
        )
    return tuple(sorted(rows, key=lambda row: (row["alert_time"] or 0, row["signal_id"])))
