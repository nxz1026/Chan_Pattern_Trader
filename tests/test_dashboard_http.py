from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from cpt.web.app import serve_snapshot


def test_dashboard_http_adapter_is_read_only() -> None:
    server = serve_snapshot(lambda: {"schema_version": "dashboard.v2"})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base}/api/dashboard/snapshot") as response:
            assert json.load(response)["schema_version"] == "dashboard.v2"
        with urllib.request.urlopen(f"{base}/api/dashboard/health") as response:
            assert json.load(response)["read_only"] is True
        request = urllib.request.Request(f"{base}/api/dashboard/snapshot", method="POST")
        try:
            urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            assert error.code == 405
        else:
            raise AssertionError("POST must be rejected")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
