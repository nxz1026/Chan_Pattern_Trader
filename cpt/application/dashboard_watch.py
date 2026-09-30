"""Read-only watch-mode projection helpers.

**状态：已接线（R22，2026-09-30）**——落到 `snapshot.watch_metrics`
（`dashboard_snapshot_v2.py::_watch_payload`，无 K 线时显式 `available: false`）；
对应 `docs/dashboard-product-roadmap.md` Phase 4 P1「reconnect/stale」。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cpt.domain.models import CanonicalBar


def watch_metrics(bars: Sequence[CanonicalBar]) -> dict[str, Any]:
    """Return truthful window metrics; unavailable 24h values stay explicit."""
    if not bars:
        return {"change_pct": None, "window_high": None, "window_low": None, "window_volume": None}
    first = bars[0]
    last = bars[-1]
    return {
        "change_pct": ((last.close - first.open) / first.open * 100) if first.open else None,
        "window_high": max(bar.high for bar in bars),
        "window_low": min(bar.low for bar in bars),
        "window_volume": sum(bar.volume for bar in bars),
        "last_price": last.close,
        "24h": None,
    }
