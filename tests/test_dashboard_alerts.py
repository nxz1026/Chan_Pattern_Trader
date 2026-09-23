from __future__ import annotations

from cpt.application.dashboard_alerts import alert_transition


def test_alert_transition_marks_new_alert_without_sending_notification() -> None:
    result = alert_transition(
        {"signal": {"status": "confirmed"}},
        {"signal": {"status": "alert"}},
    )
    assert result == {
        "changed": True,
        "previous_status": "confirmed",
        "current_status": "alert",
        "triggered": True,
        "reason": "signal_status_changed",
    }
