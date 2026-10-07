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


def test_structure_ready_stays_put_without_a_closed_reversal() -> None:
    """⚠️ 这条钉住的是「``alert``/``candidate`` 在生产路径上不可达」这个事实。

    ``_advance`` 的分支里只有 ``previous.status == alert`` 才产出 ``candidate``，
    而 ``assess_first_buy`` **从不**产出 ``alert`` —— 于是
    ``structure_ready`` 在没有收盘确认的反向笔时只能原地不动，
    生产路径上 ``status`` 的取值集合恒为
    ``{structure_ready, confirmed, invalidated}``。

    这不是「还没接线」的中间态，而是**现状**：``docs/pending-wiring.md`` 与
    ``trade_api.py`` 都据此措辞。若将来真接上盘中反向 K 线（让它产出
    ``alert``），这条会红 —— 那正是该改文档的信号。
    """
    stayed = transition_first_buy(
        ready(), reversal_closed=False, structure_valid=True, event_time=200
    )
    assert stayed.status == "structure_ready"
    assert stayed.alert_time is None
    assert stayed.candidate_time is None


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


# --------------------------------------------------------------------------- #
# R52：方向检查的镜像性与信号类型的隔离
# --------------------------------------------------------------------------- #


def test_first_buy_invalidated_on_upward_trend() -> None:
    """**向上走势不出买点**（模块 docstring 第 1 条）。

    这条以前没人测，而且**测了会红**：``_structure_ready`` 收下
    ``trend_direction`` 却完全不用，于是 ``assess_first_buy(trend_direction=1)``
    照样给 ``structure_ready`` / ``confirmed`` —— 与 docstring 直接矛盾，
    也与 :func:`assess_first_sell` 里那条 ``trend_direction == _UP`` 不对称。
    """
    result = ready(trend_direction=1)
    assert result.status == "invalidated"
    assert result.confirmed_time is None


def test_first_buy_upward_trend_with_reversal_is_not_confirmed() -> None:
    """有反向笔也不例外：方向不对时连 ``confirmed`` 都不给。"""
    assert ready(trend_direction=1, has_reversal_bi=True).status == "invalidated"


def test_transition_first_sell_rejects_a_first_buy_signal() -> None:
    """一卖推进**不许**吃一买信号。

    原来 ``transition_first_sell = transition_first_buy`` 是裸别名，而
    ``_validate_previous`` 接受 ``first_buy`` / ``first_sell`` 两种
    ``signal_type`` ⇒ 一买的推进规则能被套到一卖信号上，而它的 docstring 明写
    ``previous`` 必须 ``signal_type="first_buy"``。
    """
    from cpt.domain.signal import transition_first_sell

    with pytest.raises(ValueError, match="first_sell"):
        transition_first_sell(ready(), True, True, 300)


def test_transition_first_sell_advances_a_first_sell_signal() -> None:
    """对照组：一卖信号走自己的推进规则。"""
    from cpt.domain.signal import transition_first_sell

    advanced = transition_first_sell(sell_ready(), True, True, 300)
    assert advanced.status == "confirmed"
    assert advanced.signal_type == "first_sell"
    assert advanced.confirmed_time == 300


def test_transition_first_buy_rejects_a_first_sell_signal() -> None:
    """镜像方向同样要拦住（一买推进只认 ``first_buy``）。"""
    with pytest.raises(ValueError, match="first_buy"):
        transition_first_buy(sell_ready(), True, True, 300)
