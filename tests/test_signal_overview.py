"""R73 方案 A1：``signal_overview`` 的归并与口径测试。

这层最容易出的错不是算错，而是**把下界说成事实**：
``age_days`` 是「距最后一次跃迁」，不是「这一步卡了几天」。
状态没变就不写行，所以两者在数据里长得一样，只在语义上不同。
下面用 ``test_age_is_a_lower_bound_*`` 把这条口径钉死。
"""

from __future__ import annotations

from typing import Any

from cpt.application.signal_overview import (
    BLIND_SPOT_EMPTY_STRUCTURE,
    STALE_AFTER_DAYS,
    signal_overview,
)

#: 2026-10-21T00:00:00Z，测试里所有时间戳都相对它算。
NOW_MS = 1_792_540_800_000
DAY_MS = 86_400_000


def _event(
    code: str,
    *,
    status: str = "invalidated",
    age_days: int = 0,
    structure_id: str | None = None,
    divergence_status: str = "not_detected",
    invalidated_reason: str | None = None,
    event_id: int = 1,
) -> dict[str, Any]:
    """造一条 ``load_signal_events`` 形态的事件行。"""
    return {
        "id": event_id,
        "code": code,
        "signal_type": "first_buy",
        "status": status,
        "structure_id": structure_id if structure_id is not None else f"level5:bi:5:{code}",
        "divergence_status": divergence_status,
        "transition_time": NOW_MS - age_days * DAY_MS,
        "invalidated_reason": invalidated_reason,
    }


