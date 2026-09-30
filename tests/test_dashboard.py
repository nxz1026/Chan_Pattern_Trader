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
    # C7（D 类接线）起 data_quality 多了质量明细键；既有 4 键语义不变，故这里断言
    # 「逐键相等」而不是整字典全等——全等会在每次扩字段时假红。
    quality = snapshot["data_quality"]
    assert quality["stale"] is True
    assert quality["gap"] is False
    assert quality["closed_bar_count"] == 1
    assert quality["unclosed_bar_count"] == 1
    assert quality["severity"] == "stale"  # stale=True 优先于 gap 判定
    assert quality["gap_count"] == 0
    assert quality["out_of_order_count"] == 0
    assert quality["gaps"] == []
    assert quality["out_of_order"] == []
    assert snapshot["runtime"]["status"] == "alert"
    assert snapshot["candles"][1]["direction"] == 1


def test_dashboard_json_is_deterministic_and_json_compatible() -> None:
    snapshot = build_dashboard_snapshot(RulesConfig(), [bar(0)])
    first = dashboard_json(snapshot)
    second = dashboard_json(snapshot)
    assert first == second
    assert json.loads(first)["schema_version"] == "dashboard.v1"
