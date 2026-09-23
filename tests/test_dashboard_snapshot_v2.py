from __future__ import annotations

from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.domain.config import RulesConfig
from cpt.domain.models import make_canonical_bar


def test_snapshot_v2_retains_v1_fields_and_adds_research_contract() -> None:
    bars = tuple(
        make_canonical_bar(
            open_time=index * 300000,
            close_time=index * 300000 + 299999,
            open=10.0 + index,
            high=12.0 + index,
            low=8.0 + index,
            close=11.0 + index,
        )
        for index in range(3)
    )
    snapshot = build_dashboard_snapshot_v2(RulesConfig(), bars)
    assert snapshot["schema_version"] == "dashboard.v2"
    assert snapshot["candles"]
    assert snapshot["reproducibility"]["dataset_hash"]
    assert len(snapshot["indicators"]["macd"]) == len(bars)
    assert snapshot["runs"] == []
