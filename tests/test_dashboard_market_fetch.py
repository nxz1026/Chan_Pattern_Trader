from __future__ import annotations

from cpt.application.dashboard_market_fetch import market_snapshot


def test_market_snapshot_wraps_upstream_24h_aggregate() -> None:
    result = market_snapshot(
        "BTCUSDT",
        300000,
        {"high": 110, "low": 90, "volume": 5, "quote_volume": 500, "price_change_pct": 1.2},
    )
    assert result["symbol"] == "BTCUSDT"
    assert result["market_24h"]["available"] is True
    assert result["source"] == "upstream"
