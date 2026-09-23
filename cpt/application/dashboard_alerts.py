"""Read-only alert transition projections for Dashboard refreshes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def alert_transition(
    previous: Mapping[str, Any] | None, current: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe a signal status transition without sending notifications."""
    previous_signal = previous.get("signal") if previous else None
    current_signal = current.get("signal")
    previous_status = previous_signal.get("status") if isinstance(previous_signal, dict) else "none"
    current_status = current_signal.get("status") if isinstance(current_signal, dict) else "none"
    triggered = current_status in {"alert", "candidate"} and current_status != previous_status
    return {
        "changed": current_status != previous_status,
        "previous_status": previous_status,
        "current_status": current_status,
        "triggered": triggered,
        "reason": "signal_status_changed" if triggered else None,
    }
