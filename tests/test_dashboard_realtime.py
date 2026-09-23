from __future__ import annotations

from cpt.application.dashboard_realtime import realtime_update


def test_realtime_update_selects_latest_matching_snapshot_and_alert() -> None:
    result = realtime_update(
        [
            {
                "market": {"symbol": "BTCUSDT", "interval_ms": 300000, "last_open_time": 1},
                "signal": {"status": "confirmed"},
            },
            {
                "market": {"symbol": "BTCUSDT", "interval_ms": 300000, "last_open_time": 2},
                "signal": {"status": "alert"},
            },
        ],
        selected_symbol="BTCUSDT",
        selected_interval_ms=300000,
    )
    assert result["available"] is True
    assert result["snapshot"]["market"]["last_open_time"] == 2
    assert result["alert"] is True
