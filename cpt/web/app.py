"""Minimal standard-library HTTP adapter for a supplied Dashboard snapshot."""

from __future__ import annotations

import json
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

SnapshotProvider = Callable[[], dict[str, Any]]


def make_handler(provider: SnapshotProvider) -> type[BaseHTTPRequestHandler]:
    """Create a read-only handler bound to a thread-safe snapshot provider.

    The provider must use independent repository/connection state per request when
    backed by SQLite; this adapter does not serialize concurrent calls for it.
    """

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                snapshot = provider()
            except Exception:  # noqa: BLE001
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "snapshot unavailable")
                return
            path = urlsplit(self.path)
            query = parse_qs(path.query)
            if path.path == "/api/dashboard/health":
                payload: dict[str, Any] = {"ok": True, "read_only": True}
            elif path.path == "/api/dashboard/snapshot":
                payload = dict(snapshot)
                market = dict(payload.get("market", {}))
                runtime = dict(payload.get("runtime", {}))
                if query.get("symbol"):
                    market["symbol"] = query["symbol"][0]
                    runtime["symbol"] = query["symbol"][0]
                if query.get("interval_ms"):
                    try:
                        market["interval_ms"] = int(query["interval_ms"][0])
                    except ValueError:
                        self.send_error(HTTPStatus.BAD_REQUEST, "interval_ms must be an integer")
                        return
                payload["market"] = market
                payload["runtime"] = runtime
            elif path.path == "/api/dashboard/reproducibility":
                payload = snapshot.get("reproducibility", {})
            elif path.path == "/api/dashboard/parity":
                payload = snapshot.get("parity", {})
            elif path.path == "/api/dashboard/runs":
                payload = {"runs": snapshot.get("runs", [])}
            elif path.path == "/api/dashboard/market-24h":
                payload = snapshot.get("market_24h", {"available": False, "reason": "unavailable"})
            elif path.path == "/api/dashboard/engine-state":
                payload = snapshot.get("engine_state", {})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                encoded = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError):
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "snapshot is not JSON-safe")
                return
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
