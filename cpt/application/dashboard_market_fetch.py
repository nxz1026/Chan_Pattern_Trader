"""Read-only adapter-facing market aggregate normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cpt.application.dashboard_market import normalize_24h


def market_snapshot(
    symbol: str, interval_ms: int, aggregate: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Wrap an upstream aggregate for Dashboard; no network access occurs here."""
    return {
        "symbol": symbol,
        "interval_ms": interval_ms,
        "market_24h": normalize_24h(aggregate),
        "source": "upstream" if aggregate is not None else "unavailable",
    }
