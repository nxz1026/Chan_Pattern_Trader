"""Multi-run research projection helpers."""

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
