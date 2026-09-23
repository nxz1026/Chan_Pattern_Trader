"""Optional truthful 24-hour market aggregate contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _pick(value: Mapping[str, Any], *candidates: str) -> Any:
    for candidate in candidates:
        if candidate in value and value[candidate] is not None:
            return value[candidate]
    return None


def _to_float(value: Any) -> float | None:
    """Coerce a possibly-string numeric value into ``float``.

    Binance's ``/api/v3/ticker/24hr`` returns every numeric field as a JSON
    string. The dashboard contract documents prices / volumes / percentages as
    numbers, so we coerce here once instead of forcing every consumer to repeat
    the dance. ``None`` and unparsable values propagate as ``None`` so the
    availability gate keeps working.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def normalize_24h(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize an upstream 24h aggregate without deriving or fabricating values.

    Accepts both the camelCase Binance schema (``highPrice``/``quoteVolume``/…)
    and the snake_case schema v1 keys (``high``/``quote_volume``/…) — whichever
    the upstream actually provided. Binance returns every numeric field as a
    string; we coerce ``high`` / ``low`` / ``volume`` / ``quote_volume`` /
    ``price_change_pct`` into ``float`` so consumers can format them directly.
    Returns ``available: false`` with a structured reason when any of the
    required fields are missing or non-numeric.
    """
    if value is None:
        return {"available": False, "reason": "upstream_aggregate_unavailable"}
    high = _to_float(_pick(value, "high", "highPrice"))
    low = _to_float(_pick(value, "low", "lowPrice"))
    volume = _to_float(_pick(value, "volume"))
    quote_volume = _to_float(_pick(value, "quote_volume", "quoteVolume"))
    price_change_pct = _to_float(
        _pick(value, "price_change_pct", "priceChangePercent", "priceChange")
    )
    close_time_raw = _pick(value, "close_time", "closeTime")
    close_time = _to_float(close_time_raw)
    if any(v is None for v in (high, low, volume, quote_volume, price_change_pct)):
        return {"available": False, "reason": "upstream_aggregate_incomplete"}
    return {
        "available": True,
        "high": high,
        "low": low,
        "volume": volume,
        "quote_volume": quote_volume,
        "price_change_pct": price_change_pct,
        "close_time": close_time,
    }
