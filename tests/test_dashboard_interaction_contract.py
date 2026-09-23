from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_dashboard_controls_have_visible_feedback_and_snapshot_reload() -> None:
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    html = (ROOT / "dashboard/index.html").read_text(encoding="utf-8")
    css = (ROOT / "dashboard/dashboard.css").read_text(encoding="utf-8")
    assert "function refreshSelectedSnapshot" in javascript
    assert "refreshSelectedSnapshot();" in javascript
    assert 'data-runtime-mode="offline"' in html
    assert 'data-view-mode="research"' in html
    assert "root.dataset.runtimeMode" in javascript
    assert "root.dataset.viewMode" in javascript
    assert 'body[data-runtime-mode="offline"]' in css
    assert "function noteKey" in javascript
