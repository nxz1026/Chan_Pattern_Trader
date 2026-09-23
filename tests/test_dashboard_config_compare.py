from __future__ import annotations

from cpt.application.dashboard_config_compare import compare_configs
from cpt.domain.config import RulesConfig


def test_compare_configs_returns_field_level_differences() -> None:
    left = RulesConfig()
    right = RulesConfig(macd_fast=left.macd_fast + 1)
    result = compare_configs(left, right)
    assert any(item["field"] == "macd_fast" for item in result)
