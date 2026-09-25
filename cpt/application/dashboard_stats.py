"""Read-only research statistics projections.

**状态：待接线（pending-wiring，2026-09-25 审核 P0-2）**——生产代码零导入，尚无
调用方；对应 `docs/dashboard-product-roadmap.md` Phase 6 P2「一买统计」，故保留。
改动前请先读 `docs/pending-wiring.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def signal_statistics(signals: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Summarize signal status and invalidation reasons without changing inputs."""
    statuses: dict[str, int] = {}
    divergences: dict[str, int] = {}
    for signal in signals:
        status = str(signal.get("status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1
        divergence = str(signal.get("divergence_status", "unknown"))
        divergences[divergence] = divergences.get(divergence, 0) + 1
    total = len(signals)
    return {
        "total": total,
        "status_counts": statuses,
        "divergence_counts": divergences,
        "alert_to_confirmed_rate": statuses.get("confirmed", 0) / total if total else None,
        "invalidated_count": statuses.get("invalidated", 0),
    }
