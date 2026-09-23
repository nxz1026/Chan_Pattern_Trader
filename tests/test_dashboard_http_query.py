from __future__ import annotations

import json
import threading
import urllib.request

from cpt.web.app import serve_snapshot


def test_snapshot_query_projects_symbol_and_interval() -> None:
    server = serve_snapshot(
        lambda: {
            "schema_version": "dashboard.v2",
            "market": {"symbol": "BTCUSDT", "interval_ms": 300000},
            "runtime": {"symbol": "BTCUSDT"},
        }
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/api/dashboard/snapshot"
            "?symbol=ETHUSDT&interval_ms=3600000"
        ) as response:
            payload = json.load(response)
        assert payload["market"] == {"symbol": "ETHUSDT", "interval_ms": 3600000}
        assert payload["runtime"]["symbol"] == "ETHUSDT"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
