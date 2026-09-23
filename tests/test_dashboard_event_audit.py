from __future__ import annotations

from cpt.application.dashboard_event_audit import event_audit
from cpt.domain.models import StructureEvent


def test_event_audit_returns_before_after_changed_fields() -> None:
    events = (
        StructureEvent("created", "bi-1", 1, {"status": "created", "level": 1}, 10),
        StructureEvent("updated", "bi-1", 2, {"status": "confirmed", "level": 1}, 20),
    )
    result = event_audit(events)
    assert result[1]["before"]["status"] == "created"
    assert result[1]["after"]["status"] == "confirmed"
    assert result[1]["changed_fields"] == ("status",)
