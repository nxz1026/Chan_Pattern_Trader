"""Dashboard v2 composition service for research and watch views."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from cpt.application.dashboard import build_dashboard_snapshot
from cpt.application.dashboard_indicators import macd_series
from cpt.application.dashboard_reproducibility import reproducibility_metadata
from cpt.domain.config import RulesConfig
from cpt.domain.models import Bi, CanonicalBar, Fractal, Signal, StructureEvent, TrendType, ZhongShu

SCHEMA_VERSION = "dashboard.v2"


def build_dashboard_snapshot_v2(
    config: RulesConfig,
    bars: Sequence[CanonicalBar],
    *,
    fractals: Sequence[Fractal] = (),
    bis: Sequence[Bi] = (),
    zhongshus: Sequence[ZhongShu] = (),
    trend_types: Sequence[TrendType] = (),
    signal: Signal | None = None,
    events: Sequence[StructureEvent] = (),
    mode: str = "research",
    status: str = "confirmed",
    data_source: str = "fixture",
    parity: dict[str, Any] | None = None,
    market_24h: dict[str, Any] | None = None,
    runtime: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Compose v2 while retaining all stable v1 fields."""
    v1 = build_dashboard_snapshot(
        config,
        bars,
        fractals,
        bis,
        zhongshus,
        trend_types,
        signal,
        events,
        mode,
        status,
        data_source,
        runtime=runtime,
    )
    v2: dict[str, Any] = dict(v1)
    v2["schema_version"] = SCHEMA_VERSION
    v2["indicators"] = {"macd": list(macd_series(bars, config))}
    v2["market_24h"] = market_24h or {
        "available": False,
        "reason": "upstream_aggregate_unavailable",
    }
    v2["reproducibility"] = reproducibility_metadata(v1, config=config)
    v2["parity"] = parity or {"fractals": {}, "bis": {}, "zhongshus": {}}
    v2["runs"] = []
    runtime_v1 = v1["runtime"] if isinstance(v1["runtime"], dict) else {}
    v2["runtime"] = {**runtime_v1, "mode": mode, "status": status, "data_source": data_source}
    v2["summary"] = {
        "structure_counts": {
            "fractals": len(fractals),
            "bis": len(bis),
            "zhongshus": len(zhongshus),
            "trend_types": len(trend_types),
        },
        "signal": asdict(signal) if signal else None,
    }
    return v2
