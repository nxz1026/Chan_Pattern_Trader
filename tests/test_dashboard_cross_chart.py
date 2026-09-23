from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_parity_selection_contract_links_anchor_and_highlight() -> None:
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    css = (ROOT / "dashboard/dashboard.css").read_text(encoding="utf-8")
    assert "cpt:parity-selected" in javascript
    assert "data-parity-anchor" in javascript
    assert "data-open-time" in javascript
    assert "data-parity-anchor" in css
