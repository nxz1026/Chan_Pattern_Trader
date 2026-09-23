"""Local Dashboard HTTP service entry point.

Supports three modes:

* ``demo`` — empty schema-complete snapshot, no network or engine access.
* ``fixture`` — runs a bundled CPT replay fixture through the native backend
  and serves the resulting snapshot.  Useful for local UI smoke without any
  upstream connectivity.
* ``realtime`` — wires ``BinanceFuturesClient`` + the native backend, polls
  the upstream REST endpoints, and serves a fresh snapshot every interval.
  Defaults to a thread-unsafe single client; the ``provider`` is rebuilt per
  HTTP request from independent state, see ``cpt.web.app``.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import threading
from collections.abc import Mapping
from typing import Any

from cpt.adapters.binance_futures import BinanceFuturesClient, resolve_interval_ms
from cpt.adapters.native_chanlun import NativeChanlunBackend
from cpt.application.dashboard_market import normalize_24h
from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.application.replay import replay_bars
from cpt.domain.config import RulesConfig
from cpt.web.app import serve_snapshot

_LOG = logging.getLogger("cpt.web")


def demo_snapshot(symbol: str, interval: str) -> dict[str, Any]:
    """Build an empty but schema-complete offline snapshot for local deployment."""
    config = RulesConfig()
    snapshot = build_dashboard_snapshot_v2(
        config,
        (),
        mode="watch",
        status="empty",
        data_source="fixture",
        market_24h={"available": False, "reason": "demo_mode_no_upstream"},
        runtime={
            "data_source": "fixture",
            "symbol": symbol,
            "interval": interval,
            "status": "empty",
        },
    )
    snapshot["market"]["symbol"] = symbol
    snapshot["market"]["interval"] = interval
    snapshot["runtime"]["symbol"] = symbol
    snapshot["runtime"]["interval"] = interval
    return snapshot


def fixture_snapshot(symbol: str, interval: str, *, limit: int = 600) -> dict[str, Any]:
    """Run a synthetic 1m fixture through the native backend and project a snapshot."""
    config = RulesConfig()
    backend = NativeChanlunBackend()
    bars = tuple(
        _synthetic_bar(
            index=index,
            interval_ms=resolve_interval_ms(interval),
            symbol=symbol,
        )
        for index in range(limit)
    )
    try:
        payload = replay_bars(bars, config=config, backend=backend)
    except Exception:  # noqa: BLE001 — never let fixture mode crash the server
        return demo_snapshot(symbol, interval)
    snapshot = build_dashboard_snapshot_v2(
        config,
        payload["data"]["bars"],
        fractals=payload["data"].get("fractals", ()),
        bis=payload["data"].get("bis", ()),
        zhongshus=payload["data"].get("zhongshus", ()),
        mode="watch",
        status="confirmed",
        data_source="native_fixture",
        market_24h={"available": False, "reason": "fixture_mode_no_upstream"},
        runtime={"data_source": "native_fixture", "symbol": symbol, "interval": interval},
    )
    snapshot["market"]["symbol"] = symbol
    snapshot["market"]["interval"] = resolve_interval_ms(interval)
    snapshot["runtime"]["symbol"] = symbol
    snapshot["runtime"]["interval"] = interval
    snapshot["reproducibility"] = payload.get("metadata", {})
    return snapshot


def _synthetic_bar(*, index: int, interval_ms: int, symbol: str) -> Any:
    """Deterministic offline bar used for ``fixture`` mode UI smoke."""
    from cpt.domain.models import make_canonical_bar

    open_time = 1_700_000_000_000 + index * interval_ms
    wave = 100 + 10 * math.sin(index / 6.0)
    return make_canonical_bar(
        open_time=open_time,
        open=wave,
        high=wave + 1.5,
        low=wave - 1.5,
        close=wave + (0.5 if index % 2 else -0.5),
        close_time=open_time + interval_ms - 1,
        volume=1.0,
        quote_volume=wave,
        trade_count=10,
        is_closed=True,
    )


class _RealtimeProvider:
    """Thread-safe poll-driven snapshot builder fed by Binance REST."""

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
        poll_seconds: float,
    ) -> None:
        self._symbol = symbol
        self._interval = interval
        self._limit = limit
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = demo_snapshot(symbol, interval)
        self._stop = threading.Event()
        self._client = BinanceFuturesClient()
        self._config = RulesConfig()
        self._backend = NativeChanlunBackend()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cpt-realtime")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot_payload(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            try:
                fresh = self._poll_once()
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("realtime poll failed: %s", exc)
                continue
            with self._lock:
                self._snapshot = fresh

    def _poll_once(self) -> dict[str, Any]:
        try:
            bars = self._client.fetch_validated_klines(
                self._symbol, self._interval, limit=self._limit
            )
        except Exception:
            return demo_snapshot(self._symbol, self._interval)
        try:
            payload = replay_bars(bars, config=self._config, backend=self._backend)
        except Exception:
            return demo_snapshot(self._symbol, self._interval)
        market_24h = self._safe_24h()
        snapshot = build_dashboard_snapshot_v2(
            self._config,
            payload["data"]["bars"],
            fractals=payload["data"].get("fractals", ()),
            bis=payload["data"].get("bis", ()),
            zhongshus=payload["data"].get("zhongshus", ()),
            mode="watch",
            status="confirmed",
            data_source="binance_realtime",
            market_24h=market_24h,
            runtime={
                "data_source": "binance_realtime",
                "symbol": self._symbol,
                "interval": self._interval,
            },
        )
        snapshot["market"]["symbol"] = self._symbol
        snapshot["market"]["interval_ms"] = resolve_interval_ms(self._interval)
        snapshot["runtime"]["symbol"] = self._symbol
        snapshot["runtime"]["interval"] = self._interval
        snapshot["reproducibility"] = payload.get("metadata", {})
        return snapshot

    def _safe_24h(self) -> dict[str, Any]:
        try:
            ticker = self._client.fetch_24h_ticker(self._symbol)
        except Exception:  # noqa: BLE001 — keep API alive on transient upstream errors
            return {"available": False, "reason": "upstream_ticker_unavailable"}
        return normalize_24h(ticker)


def realtime_snapshot(
    symbol: str, interval: str, *, limit: int = 600, poll_seconds: float = 30.0
) -> _RealtimeProvider:
    return _RealtimeProvider(
        symbol=symbol, interval=interval, limit=limit, poll_seconds=poll_seconds
    )


def _query_value(query: Mapping[str, str], key: str, fallback: str) -> str:
    return query.get(key, fallback) or fallback


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CPT read-only Dashboard API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--mode",
        choices=("demo", "fixture", "realtime"),
        default="demo",
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="5m")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=30.0,
        help="Polling interval for realtime mode.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=600,
        help="K-line limit per upstream request (realtime mode).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.mode == "demo":
        snapshot = demo_snapshot(args.symbol, args.interval)

        def provider_callable() -> dict[str, Any]:
            return snapshot

    elif args.mode == "fixture":
        snapshot = fixture_snapshot(args.symbol, args.interval, limit=args.limit)

        def provider_callable() -> dict[str, Any]:
            return snapshot

    else:
        provider = realtime_snapshot(
            args.symbol,
            args.interval,
            limit=args.limit,
            poll_seconds=args.poll_seconds,
        )
        provider_callable = provider.snapshot_payload
    server = serve_snapshot(provider_callable, host=args.host, port=args.port)
    print(
        f"CPT Dashboard API listening on http://{args.host}:{server.server_port} "
        f"(mode={args.mode})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
