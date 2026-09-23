"""Minimal standard-library HTTP adapter for a supplied Dashboard snapshot."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from cpt.adapters.binance_futures import resolve_interval_label

_LOG = logging.getLogger("cpt.web.handler")

SnapshotProvider = Callable[[], dict[str, Any]]


class SnapshotSource(Protocol):
    """Read-only provider exposing a ``snapshot_payload`` method."""

    def snapshot_payload(self) -> dict[str, Any]: ...


@runtime_checkable
class MultiLevelSource(Protocol):
    """Read-only provider that can rebuild a snapshot for a given level."""

    def snapshot_for_level(self, level: int) -> dict[str, Any]: ...


@runtime_checkable
class InspectProvider(Protocol):
    """Read-only per-bar inspection contract for ``/api/dashboard/inspect``."""

    def inspect(self, bar_index: int) -> dict[str, Any]: ...


@runtime_checkable
class RangeSource(Protocol):
    """Read-only provider that can rebuild a snapshot for an explicit time window."""

    def snapshot_for_range(self, start_ms: int, end_ms: int) -> dict[str, Any]: ...


@runtime_checkable
class SelectableSource(Protocol):
    """Provider that supports hot-reloading the live symbol/interval.

    Implemented by ``_RealtimeProvider`` so the HTTP layer can route
    ``?symbol=`` / ``?interval_ms=`` requests to the live data stream
    rather than the snapshot's cached market.symbol field only.
    """

    def select_symbol(self, symbol: str, interval: str) -> None: ...

    def force_refresh(self) -> None: ...


def make_handler(
    provider: SnapshotProvider | SnapshotSource,
) -> type[BaseHTTPRequestHandler]:
    """Create a read-only handler bound to a thread-safe snapshot provider.

    The provider must use independent repository/connection state per request when
    backed by SQLite; this adapter does not serialize concurrent calls for it.

    If ``provider`` additionally exposes ``inspect(bar_index)``, the
    ``/api/dashboard/inspect`` route is enabled for B3 per-bar inspection.
    """

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                if callable(provider) and not hasattr(provider, "snapshot_payload"):
                    snapshot = provider()
                else:
                    snapshot = provider.snapshot_payload()
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
                # 当 provider 支持 hot-reload（realtime 模式）时，把 ?symbol= / ?interval_ms=
                # 透传给底层 provider，让下一次响应已经是新交易对的数据，
                # 而不是只改 payload 字段、K 线仍为旧交易对。
                # demo / fixture 模式 provider 没有 select_symbol，按下面 fallback 走。
                provider_switched = False
                provider_error: str | None = None
                if query.get("symbol") and isinstance(provider, SelectableSource):
                    requested_symbol = query["symbol"][0]
                    requested_interval = runtime.get("interval") or "1h"
                    if query.get("interval_ms"):
                        try:
                            interval_ms = int(query["interval_ms"][0])
                        except ValueError:
                            self.send_error(HTTPStatus.BAD_REQUEST, "interval_ms must be int")
                            return
                        requested_interval = resolve_interval_label(interval_ms)
                    try:
                        provider.select_symbol(requested_symbol, requested_interval)
                        provider.force_refresh()
                        provider_switched = True
                    except Exception as exc:  # noqa: BLE001
                        provider_error = f"symbol_switch_failed:{exc}"
                        _LOG.warning("provider.select_symbol failed: %s", exc)
                    else:
                        # 强制刷新后重读 snapshot（已经是新交易对）
                        snapshot = provider.snapshot_payload()
                        payload = dict(snapshot)
                        market = dict(payload.get("market", {}))
                        runtime = dict(payload.get("runtime", {}))
                if not provider_switched:
                    if query.get("symbol"):
                        market["symbol"] = query["symbol"][0]
                        runtime["symbol"] = query["symbol"][0]
                    if query.get("interval_ms"):
                        try:
                            market["interval_ms"] = int(query["interval_ms"][0])
                        except ValueError:
                            self.send_error(HTTPStatus.BAD_REQUEST, "interval_ms must be int")
                            return
                payload["market"] = market
                payload["runtime"] = runtime
                if provider_error:
                    payload["provider_warnings"] = [provider_error]
                range_applied = False
                if query.get("start_ms") or query.get("end_ms"):
                    raw_start = (query.get("start_ms") or [""])[0]
                    raw_end = (query.get("end_ms") or [""])[0]
                    try:
                        start_ms = int(raw_start)
                        end_ms = int(raw_end)
                    except ValueError:
                        self.send_error(HTTPStatus.BAD_REQUEST, "start_ms/end_ms must be integers")
                        return
                    if start_ms >= end_ms:
                        self.send_error(
                            HTTPStatus.BAD_REQUEST, "start_ms must be earlier than end_ms"
                        )
                        return
                    if not isinstance(provider, RangeSource):
                        payload = {"available": False, "reason": "range_unavailable_in_mode"}
                        return self._write_json(payload)
                    try:
                        payload = provider.snapshot_for_range(start_ms, end_ms)
                    except Exception:  # noqa: BLE001
                        self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "range rebuild failed")
                        return
                    payload = dict(payload)
                    payload["market"] = dict(payload.get("market", {}))
                    payload["runtime"] = dict(payload.get("runtime", {}))
                    range_applied = True
                if (
                    query.get("level")
                    and not range_applied
                    and isinstance(provider, MultiLevelSource)
                ):
                    try:
                        level = int(query["level"][0])
                    except ValueError:
                        self.send_error(HTTPStatus.BAD_REQUEST, "level must be an integer")
                        return
                    try:
                        payload = provider.snapshot_for_level(level)
                    except Exception:  # noqa: BLE001
                        self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "level rebuild failed")
                        return
                    payload = dict(payload)
                    payload["market"] = dict(payload.get("market", {}))
                    payload["runtime"] = dict(payload.get("runtime", {}))
                    payload["market"]["symbol"] = query.get(
                        "symbol", [payload["market"].get("symbol", "")]
                    )[0] or payload["market"].get("symbol")
                    payload["runtime"]["symbol"] = payload["runtime"]["symbol"]
                    if query.get("interval_ms"):
                        payload["market"]["interval_ms"] = int(query["interval_ms"][0])
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
            elif path.path == "/api/dashboard/inspect":
                bar_index_raw = (query.get("bar_index") or [""])[0]
                try:
                    bar_index = int(bar_index_raw)
                except ValueError:
                    self.send_error(HTTPStatus.BAD_REQUEST, "bar_index must be an integer")
                    return
                if not isinstance(provider, InspectProvider):
                    payload = {"available": False, "reason": "inspect_unavailable_in_mode"}
                    return self._write_json(payload)
                try:
                    payload = provider.inspect(bar_index)
                except IndexError as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                except Exception:  # noqa: BLE001
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "inspect failed")
                    return
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._write_json(payload)

        def _write_json(self, payload: dict[str, Any]) -> None:
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
    provider: SnapshotProvider | SnapshotSource,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """Build a server; caller owns lifecycle and must call ``server_close``."""
    return ThreadingHTTPServer((host, port), make_handler(provider))
