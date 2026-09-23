"""Structured research data-quality projections."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cpt.domain.models import CanonicalBar


def quality_report(bars: Sequence[CanonicalBar], *, stale: bool = False) -> dict[str, Any]:
    """Return ordered gap/stale/ordering issues for research mode."""
    gaps: list[dict[str, int]] = []
    out_of_order: list[dict[str, int]] = []
    for previous, current in zip(bars, bars[1:], strict=False):
        if current.open_time <= previous.open_time:
            out_of_order.append({"previous": previous.open_time, "current": current.open_time})
        interval = current.open_time - previous.open_time
        if interval != previous.close_time - previous.open_time + 1:
            gaps.append({"from": previous.open_time, "to": current.open_time, "delta": interval})
    return {
        "severity": "stale" if stale else "gap" if gaps or out_of_order else "ok",
        "stale": stale,
        "gap_count": len(gaps),
        "out_of_order_count": len(out_of_order),
        "gaps": gaps,
        "out_of_order": out_of_order,
    }
