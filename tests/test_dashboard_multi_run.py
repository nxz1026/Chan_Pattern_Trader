from __future__ import annotations

from cpt.application.dashboard_multi_run import align_runs


def test_align_runs_by_open_time() -> None:
    result = align_runs(
        [
            {"candles": [{"open_time": 2, "close": 20}, {"open_time": 3, "close": 30}]},
            {"candles": [{"open_time": 3, "close": 31}]},
        ]
    )
    assert result["timestamps"] == (2, 3)
    assert result["points"][1]["run_1"]["close"] == 31
