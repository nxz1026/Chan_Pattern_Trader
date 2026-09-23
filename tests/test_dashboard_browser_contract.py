from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_browser_contract_contains_accessible_parity_selection_and_readonly_http() -> None:
    html = (ROOT / "dashboard/index.html").read_text(encoding="utf-8")
    js = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    http = (ROOT / "cpt/web/app.py").read_text(encoding="utf-8")
    assert 'data-testid="parity-selection"' in html
    assert 'data-testid="parity-chart-cpt"' in html
    assert 'data-testid="parity-chart-oracle"' in html
    assert "cpt:parity-selected" in js
    assert "data-parity-selected" in js
    assert "def do_POST" in http
    assert "METHOD_NOT_ALLOWED" in http
