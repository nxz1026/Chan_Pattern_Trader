"""Optional truthful 24-hour market aggregate contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _pick(value: Mapping[str, Any], *candidates: str) -> Any:
    for candidate in candidates:
        if candidate in value and value[candidate] is not None:
            return value[candidate]
    return None


def normalize_24h(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize an upstream 24h aggregate without deriving or fabricating values.

    Accepts both the camelCase Binance schema (``highPrice``/``quoteVolume``/…)
    and the snake_case schema v1 keys (``high``/``quote_volume``/…) — whichever
    the upstream actually provided. Returns ``available: false`` with a
    structured reason when any of the required fields are missing.
    """
    if value is None:
        return {"available": False, "reason": "upstream_aggregate_unavailable"}
    high = _pick(value, "high", "highPrice")
    low = _pick(value, "low", "lowPrice")
    volume = _pick(value, "volume")
    quote_volume = _pick(value, "quote_volume", "quoteVolume")
    price_change_pct = _pick(value, "price_change_pct", "priceChangePercent", "priceChange")
    if any(v is None for v in (high, low, volume, quote_volume, price_change_pct)):
        return {"available": False, "reason": "upstream_aggregate_incomplete"}
    return {
        "available": True,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": quote_volume,
        "price_change_pct": price_change_pct,
        "close_time": _pick(value, "close_time", "closeTime"),
    }
