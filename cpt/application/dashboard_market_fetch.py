"""Read-only adapter-facing market aggregate normalization.

**状态：待接线（pending-wiring，2026-09-25 审核 P0-2）**——生产代码零导入，尚无
调用方；对应 `docs/dashboard-product-roadmap.md` Phase 3 P0「真实 24h 高低点和
成交量，无法提供时明确 unavailable」，故保留。改动前请先读 `docs/pending-wiring.md`。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cpt.application.dashboard_market import normalize_24h


def market_snapshot(
    symbol: str, interval_ms: int, aggregate: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Wrap an upstream aggregate for Dashboard; no network access occurs here."""
    return {
        "symbol": symbol,
        "interval_ms": interval_ms,
        "market_24h": normalize_24h(aggregate),
        "source": "upstream" if aggregate is not None else "unavailable",
    }
