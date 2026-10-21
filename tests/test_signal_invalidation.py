"""R71 Step3：失效归因的判定与落库测试。

两组重点：

1. **判定语义** —— 结构原因必须优先于「以前失效过」，否则答案退化成
   「以前失效过」等于没回答（实测 000006/000014 都踩过）。
2. **落库可达性** —— 失效是终态，若沿用「同状态就跳过」，原因永远写不进去；
   这里钉住「带原因的 invalidated 会补一笔、同原因不刷屏」。
"""

from __future__ import annotations

import pytest
from cpt.application.signal_invalidation import (
    REASON_CODES,
    REASON_TEXT,
    classify_invalidation,
    explain_invalidation,
    invalidation_payload,
)


class _Facts:
    def __init__(self, *, two: bool = True, leg: bool = True, reversal: bool = False) -> None:
        self.has_two_centers = two
        self.has_divergence_leg = leg
        self.has_reversal_bi = reversal
        self.divergence_status = "not_detected"


# ---------------------------------------------------------------- 判定语义


def test_up_trend_is_not_in_downtrend() -> None:
    assert classify_invalidation(trend_direction=1, facts=None) == "not_in_downtrend"


def test_unknown_direction_is_not_in_downtrend() -> None:
    """方向未知（``0``）不能当成下跌走势去归因，否则会编出结构原因。"""
    assert classify_invalidation(trend_direction=0, facts=_Facts()) == "not_in_downtrend"


def test_structural_reason_beats_previous_invalidated() -> None:
    """**核心语义**：结构原因优先于「以前失效过」。

    若顺序反过来，一个失效过的结构每轮都只得到 ``previous_invalidated``，
    用户看到的永远是「以前失效过」，而不是「这次为什么没成立」。
    """
    assert (
        classify_invalidation(
            trend_direction=-1, facts=_Facts(two=False), previous_status="invalidated"
        )
        == "not_enough_centers"
    )
    assert (
        classify_invalidation(
            trend_direction=-1, facts=_Facts(leg=False), previous_status="invalidated"
        )
        == "no_divergence_leg"
    )


def test_previous_invalidated_only_when_structure_is_fine() -> None:
    """三条件都满足却仍作废 —— 此时才轮到「终态不回退」这个解释。"""
    assert (
        classify_invalidation(trend_direction=-1, facts=_Facts(), previous_status="invalidated")
        == "previous_invalidated"
    )


def test_no_reason_when_structure_passes() -> None:
    assert classify_invalidation(trend_direction=-1, facts=_Facts()) is None


def test_missing_facts_returns_none_instead_of_guessing() -> None:
    """方向是向下但事实没算出来 —— 证据不足，宁可 ``None`` 也不猜。"""
    assert classify_invalidation(trend_direction=-1, facts=None) is None


def test_every_reason_code_has_text() -> None:
    for code in REASON_CODES:
        assert code in REASON_TEXT, f"{code} 没有文案"
        assert REASON_TEXT[code].strip(), f"{code} 文案为空"


def test_explain_never_returns_empty_string() -> None:
    assert explain_invalidation(None) == "未失效。"
    unknown = explain_invalidation("no_such_code")
    assert unknown and "no_such_code" in unknown, "未知码必须显式报出码，不能悄悄返回空"


def test_payload_shape_and_none_on_no_invalidation() -> None:
    payload = invalidation_payload(
        trend_direction=-1, facts=_Facts(leg=False), previous_status=None, segment_position=0.9
    )
    assert payload is not None
    assert payload["reason"] == "no_divergence_leg"
    assert payload["explain"] == REASON_TEXT["no_divergence_leg"]
    assert payload["evidence"]["has_divergence_leg"] is False
    assert payload["evidence"]["segment_position"] == 0.9
    assert invalidation_payload(trend_direction=-1, facts=_Facts()) is None


def test_payload_evidence_says_null_when_facts_missing() -> None:
    """事实缺失时证据字段必须是 ``None``（不知道），不能填 ``False``（当成没满足）。"""
    payload = invalidation_payload(trend_direction=1, facts=None, previous_status=None)
    assert payload is not None
    assert payload["reason"] == "not_in_downtrend"
    assert payload["evidence"]["has_two_centers"] is None
    assert payload["evidence"]["has_divergence_leg"] is None


# ---------------------------------------------------------------- 落库可达性

psycopg = pytest.importorskip("psycopg")


