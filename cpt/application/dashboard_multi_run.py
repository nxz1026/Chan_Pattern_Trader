"""Multi-run research projection helpers.

**状态：已接线（R22，2026-09-30）**——`GET /api/dashboard/multi-run?run_ids=`
（`cpt/web/app.py`）的唯一实现，对应 `docs/archive/plans-and-acceptance.md` Phase 6 P2
「双数据集同步对比」。入参是**本进程已记录的快照本体**（`dashboard_runs.run_body`），
与 `dashboard_compare` 同源。
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
