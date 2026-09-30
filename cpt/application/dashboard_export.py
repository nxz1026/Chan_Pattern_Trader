"""Read-only range slicing for research exports.

**状态：已接线（R22，2026-09-30）**——`GET /api/dashboard/export?start_ms=&end_ms=`
（`cpt/web/app.py`）的唯一实现，对应 `docs/dashboard-product-roadmap.md` Phase 6 P2
「时间范围切片导出」。
"""

from __future__ import annotations

from typing import Any


def slice_snapshot(snapshot: dict[str, Any], start_time: int, end_time: int) -> dict[str, Any]:
    """Return a deterministic time-range slice without mutating the source."""
    if start_time > end_time:
        raise ValueError("start_time must be <= end_time")
    result = dict(snapshot)
    candles = snapshot.get("candles", [])
    result["candles"] = [
        candle for candle in candles if start_time <= candle.get("open_time", -1) <= end_time
    ]
    result["slice"] = {"start_time": start_time, "end_time": end_time}
    result["market"] = dict(snapshot.get("market", {}))
    result["market"]["bar_count"] = len(result["candles"])
    return result
