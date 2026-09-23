from __future__ import annotations

from cpt.application.dashboard_watchlist import watchlist_rows


def test_watchlist_rows_sorts_symbols_and_marks_alerts() -> None:
    result = watchlist_rows(
        [
            {"symbol": "ETHUSDT", "signal_status": "alert"},
            {"symbol": "BTCUSDT", "signal_status": "confirmed"},
        ]
    )
    assert [row["symbol"] for row in result] == ["BTCUSDT", "ETHUSDT"]
    assert result[1]["alert"] is True
