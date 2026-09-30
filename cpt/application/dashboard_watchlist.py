"""Read-only multi-symbol watchlist and alert projections.

**状态：已接线（R22，2026-09-30）**——`GET /api/dashboard/watchlist`
（`cpt/web/app.py::_watchlist_payload`）的唯一实现，入参投影自 A 股候选池
（`cpt/web/a_share_routes.py::pool_payload`）；对应
`docs/dashboard-product-roadmap.md` Phase 4 P1「多交易对」。
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
