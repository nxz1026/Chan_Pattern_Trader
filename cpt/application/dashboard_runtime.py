"""Read-only engine runtime projection for research mode."""

from __future__ import annotations

from typing import Any


def runtime_panel(runtime: dict[str, Any] | None) -> dict[str, Any]:
    """Expose stable engine state fields without leaking engine objects."""
    value = runtime or {}
    return {
        "revision": value.get("revision"),
        "pending_count": value.get("pending_count", 0),
        "buffer_size": value.get("buffer_size", 0),
        "window_size": value.get("window_size"),
        "truncated": bool(value.get("truncated", False)),
        "truncation_reason": value.get("truncation_reason"),
        "status": value.get("status", "unknown"),
    }
