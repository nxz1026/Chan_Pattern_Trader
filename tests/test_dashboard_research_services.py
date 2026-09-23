from __future__ import annotations

from cpt.application.dashboard_parity import build_parity_view
from cpt.application.dashboard_reproducibility import config_hash, snapshot_diff
from cpt.domain.config import RulesConfig


def test_parity_classifies_missing_extra_and_match() -> None:
    result = build_parity_view(
        [{"kind": "top", "bar_index": 1}],
        [{"kind": "top", "bar_index": 1}, {"kind": "bottom", "bar_index": 2}],
        kind="fractal",
    )
    assert result["summary"] == {
        "matched": 1,
        "missing": 1,
        "extra": 0,
        "mismatched": 0,
        "total": 2,
        "match_rate": 0.5,
    }
    assert any(item["status"] == "missing" for item in result["items"])


def test_reproducibility_hash_and_diff_are_stable() -> None:
    config = RulesConfig()
    assert config_hash(config) == config_hash(config)
    assert snapshot_diff({"a": 1}, {"a": 2}) == ({"field": "a", "left": 1, "right": 2},)
