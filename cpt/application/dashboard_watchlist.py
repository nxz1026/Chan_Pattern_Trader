"""Read-only multi-symbol watchlist and alert projections.

**状态：待接线（pending-wiring，2026-09-25 审核 P0-2）**——生产代码零导入，尚无
调用方；对应 `docs/dashboard-product-roadmap.md` Phase 4 P1「多交易对」，故保留。
改动前请先读 `docs/pending-wiring.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def watchlist_rows(markets: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Sort supplied market snapshots and expose explicit alert state."""
    rows = []
    for market in markets:
        rows.append(
            {
                "symbol": market.get("symbol"),
                "last_price": market.get("last_price"),
                "change_pct": market.get("change_pct"),
                "signal_status": market.get("signal_status", "none"),
                "alert": market.get("signal_status") in {"alert", "candidate"},
                "available": market.get("available", True),
            }
        )
    return tuple(sorted(rows, key=lambda row: str(row["symbol"])))
