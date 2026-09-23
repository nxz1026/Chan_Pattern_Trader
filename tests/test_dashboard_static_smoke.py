from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_dashboard_static_smoke_contract() -> None:
    html = (ROOT / "dashboard/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    css = (ROOT / "dashboard/dashboard.css").read_text(encoding="utf-8")
    assert 'data-testid="dashboard-root"' in html
    assert 'data-testid="mode-switch"' in html
    assert 'data-testid="interval-select"' in html
    assert 'data-testid="level-select"' in html
    assert 'data-testid="chart-canvas-region"' in html
    assert 'data-testid="parity-chart"' in html
    assert "window.CPTDashboard" in javascript
    assert "cpt:mode-changed" in javascript
    assert "cpt:interval-changed" in javascript
    assert "cpt:level-changed" in javascript
    assert ".chart-canvas" in css
