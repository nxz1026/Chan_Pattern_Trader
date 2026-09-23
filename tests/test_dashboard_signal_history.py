from __future__ import annotations

from cpt.application.dashboard_signal_history import signal_history
from cpt.domain.models import Signal, StructureEvent


def test_signal_history_projects_lifecycle_events() -> None:
    signal = Signal(
        signal_id="s-1",
        level=1,
        signal_type="first_buy",
        status="confirmed",
        structure_id="z-1",
        center_ids=("c-1",),
        divergence_status="detected",
        alert_time=10,
        candidate_time=20,
        confirmed_time=30,
        invalidated_time=None,
        price=100.0,
        source_revision=2,
    )
    event = StructureEvent("confirmed", "z-1", 2, {}, 30)
    result = signal_history((signal,), (event,))
    assert result[0]["status"] == "confirmed"
    assert result[0]["lifecycle"][0]["event_type"] == "confirmed"
