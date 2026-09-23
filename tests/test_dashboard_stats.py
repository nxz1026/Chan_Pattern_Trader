from __future__ import annotations

from cpt.application.dashboard_stats import signal_statistics


def test_signal_statistics_reports_conversion_and_divergence_counts() -> None:
    result = signal_statistics(
        [
            {"status": "confirmed", "divergence_status": "detected"},
            {"status": "invalidated", "divergence_status": "not_detected"},
            {"status": "alert", "divergence_status": "detected"},
        ]
    )
    assert result["total"] == 3
    assert result["invalidated_count"] == 1
    assert result["alert_to_confirmed_rate"] == 1 / 3
    assert result["divergence_counts"]["detected"] == 2
