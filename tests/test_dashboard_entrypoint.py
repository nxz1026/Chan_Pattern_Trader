from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request


def test_demo_entrypoint_serves_schema_snapshot() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "cpt.web", "--host", "127.0.0.1", "--port", "0"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        deadline = time.monotonic() + 5
        line = ""
        while not line and time.monotonic() < deadline:
            line = process.stdout.readline().strip()
        port = int(line.rsplit(":", 1)[1])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dashboard/snapshot") as response:
            payload = json.load(response)
        assert payload["schema_version"] == "dashboard.v2"
        assert payload["runtime"]["data_source"] == "fixture"
        assert payload["market_24h"]["available"] is False
    finally:
        process.terminate()
        process.wait(timeout=5)
