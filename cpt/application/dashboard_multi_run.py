"""Multi-run research projection helpers.

**状态：待接线（pending-wiring，2026-09-25 审核 P0-2）**——生产代码零导入，尚无
调用方；对应 `docs/dashboard-product-roadmap.md` Phase 6 P2「双数据集同步对比」，
故保留。改动前请先读 `docs/pending-wiring.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def align_runs(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Align run candles by open_time for deterministic A/B inspection."""
    points: dict[int, dict[str, Any]] = {}
    for index, run in enumerate(runs):
        for candle in run.get("candles", []):
            open_time = candle.get("open_time")
            if isinstance(open_time, int):
                points.setdefault(open_time, {})[f"run_{index}"] = candle
    return {
        "run_count": len(runs),
        "timestamps": tuple(sorted(points)),
        "points": tuple(
            {"open_time": timestamp, **points[timestamp]} for timestamp in sorted(points)
        ),
    }
