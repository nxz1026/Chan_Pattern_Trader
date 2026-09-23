from __future__ import annotations

from dataclasses import replace

import pytest
from cpt.domain.config import RulesConfig
from cpt.domain.models import make_canonical_bar
from cpt.engine.realtime import RealtimeEngine


def bar(index: int, *, closed: bool = True):
    return make_canonical_bar(
        open_time=index * 300000,
        open=10.0,
        high=12.0 + index,
        low=8.0 + index,
        close=10.0 + index,
        close_time=index * 300000 + 299999,
        is_closed=closed,
    )


def test_unclosed_bar_is_alert_and_close_rebuilds() -> None:
    engine = RealtimeEngine(RulesConfig())
    alert = engine.feed(bar(0, closed=False))
    assert alert["mode"] == "realtime"
    assert alert["status"] == "alert"
    closed = engine.feed(bar(0, closed=True))
    assert closed["status"] == "confirmed"
    assert len(engine.snapshot()) == 1


def test_window_is_bounded_and_duplicate_conflict_is_rejected() -> None:
    engine = RealtimeEngine(RulesConfig(), max_window=2)
    engine.feed(bar(0))
    engine.feed(bar(1))
    engine.feed(bar(2))
    assert [item.open_time for item in engine.snapshot()] == [300000, 600000]
    with pytest.raises(ValueError):
        engine.feed(replace(bar(2), close=999.0))


def test_reset_clears_state() -> None:
    engine = RealtimeEngine(RulesConfig())
    engine.feed(bar(0))
    engine.reset()
    assert engine.snapshot() == ()
