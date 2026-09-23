from __future__ import annotations

from cpt.application.dashboard_runtime import runtime_panel


def test_runtime_panel_has_stable_engine_state_fields() -> None:
    result = runtime_panel({"revision": 4, "pending_count": 2, "buffer_size": 8, "truncated": True})
    assert result["revision"] == 4
    assert result["pending_count"] == 2
    assert result["buffer_size"] == 8
    assert result["truncated"] is True
    assert result["status"] == "unknown"
