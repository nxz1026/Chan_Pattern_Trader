from __future__ import annotations

from cpt.application.dashboard_reproducibility import config_hash, snapshot_diff
from cpt.domain.config import RulesConfig


def test_reproducibility_hash_and_diff_are_stable() -> None:
    config = RulesConfig()
    assert config_hash(config) == config_hash(config)
    assert snapshot_diff({"a": 1}, {"a": 2}) == ({"field": "a", "left": 1, "right": 2},)
