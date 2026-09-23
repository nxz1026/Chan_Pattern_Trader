from __future__ import annotations

from cpt.application.dashboard_runs import build_run_index


def test_run_index_is_stable_and_contains_reproducibility_fields() -> None:
    rows = build_run_index(
        [
            {
                "market": {"symbol": "BTCUSDT", "interval_ms": 300000, "bar_count": 2},
                "candles": [{}, {}],
                "runtime": {"data_source": "fixture", "status": "confirmed"},
                "reproducibility": {"dataset_hash": "abc", "config_hash": "cfg", "generated_at": 2},
            }
        ]
    )
    assert rows[0]["run_id"] == "abc"
    assert rows[0]["bar_count"] == 2
    assert rows[0]["config_hash"] == "cfg"
