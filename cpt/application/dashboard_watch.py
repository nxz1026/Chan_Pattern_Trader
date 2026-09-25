"""Read-only watch-mode projection helpers.

**状态：待接线（pending-wiring，2026-09-25 审核 P0-2）**——生产代码零导入，尚无
调用方；对应 `docs/dashboard-product-roadmap.md` Phase 4 P1「reconnect/stale」，
故保留。改动前请先读 `docs/pending-wiring.md`。
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
