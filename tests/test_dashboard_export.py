from __future__ import annotations

from cpt.application.dashboard_export import slice_snapshot


def test_slice_snapshot_is_non_mutating_and_updates_bar_count() -> None:
    source = {"candles": [{"open_time": 1}, {"open_time": 2}, {"open_time": 3}], "market": {"bar_count": 3}}
    result = slice_snapshot(source, 2, 3)
    assert [item["open_time"] for item in result["candles"]] == [2, 3]
    assert result["market"]["bar_count"] == 2
    assert source["market"]["bar_count"] == 3
