"""Read-only research statistics projections.

**状态：已接线（R22，2026-09-30）**——`GET /api/dashboard/signal-stats?days=&code=`
（`cpt/web/app.py`）的唯一实现，事件来自 `signal_event_store.load_signal_events`；
对应 `docs/dashboard-product-roadmap.md` Phase 6 P2「一买统计」。响应必须自报
`basis: "signal_event_transitions"`：这是**状态跃迁事件**分布，不是「当前若干只票
的状态」。
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
