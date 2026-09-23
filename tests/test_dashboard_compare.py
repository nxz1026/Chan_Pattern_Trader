from __future__ import annotations

from cpt.application.dashboard_compare import compare_snapshots


def test_compare_snapshots_reports_field_differences() -> None:
    result = compare_snapshots(
        {"runtime": {"run_id": "a"}, "value": 1},
        {"runtime": {"run_id": "b"}, "value": 2},
    )
    assert result["left_run_id"] == "a"
    assert result["right_run_id"] == "b"
    assert result["differences"][0]["field"] == "runtime"
