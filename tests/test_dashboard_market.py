from __future__ import annotations

from cpt.application.dashboard_market import normalize_24h


def test_24h_contract_is_explicitly_unavailable_without_upstream_data() -> None:
    assert normalize_24h(None) == {
        "available": False,
        "reason": "upstream_aggregate_unavailable",
    }


def test_24h_contract_accepts_complete_upstream_aggregate() -> None:
    result = normalize_24h(
        {"high": 110, "low": 90, "volume": 12, "quote_volume": 1200, "price_change_pct": 2.5}
    )
    assert result["available"] is True
    assert result["high"] == 110
