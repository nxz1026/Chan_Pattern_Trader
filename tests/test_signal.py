from __future__ import annotations

import pytest
from cpt.domain.models import Signal
from cpt.domain.signal import assess_first_buy, assess_first_sell, transition_first_buy


def ready(**kwargs: object) -> Signal:
    args: dict[str, object] = {
        "level": 1,
        "structure_id": "trend:1",
        "center_ids": ("zs:1", "zs:2"),
        "trend_direction": -1,
        "has_two_centers": True,
        "has_divergence_leg": True,
        "has_reversal_bi": False,
        "event_time": 100,
        "price": 100.0,
    }
    args.update(kwargs)
    return assess_first_buy(**args)


def test_first_buy_structure_ready_does_not_require_detected_divergence() -> None:
    result = ready(divergence_status="not_detected")
    assert result.status == "structure_ready"
    assert result.divergence_status == "not_detected"
    assert result.alert_time is None
    assert result.confirmed_time is None


def test_reversal_confirms_first_buy() -> None:
    result = ready(has_reversal_bi=True, event_time=200, price=101.0)
    assert result.status == "confirmed"
    assert result.confirmed_time == 200
    assert result.price == 101.0


def test_transition_preserves_and_advances_states() -> None:
    initial = ready()
    confirmed = transition_first_buy(
        initial, reversal_closed=True, structure_valid=True, event_time=300
    )
    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_time == 300
    assert (
        transition_first_buy(
            confirmed, reversal_closed=False, structure_valid=True, event_time=400
        ).status
        == "confirmed"
    )
    invalid = transition_first_buy(
        confirmed, reversal_closed=False, structure_valid=False, event_time=500
    )
    assert invalid.status == "invalidated"
    assert invalid.invalidated_time == 500


def test_alert_candidate_confirmation_flow() -> None:
    alert = Signal(
        signal_id="first_buy:trend:1",
        level=1,
        signal_type="first_buy",
        status="alert",
        structure_id="trend:1",
        center_ids=("zs:1", "zs:2"),
        divergence_status="not_checked",
        alert_time=100,
        candidate_time=None,
        confirmed_time=None,
        invalidated_time=None,
        price=100.0,
        source_revision=2,
    )
    candidate = transition_first_buy(
        alert, reversal_closed=False, structure_valid=True, event_time=200
    )
    assert candidate.status == "candidate"
    assert candidate.candidate_time == 200
    confirmed = transition_first_buy(
        candidate, reversal_closed=True, structure_valid=True, event_time=300
    )
    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_time == 300
    assert confirmed.source_revision == 2


def test_validation_and_invalidated_state() -> None:
    with pytest.raises(ValueError, match="trend_direction"):
        ready(trend_direction=0)
    with pytest.raises(ValueError, match="divergence_status"):
        ready(divergence_status="unknown")
    invalid = ready(has_two_centers=False)
    assert invalid.status == "invalidated"
    assert (
        transition_first_buy(
            invalid, reversal_closed=True, structure_valid=True, event_time=200
        ).status
        == "invalidated"
    )


# --------------------------------------------------------------------------- #
# 一卖（R21 镜像）
# --------------------------------------------------------------------------- #


def sell_ready(**kwargs: object) -> Signal:
    args: dict[str, object] = {
        "level": 1,
        "structure_id": "trend:1",
        "center_ids": ("zs:1", "zs:2"),
        "trend_direction": 1,
        "has_two_centers": True,
        "has_divergence_leg": True,
        "has_reversal_bi": False,
        "event_time": 100,
        "price": 100.0,
    }
    args.update(kwargs)
    return assess_first_sell(**args)


def test_first_sell_structure_ready_on_upward_trend() -> None:
    """向上趋势 + 两个中枢 + 背驰 → ``structure_ready``。"""
    result = sell_ready(divergence_status="not_detected")
    assert result.status == "structure_ready"
    assert result.signal_type == "first_sell"
    assert result.signal_id == "first_sell:1:trend:1"


def test_first_sell_reversal_confirms() -> None:
    """反向笔出现 → ``confirmed``。"""
    result = sell_ready(has_reversal_bi=True, event_time=200, price=101.0)
    assert result.status == "confirmed"
    assert result.confirmed_time == 200


def test_first_sell_invalidated_without_two_centers() -> None:
    """只有一个中枢 → ``invalidated``。"""
    result = sell_ready(has_two_centers=False)
    assert result.status == "invalidated"


def test_first_sell_invalidated_on_downward_trend() -> None:
    """向下趋势 → 一卖无意义，``invalidated``。"""
    result = sell_ready(trend_direction=-1)
    assert result.status == "invalidated"


def test_first_sell_rejects_invalid_direction() -> None:
    with pytest.raises(ValueError, match="trend_direction"):
        sell_ready(trend_direction=0)
