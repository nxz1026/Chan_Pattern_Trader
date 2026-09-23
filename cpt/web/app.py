"""Minimal standard-library HTTP adapter for a supplied Dashboard snapshot."""

from __future__ import annotations

import json
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SnapshotProvider = Callable[[], dict[str, Any]]


def make_handler(provider: SnapshotProvider) -> type[BaseHTTPRequestHandler]:
    """Create a read-only handler bound to an application snapshot provider."""

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            snapshot = provider()
            if self.path == "/api/dashboard/health":
                payload: dict[str, Any] = {"ok": True, "read_only": True}
            elif self.path == "/api/dashboard/snapshot":
                payload = snapshot
            elif self.path == "/api/dashboard/reproducibility":
                payload = snapshot.get("reproducibility", {})
            elif self.path == "/api/dashboard/parity":
                payload = snapshot.get("parity", {})
            elif self.path == "/api/dashboard/runs":
                payload = {"runs": snapshot.get("runs", [])}
            elif self.path == "/api/dashboard/market-24h":
                payload = snapshot.get("market_24h", {"available": False, "reason": "unavailable"})
            elif self.path == "/api/dashboard/engine-state":
                payload = snapshot.get("engine_state", {})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:  # noqa: N802
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

        def log_message(self, format: str, *args: object) -> None:
            return

    return DashboardHandler


def serve_snapshot(
    provider: SnapshotProvider, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    """Build a server; caller owns lifecycle and must call ``server_close``."""
    return ThreadingHTTPServer((host, port), make_handler(provider))
