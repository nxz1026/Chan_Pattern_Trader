from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_parity_navigation_contract_has_cross_chart_target_fields() -> None:
    js = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    assert "cpt:parity-selected" in js
    assert "data-parity-selected" in js
    assert "start_time" in js
    assert "bar_index" in js
    assert "JSON.stringify(ref)" in js
