from __future__ import annotations

import json
import urllib.request

from tests.conftest import served


def test_snapshot_query_projects_symbol_and_interval() -> None:
    provider = lambda: {  # noqa: E731
        "schema_version": "dashboard.v2",
        "market": {"symbol": "BTCUSDT", "interval_ms": 300000},
        "runtime": {"symbol": "BTCUSDT"},
    }
    with served(provider) as base:
        with urllib.request.urlopen(
            f"{base}/api/dashboard/snapshot?symbol=ETHUSDT&interval_ms=3600000"
        ) as response:
            payload = json.load(response)
        assert payload["market"] == {"symbol": "ETHUSDT", "interval_ms": 3600000}
        assert payload["runtime"]["symbol"] == "ETHUSDT"
