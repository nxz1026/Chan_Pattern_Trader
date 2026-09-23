from __future__ import annotations

from cpt.domain.models import StructureState
from cpt.engine.rebuild import make_rebuild_events, rebuild_tail, scan_stale_dependents


def state(
    identifier: str,
    revision: int,
    status: str = "forming",
    end_time: int = 100,
    source_ids: tuple[str, ...] = (),
) -> StructureState:
    return StructureState(
        id=identifier,
        level=1,
        kind="trend_type",
        direction=1,
        start_time=0,
        end_time=end_time,
        status=status,
        revision=revision,
        first_seen_at=0,
        confirmed_at=None,
        invalidated_at=None,
        source_ids=source_ids,
    )


def test_unconfirmed_tail_is_replaced_with_incremented_revision() -> None:
    result = rebuild_tail([state("s1", 1)], [state("s1", 1, end_time=200)], 300)
    assert len(result) == 1
    assert (result[0].revision, result[0].end_time) == (2, 200)
    assert result[0].status == "forming"


def test_confirmed_state_is_frozen_and_new_forming_revision_is_appended() -> None:
    result = rebuild_tail(
        [state("s1", 2, "confirmed", end_time=100)],
        [state("s1", 2, "confirmed", end_time=200)],
        300,
    )
    assert [(item.revision, item.status, item.end_time) for item in result] == [
        (2, "confirmed", 100),
        (3, "forming", 200),
    ]


def test_rebuild_events_are_stable_and_report_update_or_invalidation() -> None:
    before = [state("s1", 1), state("s2", 1, "confirmed")]
    after = [state("s1", 2, end_time=200), state("s2", 1, "invalidated")]
    events = make_rebuild_events(before, after, 400)
    assert [(event.event_type, event.structure_id, event.revision) for event in events] == [
        ("updated", "s1", 2),
        ("invalidated", "s2", 1),
    ]
    assert events[0].payload["source_ids"] == ()
    assert events[0].occurred_at == 400


def test_stale_dependents_use_explicit_source_revision_convention() -> None:
    stale = state("s2", 1, source_ids=("s1@r1",))
    fresh = state("s3", 1, source_ids=("source:s1@r3",))
    unrelated = state("s4", 1, source_ids=("s1",))
    assert scan_stale_dependents([stale, fresh, unrelated], "s1", 2) == (stale,)
