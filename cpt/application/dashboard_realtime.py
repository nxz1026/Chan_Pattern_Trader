"""Read-only realtime refresh and alert projections."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def realtime_update(
    snapshots: Sequence[dict[str, Any]], *, selected_symbol: str, selected_interval_ms: int
) -> dict[str, Any]:
    """Select the latest upstream snapshot without performing network I/O."""
    candidates = [
        snapshot
        for snapshot in snapshots
        if snapshot.get("market", {}).get("symbol") == selected_symbol
        and snapshot.get("market", {}).get("interval_ms") == selected_interval_ms
    ]
    if not candidates:
        return {"available": False, "reason": "snapshot_unavailable", "symbol": selected_symbol}
    latest = max(
        candidates,
        key=lambda snapshot: snapshot.get("market", {}).get("last_open_time") or 0,
    )
    signal_value = latest.get("signal")
    signal = signal_value if isinstance(signal_value, dict) else {}
    signal_status = signal.get("status")
    return {
        "available": True,
        "symbol": selected_symbol,
        "interval_ms": selected_interval_ms,
        "snapshot": latest,
        "alert": signal_status in {"alert", "candidate"},
    }