def test_latest_event_per_code_wins() -> None:
    """每只票只取最新一条：旧行不能覆盖新行。"""
    events = [
        _event("000001", status="structure_ready", age_days=0, event_id=2),
        _event("000001", status="invalidated", age_days=30, event_id=1),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    assert overview["total"] == 1
    assert overview["rows"][0]["status"] == "structure_ready"
    assert overview["rows"][0]["age_days"] == 0


def test_ties_broken_by_id_not_by_input_order() -> None:
    """同一时刻两条事件：按 id 定序，与传入顺序无关。"""
    same_time = [
        _event("000001", status="invalidated", age_days=0, event_id=7),
        _event("000001", status="confirmed", age_days=0, event_id=9),
    ]
    overview = signal_overview(same_time, now_ms=NOW_MS)
    assert overview["rows"][0]["status"] == "confirmed"


def test_events_without_code_are_ignored() -> None:
    """没有 code 的行无法归属到票，跳过而不崩。"""
    overview = signal_overview([_event(""), _event("000001")], now_ms=NOW_MS)
    assert overview["total"] == 1


def test_empty_structure_is_counted_as_blind_spot() -> None:
    """``level5:empty:<code>`` 是兜底结构：单列，不进状态分布。"""
    events = [
        _event("000014", structure_id="level5:empty:000014"),
        _event("000009", status="structure_ready"),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    assert overview["blind_spots"] == {BLIND_SPOT_EMPTY_STRUCTURE: 1}
    # 兜底结构虽然 status=invalidated，也不能算进「已作废」
    assert overview["status_counts"] == {"structure_ready": 1}


def test_status_counts_cover_only_classifiable_rows() -> None:
    """能区分结构 + 兜底结构的总数应等于 total（不重不漏）。"""
    events = [
        _event("000001", status="structure_ready"),
        _event("000002", status="confirmed"),
        _event("000003", status="invalidated"),
        _event("000004", structure_id="level5:empty:000004"),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    counted = sum(overview["status_counts"].values()) + sum(overview["blind_spots"].values())
    assert counted == overview["total"] == 4


def test_unrecorded_reason_stays_unknown_not_guessed() -> None:
    """R71 之前的失效行没记原因 —— 单列 unknown，不并进任何具体原因。"""
    events = [
        _event("000001", invalidated_reason=None),
        _event("000002", invalidated_reason="no_divergence_leg"),
        _event("000003", invalidated_reason="not_enough_centers"),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    assert overview["invalidation_reasons"] == {
        "unknown": 1,
        "no_divergence_leg": 1,
        "not_enough_centers": 1,
    }


def test_non_invalidated_rows_never_contribute_a_reason() -> None:
    """非失效行就算带了原因字段，也不算失效原因。"""
    events = [
        _event("000001", status="structure_ready", invalidated_reason="no_divergence_leg"),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    assert overview["invalidation_reasons"] == {}


def test_stale_is_measured_against_the_threshold() -> None:
    """距最后跃迁 >= STALE_AFTER_DAYS 标陈旧，差一天不算。"""
    events = [
        _event("000001", age_days=STALE_AFTER_DAYS - 1),
        _event("000002", age_days=STALE_AFTER_DAYS),
        _event("000003", age_days=STALE_AFTER_DAYS + 10),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    assert overview["stale_count"] == 2
    flags = {row["code"]: row["stale"] for row in overview["rows"]}
    assert flags == {"000001": False, "000002": True, "000003": True}


def test_age_is_a_lower_bound_and_says_so() -> None:
    """``age_is_lower_bound`` 恒为 True —— 这是给前端/人的口径信号，不是计算值。

    状态没变就不写行，所以「距最后跃迁 1 天」的票可能已经卡了 180 天。
    本层**没有**任何字段能回答真实卡了多久，故必须自报下界。
    """
    overview = signal_overview([_event("000001", age_days=1)], now_ms=NOW_MS)
    assert overview["age_is_lower_bound"] is True
    assert "age_days" in overview["rows"][0]
    # 不存在任何声称「真实停滞时长」的字段
    for row in overview["rows"]:
        assert not any("stuck" in key or "held" in key for key in row)


def test_age_days_never_negative_for_future_timestamps() -> None:
    """时钟漂移造出未来时间戳时夹到 0，不产出负数天数。"""
    event = _event("000001")
    event["transition_time"] = NOW_MS + 5 * DAY_MS
    overview = signal_overview([event], now_ms=NOW_MS)
    assert overview["rows"][0]["age_days"] == 0


def test_missing_transition_time_yields_none_age() -> None:
    """时间戳不可用 → age 为 None，而不是 0（0 会被读成「今天刚跃迁」）。"""
    event = _event("000001")
    event["transition_time"] = None
    overview = signal_overview([event], now_ms=NOW_MS)
    row = overview["rows"][0]
    assert row["age_days"] is None
    assert row["stale"] is False
    # 不确定的行排在已知行之后
    assert overview["rows"][-1] is row


def test_rows_sorted_newest_first() -> None:
    """行按 (age_days, code) 升序：最新的在最前，用户一眼看到该看的。"""
    events = [
        _event("000003", age_days=5),
        _event("000001", age_days=0),
        _event("000002", age_days=0),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    assert [row["code"] for row in overview["rows"]] == ["000001", "000002", "000003"]


def test_reason_codes_exposed_for_frontend_validation() -> None:
    """合法原因码随响应下发，前端据此校验标签表不漂移。"""
    overview = signal_overview([], now_ms=NOW_MS)
    assert overview["reason_codes"] == [
        "not_in_downtrend",
        "not_enough_centers",
        "no_divergence_leg",
        "previous_invalidated",
    ]


def test_empty_input_is_an_honest_empty() -> None:
    """没有事件 → 空统计，不伪造任何计数。"""
    overview = signal_overview([], now_ms=NOW_MS)
    assert overview["total"] == 0
    assert overview["rows"] == []
    assert overview["status_counts"] == {}
    assert overview["stale_count"] == 0


def test_input_is_not_mutated() -> None:
    """只读：不改调用方传进来的事件字典。"""
    event = _event("000001")
    before = dict(event)
    signal_overview([event], now_ms=NOW_MS)
    assert event == before


def test_step_label_only_for_standing_states() -> None:
    """只有站住的状态有「一步」的说法；失效行不给步骤名（无意义）。"""
    events = [
        _event("000001", status="structure_ready"),
        _event("000002", status="confirmed"),
        _event("000003", status="invalidated"),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    steps = {row["code"]: row["step"] for row in overview["rows"]}
    assert "反向笔" in steps["000001"]
    assert "三步全过" in steps["000002"]
    assert steps["000003"] == ""
