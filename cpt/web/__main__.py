"""Local Dashboard HTTP service entry point.

The demo mode is deliberately deterministic and read-only.  Real market mode is
not enabled until a complete application provider is wired to the engine.
"""

from __future__ import annotations

import argparse
from typing import Any

from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.domain.config import RulesConfig
from cpt.web.app import serve_snapshot


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
    return snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CPT read-only Dashboard API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--mode", choices=("demo", "realtime"), default="demo")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="5m")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "realtime":
        raise SystemExit(
            "realtime mode is not enabled: wire a thread-safe engine snapshot provider first"
        )
    snapshot = demo_snapshot(args.symbol, args.interval)
    server = serve_snapshot(lambda: snapshot, host=args.host, port=args.port)
    print(f"CPT Dashboard API listening on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
