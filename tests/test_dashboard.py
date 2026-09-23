from __future__ import annotations

import json

from cpt.application.dashboard import build_dashboard_snapshot, dashboard_json
from cpt.domain.config import RulesConfig
from cpt.domain.models import make_canonical_bar


def bar(index: int, closed: bool = True):
    return make_canonical_bar(
        open_time=index * 300000,
        open=10.0,
        high=12.0 + index,
        low=8.0 + index,
        close=11.0 + index,
        close_time=index * 300000 + 299999,
        is_closed=closed,
    )


def test_snapshot_contains_stable_market_overlays_and_quality() -> None:
    snapshot = build_dashboard_snapshot(
        RulesConfig(),
        [bar(0), bar(1, closed=False)],
        mode="realtime",
        status="alert",
        data_source="fixture",
        stale=True,
        gap=False,
    )
    assert snapshot["schema_version"] == "dashboard.v1"
    assert snapshot["market"]["symbol"] == "BTCUSDT"
    assert snapshot["market"]["bar_count"] == 2
    assert snapshot["data_quality"] == {
        "stale": True,
        "gap": False,
        "closed_bar_count": 1,
        "unclosed_bar_count": 1,
    }
    assert snapshot["runtime"]["status"] == "alert"
    assert snapshot["candles"][1]["direction"] == 1


def test_dashboard_json_is_deterministic_and_json_compatible() -> None:
    snapshot = build_dashboard_snapshot(RulesConfig(), [bar(0)])
    first = dashboard_json(snapshot)
    second = dashboard_json(snapshot)
    assert first == second
    assert json.loads(first)["schema_version"] == "dashboard.v1"
