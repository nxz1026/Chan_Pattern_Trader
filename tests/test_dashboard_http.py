from __future__ import annotations

import json
import urllib.error
import urllib.request

from tests.conftest import served


def test_dashboard_http_adapter_is_read_only() -> None:
    provider = lambda: {  # noqa: E731
        "schema_version": "dashboard.v2",
        "reproducibility": {"hashes": {}},
        "parity": {"items": []},
        "runs": [],
        "market_24h": {"available": False},
        "engine_state": {"revision": 0},
    }
    with served(provider) as base:
        with urllib.request.urlopen(f"{base}/api/dashboard/snapshot") as response:
            assert json.load(response)["schema_version"] == "dashboard.v2"
        with urllib.request.urlopen(f"{base}/api/dashboard/health") as response:
            assert json.load(response)["read_only"] is True
        for path, key in (
            ("reproducibility", "hashes"),
            ("parity", "items"),
            ("runs", "runs"),
            ("market-24h", "available"),
            ("engine-state", "revision"),
        ):
            with urllib.request.urlopen(f"{base}/api/dashboard/{path}") as response:
                assert key in json.load(response)
        request = urllib.request.Request(f"{base}/api/dashboard/snapshot", method="POST")
        try:
            urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            assert error.code == 405
        else:
            raise AssertionError("POST must be rejected")
