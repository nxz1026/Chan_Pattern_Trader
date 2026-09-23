from __future__ import annotations

from cpt.application.dashboard_watch import watch_metrics
from cpt.domain.models import make_canonical_bar


def test_watch_metrics_never_fabricates_24h_values() -> None:
    bars = tuple(
        make_canonical_bar(
            open_time=index * 300000,
            close_time=index * 300000 + 299999,
            open=10.0,
            high=12.0 + index,
            low=8.0 + index,
            close=11.0 + index,
            volume=2.0,
        )
        for index in range(2)
    )
    metrics = watch_metrics(bars)
    assert metrics["change_pct"] == 20.0
    assert metrics["window_high"] == 13.0
    assert metrics["window_volume"] == 4.0
    assert metrics["24h"] is None
