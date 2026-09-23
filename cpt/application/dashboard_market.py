"""Optional truthful 24-hour market aggregate contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def normalize_24h(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize an upstream 24h aggregate without deriving or fabricating values."""
    if value is None:
        return {"available": False, "reason": "upstream_aggregate_unavailable"}
    required = ("high", "low", "volume", "quote_volume", "price_change_pct")
    if any(key not in value or value[key] is None for key in required):
        return {"available": False, "reason": "upstream_aggregate_incomplete"}
    return {
        "available": True,
        "high": value["high"],
        "low": value["low"],
        "volume": value["volume"],
        "quote_volume": value["quote_volume"],
        "price_change_pct": value["price_change_pct"],
        "close_time": value.get("close_time"),
    }
