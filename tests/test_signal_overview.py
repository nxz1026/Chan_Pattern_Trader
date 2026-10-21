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


# ── R78：走完几步 / 卡在哪步、为什么 ───────────────────────────────


def test_step_index_is_three_for_confirmed_and_two_for_ready() -> None:
    """confirmed = 三步全过；structure_ready = 前两步过了、等反向笔。"""
    events = [
        _event("000001", status="confirmed"),
        _event("000002", status="structure_ready"),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    by_code = {row["code"]: row["step_index"] for row in overview["rows"]}
    assert by_code == {"000001": 3, "000002": 2}


def test_step_index_is_located_by_reason_for_invalidated() -> None:
    """失效票靠原因码定位卡在第几步 —— 这是 R78 的核心增量。"""
    events = [
        _event("000001", invalidated_reason="not_enough_centers"),
        _event("000002", invalidated_reason="no_divergence_leg"),
        _event("000003", invalidated_reason="not_in_downtrend"),
        _event("000004", invalidated_reason="previous_invalidated"),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    by_code = {row["code"]: (row["step_index"], row["blocked_at"]) for row in overview["rows"]}
    assert by_code == {
        "000001": (0, "two_centers"),
        "000002": (1, "divergence_leg"),
        "000003": (0, "two_centers"),  # 方向不对：三步一步都没开始
        "000004": (2, "reversal_bi"),  # 三步都过了却仍是终态
    }


def test_missing_reason_yields_none_step_index_not_a_guess() -> None:
    """**R78 踩过的坑**：没原因时不许「保守取 2」。

    第一版把没原因的 invalidated 一律取 2（上界推断），生产实测 117 只全部
    落进「第 2 步」，与 55 只真正的 structure_ready 混成一桶，分布读起来
    像「市场普遍就差临门一脚」。上界一旦被当成分布读就会造出反向结论。
    """
    overview = signal_overview([_event("000001", invalidated_reason=None)], now_ms=NOW_MS)
    row = overview["rows"][0]
    assert row["step_index"] is None
    assert row["blocked_at"] is None


def test_step_distribution_buckets_unknown_separately() -> None:
    """分布里「哪一步不确定」必须单列一桶，不能并进任何一个具体步号。"""
    events = [
        _event("000001", status="confirmed"),
        _event("000002", status="structure_ready"),
        _event("000003", invalidated_reason="no_divergence_leg"),
        _event("000004", invalidated_reason=None),
        _event("000005", invalidated_reason=None),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    dist = overview["step_distribution"]
    assert dist["step_index_counts"] == {"0": 0, "1": 1, "2": 1, "3": 1, "unknown": 2}
    assert dist["no_reason_but_blocked"] == 2


def test_step_distribution_unknown_matches_the_standalone_counter() -> None:
    """``unknown`` 桶与 ``no_reason_but_blocked`` 必须相等 —— 同一件事的两种说法。"""
    events = [
        _event("000001", invalidated_reason=None),
        _event("000002", invalidated_reason=None),
        _event("000003", invalidated_reason="no_divergence_leg"),
    ]
    dist = signal_overview(events, now_ms=NOW_MS)["step_distribution"]
    assert dist["step_index_counts"]["unknown"] == dist["no_reason_but_blocked"] == 2


def test_step_distribution_accounts_for_every_non_blind_row() -> None:
    """不重不漏：非盲区行全部有归属（>= 真步号桶 或 unknown 桶）。"""
    events = [
        _event("000001", status="confirmed"),
        _event("000002", status="structure_ready"),
        _event("000003", invalidated_reason="not_enough_centers"),
        _event("000004", invalidated_reason=None),
        _event("000005", structure_id="level5:empty:000005"),  # 盲区，不进分布
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    dist = overview["step_distribution"]
    accounted = sum(dist["step_index_counts"].values())
    non_blind = overview["total"] - sum(overview["blind_spots"].values())
    assert accounted == non_blind == 4


def test_step_distribution_breaks_down_each_blocked_step_by_reason() -> None:
    """卡住的票要能分解到「第几步 + 哪条原因」，两个维度都得有。"""
    events = [
        _event("000001", invalidated_reason="no_divergence_leg"),
        _event("000002", invalidated_reason="no_divergence_leg"),
        _event("000003", invalidated_reason="not_enough_centers"),
    ]
    dist = signal_overview(events, now_ms=NOW_MS)["step_distribution"]
    assert dist["blocked_reason_counts"] == {
        "two_centers": {"not_enough_centers": 1},
        "divergence_leg": {"no_divergence_leg": 2},
    }
    assert dist["blocked_at_order"] == ["two_centers", "divergence_leg", "reversal_bi"]


def test_blind_spots_never_enter_the_step_distribution() -> None:
    """兜底结构连第一步都没开始，混进分布会让「第 0 步」虚高（实测 25 只）。"""
    events = [
        _event("000001", structure_id="level5:empty:000001"),
        _event("000002", structure_id="level5:empty:000002"),
        _event("000003", status="structure_ready"),
    ]
    overview = signal_overview(events, now_ms=NOW_MS)
    dist = overview["step_distribution"]
    assert dist["step_index_counts"]["0"] == 0
    assert sum(dist["step_index_counts"].values()) == 1


def test_step_titles_are_shipped_so_frontend_does_not_hardcode() -> None:
    """步号 → 人话的标题由后端下发，前端只做拼装，不各自维护一份。"""
    overview = signal_overview([], now_ms=NOW_MS)
    assert overview["step_titles"] == {
        "two_centers": "第一步：两个同级中枢",
        "divergence_leg": "第二步：中枢二之后还在跌",
        "reversal_bi": "第三步：跌势里冒出一笔反弹",
    }


def test_every_reason_code_has_a_blocked_step() -> None:
    """每个合法原因码都必须能映射到某一步，否则它的失效票永远定位不了。"""
    from cpt.application.signal_invalidation import REASON_CODES
    from cpt.application.signal_overview import REASON_TO_BLOCKED_AT

    assert set(REASON_CODES) == set(REASON_TO_BLOCKED_AT)


# --------------------------------------------------------------------------- #
# R80：关注列表「跑过没有 / 数据多旧」
# --------------------------------------------------------------------------- #


def test_no_watchlist_means_not_watched_not_an_empty_alarm() -> None:
    """没关注任何票时 ``watched=False``、``counts=None`` —— 前端据此不渲染提醒。"""
    out = signal_overview([_event("000001")], now_ms=NOW_MS, watched=[])
    wl = out["watchlist"]
    assert wl["watched"] is False
    assert wl["counts"] is None
    assert wl["items"] == []


def test_omitting_watched_is_the_same_as_an_empty_list() -> None:
    """``watched=None``（老调用方）与 ``[]`` 同义，不许抛。"""
    out = signal_overview([_event("000001")], now_ms=NOW_MS)
    assert out["watchlist"]["watched"] is False


def test_never_run_is_not_merged_into_stale() -> None:
    """没跑过 ≠ 跑了但旧：两者分开计数，理由见模块 docstring。

    合并成一句「没数据」会让用户不知道该等自动任务还是该手动跑。
    """
    events = [_event("000001", age_days=0)]
    out = signal_overview(events, now_ms=NOW_MS, watched=["000001", "600000"])
    wl = out["watchlist"]
    assert wl["counts"] == {"total": 2, "never_run": 1, "stale": 0, "fresh": 1}
    by_code = {it["code"]: it for it in wl["items"]}
    assert by_code["600000"]["never_run"] is True
    assert by_code["600000"]["age_days"] is None
    assert by_code["600000"]["status"] is None
    assert by_code["000001"]["never_run"] is False
    assert by_code["000001"]["age_days"] == 0


def test_stale_watchlist_entry_uses_the_same_threshold_as_the_overview() -> None:
    """``stale`` 判据必须与全景行一致（≥ STALE_AFTER_DAYS），不能各算一套。"""
    events = [
        _event("000001", age_days=STALE_AFTER_DAYS - 1),
        _event("000002", age_days=STALE_AFTER_DAYS, event_id=2),
        _event("000003", age_days=STALE_AFTER_DAYS + 30, event_id=3),
    ]
    out = signal_overview(events, now_ms=NOW_MS, watched=["000001", "000002", "000003"])
    assert out["watchlist"]["counts"] == {
        "total": 3,
        "never_run": 0,
        "stale": 2,
        "fresh": 1,
    }


def test_watchlist_status_reuses_the_latest_event_of_the_overview() -> None:
    """同一份「最新事件」：关注区与全景不许对同一只票给出两个状态。"""
    events = [
        _event("000001", status="structure_ready", age_days=1),
        _event("000001", status="confirmed", age_days=0, event_id=2),
    ]
    out = signal_overview(events, now_ms=NOW_MS, watched=["000001"])
    row = next(r for r in out["rows"] if r["code"] == "000001")
    assert row["status"] == "confirmed"
    assert out["watchlist"]["items"][0]["status"] == "confirmed"


def test_watchlist_dedupes_and_keeps_order() -> None:
    """重复代码不重复计数；顺序保持用户关注顺序（不是字典序）。"""
    out = signal_overview([], now_ms=NOW_MS, watched=["600519", "000001", "600519"])
    items = out["watchlist"]["items"]
    assert [it["code"] for it in items] == ["600519", "000001"]
    assert out["watchlist"]["counts"]["total"] == 2


def test_watch_refresh_hours_is_passed_through_for_the_copy() -> None:
    """「几小时内跑一次」由调用方给，本模块不读调度器配置。"""
    out = signal_overview([], now_ms=NOW_MS, watched=["000001"], watch_refresh_hours=6)
    assert out["watchlist"]["refresh_hours"] == 6