class _StubCursor:
    """记录执行过的 SQL 与参数，供断言「写没写」与「写了什么原因」。"""

    def __init__(self, latest: tuple[object, object] | None) -> None:
        self.latest = latest
        self.statements: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append((sql, params))

    def fetchone(self) -> tuple[object, object] | None:
        return self.latest

    def __enter__(self) -> _StubCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def inserts(self) -> list[tuple[str, object]]:
        return [
            (sql, p) for sql, p in self.statements if "INSERT INTO public.cpt_signal_event" in sql
        ]


class _StubConn:
    def __init__(self, latest: tuple[object, object] | None) -> None:
        self.cur = _StubCursor(latest)

    def cursor(self) -> _StubCursor:
        return self.cur


def _signal(status: str):
    from cpt.domain.models import Signal

    return Signal(
        signal_id="first_buy:5:struct",
        level=5,
        signal_type="first_buy",
        status=status,
        structure_id="struct",
        center_ids=("cn:zhongshu:5:1",),
        divergence_status="not_detected",
        alert_time=None,
        candidate_time=None,
        confirmed_time=None,
        invalidated_time=None,
        price=10.0,
        source_revision=0,
    )


def test_terminal_invalidated_still_records_reason() -> None:
    """**核心可达性**：失效是终态，同状态也要把原因补写进去。

    历史行 ``prev_status=None → invalidated`` 且无原因时，本轮再算出
    ``invalidated`` 必须补一笔带原因的，否则原因永远写不进去。
    """
    from cpt.storage.signal_event_store import record_signal_event

    conn = _StubConn(latest=("invalidated", ["cn:zhongshu:5:1"]))
    wrote = record_signal_event(
        conn,
        _signal("invalidated"),
        "invalidated",
        "000006",
        1_700_000_000_000,
        invalidated_reason={"reason": "not_enough_centers", "explain": "x"},
    )
    assert wrote is True
    inserts = conn.cur.inserts()
    assert len(inserts) == 1
    params = inserts[0][1]
    assert isinstance(params, tuple)
    # center_ids 是第 11 个占位符（见 INSERT 列顺序），必须是 list 且尾项带原因
    center_ids = params[10]
    assert isinstance(center_ids, list)
    assert center_ids[-1] == "reason:not_enough_centers"


def test_same_reason_is_not_written_twice() -> None:
    """同原因重复评估不该每轮刷一行。"""
    from cpt.storage.signal_event_store import record_signal_event

    conn = _StubConn(latest=("invalidated", ["cn:zhongshu:5:1", "reason:not_enough_centers"]))
    wrote = record_signal_event(
        conn,
        _signal("invalidated"),
        "invalidated",
        "000006",
        1_700_000_000_000,
        invalidated_reason={"reason": "not_enough_centers", "explain": "x"},
    )
    assert wrote is False
    assert conn.cur.inserts() == []


def test_reason_can_upgrade_from_unknown() -> None:
    """旧行原因为 ``None``（R71 之前）时，已知原因要能覆盖它。"""
    from cpt.storage.signal_event_store import record_signal_event

    conn = _StubConn(latest=("invalidated", ["cn:zhongshu:5:1"]))
    wrote = record_signal_event(
        conn,
        _signal("invalidated"),
        "invalidated",
        "000006",
        1_700_000_000_000,
        invalidated_reason={"reason": "no_divergence_leg", "explain": "x"},
    )
    assert wrote is True


def test_non_invalidated_transition_unchanged() -> None:
    """非失效跃迁仍按老语义去重，不受 R71 影响。"""
    from cpt.storage.signal_event_store import record_signal_event

    conn = _StubConn(latest=("confirmed", []))
    wrote = record_signal_event(
        conn, _signal("confirmed"), "confirmed", "000006", 1_700_000_000_000
    )
    assert wrote is False


def test_reason_from_center_ids_roundtrip() -> None:
    from cpt.storage.signal_event_store import reason_from_center_ids

    assert (
        reason_from_center_ids(["cn:zhongshu:5:1", "reason:no_divergence_leg"])
        == "no_divergence_leg"
    )
    assert reason_from_center_ids(["cn:zhongshu:5:1"]) is None
    assert reason_from_center_ids([]) is None
    assert reason_from_center_ids(None) is None
    # 记号不在尾项 ⇒ 不认（防止历史数据里混入的字符串被误读）
    assert reason_from_center_ids(["reason:no_divergence_leg", "cn:zhongshu:5:1"]) is None
