"""``cpt.application.signal_event_store`` 测试。

不依赖 psycopg — mock conn 即可。验证：
1. ``load_previous_signal``：空表返回 None；有数据返回最新 Signal。
2. ``record_signal_event``：status 变化时写入；status 不变时跳过。
3. ``_derive_first_buy_signal`` 接线：传入 client 时加载 previous + 记录事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from cpt.application.signal_event_store import (
    SignalEventError,
    load_previous_signal,
    record_signal_event,
)
from cpt.domain.models import Signal

# --------------------------------------------------------------------------- #
# Mock DB
# --------------------------------------------------------------------------- #


@dataclass
class MockCursor:
    """psycopg cursor duck type — 记录 execute 调用并返回预设结果。"""

    rows: list[tuple] | None = None
    executed: list[tuple[str, tuple]] = field(default_factory=list)
    _pending: list[tuple] | None = None

    def __enter__(self) -> MockCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((sql, params))
        if self._pending is not None:
            self.rows = self._pending

    def fetchone(self) -> tuple | None:
        if self.rows:
            return self.rows[0]
        return None


class MockConn:
    """psycopg connection duck type — 返回 MockCursor。"""

    def __init__(self, rows: list[tuple] | None = None) -> None:
        self._rows = rows
        self.committed: bool = False
        self.cursors: list[MockCursor] = []

    def cursor(self) -> MockCursor:
        cur = MockCursor(rows=self._rows)
        self.cursors.append(cur)
        return cur

    def commit(self) -> None:
        self.committed = True


def _make_signal(
    *,
    signal_id: str = "first_buy:0:level0:zs1",
    status: str = "structure_ready",
    level: int = 0,
    structure_id: str = "level0:zs1",
    center_ids: tuple[str, ...] = ("zs0", "zs1"),
    divergence_status: str = "not_checked",
    alert_time: int | None = None,
    candidate_time: int | None = None,
    confirmed_time: int | None = None,
    invalidated_time: int | None = None,
    price: float = 100.0,
    source_revision: int = 0,
) -> Signal:
    return Signal(
        signal_id=signal_id,
        level=level,
        signal_type="first_buy",
        status=status,
        structure_id=structure_id,
        center_ids=center_ids,
        divergence_status=divergence_status,
        alert_time=alert_time,
        candidate_time=candidate_time,
        confirmed_time=confirmed_time,
        invalidated_time=invalidated_time,
        price=price,
        source_revision=source_revision,
    )


# --------------------------------------------------------------------------- #
# load_previous_signal
# --------------------------------------------------------------------------- #


def test_load_previous_signal_empty_table_returns_none() -> None:
    conn = MockConn(rows=[])
    result = load_previous_signal(conn, "first_buy:0:level0:zs1")
    assert result is None


def test_load_previous_signal_returns_latest_signal() -> None:
    """有事件时返回最新一条重建的 Signal。"""
    row = (
        "first_buy:0:level0:zs1",  # signal_id
        0,  # level
        "first_buy",  # signal_type
        "confirmed",  # status
        "level0:zs1",  # structure_id
        ["zs0", "zs1"],  # center_ids (text[] -> list)
        "detected",  # divergence_status
        datetime(2026, 1, 1, tzinfo=UTC),  # alert_time
        datetime(2026, 1, 2, tzinfo=UTC),  # candidate_time
        datetime(2026, 1, 3, tzinfo=UTC),  # confirmed_time
        None,  # invalidated_time
        105.0,  # price
        0,  # source_revision
    )
    conn = MockConn(rows=[row])
    result = load_previous_signal(conn, "first_buy:0:level0:zs1")
    assert result is not None
    assert result.signal_id == "first_buy:0:level0:zs1"
    assert result.status == "confirmed"
    assert result.level == 0
    assert result.center_ids == ("zs0", "zs1")
    assert result.divergence_status == "detected"
    assert result.price == 105.0
    # timestamptz -> ms round-trip
    expected_ms = int(datetime(2026, 1, 3, tzinfo=UTC).timestamp() * 1000)
    assert result.confirmed_time == expected_ms


def test_load_previous_signal_db_error_degrades_gracefully() -> None:
    """DB 报错时降级为 None（不抛异常），不把快照搞挂。"""

    class ExplodingConn:
        def cursor(self) -> Any:
            raise RuntimeError("DB is down")

        def commit(self) -> None:
            pass

    result = load_previous_signal(ExplodingConn(), "first_buy:0:zs1")
    assert result is None


# --------------------------------------------------------------------------- #
# record_signal_event
# --------------------------------------------------------------------------- #


def test_record_signal_event_writes_on_status_change() -> None:
    """status 从 structure_ready -> confirmed -> 写入一条事件。"""
    conn = MockConn()
    signal = _make_signal(status="confirmed")
    written = record_signal_event(
        conn,
        signal,
        prev_status="structure_ready",
        code="600519",
        event_time=1704067200000,
    )
    assert written is True
    assert len(conn.cursors) == 1
    sql, params = conn.cursors[0].executed[0]
    assert "INSERT INTO public.cpt_signal_event" in sql
    assert params[0] == "first_buy:0:level0:zs1"  # signal_id
    assert params[1] == "600519"  # code
    assert params[5] == "structure_ready"  # prev_status
    assert params[6] == "confirmed"  # status


def test_record_signal_event_skips_on_same_status() -> None:
    """status 不变 -> 不写，返回 False。"""
    conn = MockConn()
    signal = _make_signal(status="structure_ready")
    written = record_signal_event(
        conn,
        signal,
        prev_status="structure_ready",
        code="600519",
        event_time=1704067200000,
    )
    assert written is False
    assert len(conn.cursors) == 0  # 没碰 DB


def test_record_signal_event_first_event_prev_status_none() -> None:
    """首次评估 -> prev_status=None，写入一条事件。"""
    conn = MockConn()
    signal = _make_signal(status="structure_ready")
    written = record_signal_event(
        conn,
        signal,
        prev_status=None,
        code="600519",
        event_time=1704067200000,
    )
    assert written is True
    sql, params = conn.cursors[0].executed[0]
    assert params[5] is None  # prev_status


def test_record_signal_event_db_error_raises() -> None:
    """DB 报错 -> 抛 SignalEventError。"""

    class ExplodingCursor:
        def __enter__(self) -> ExplodingCursor:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def execute(self, sql: str, params: tuple = ()) -> None:
            raise RuntimeError("unique_violation")

    class ExplodingConn:
        def cursor(self) -> ExplodingCursor:
            return ExplodingCursor()

        def commit(self) -> None:
            pass

    signal = _make_signal(status="confirmed")
    with pytest.raises(SignalEventError, match="记录信号事件失败"):
        record_signal_event(
            ExplodingConn(),
            signal,
            prev_status="structure_ready",
            code="600519",
            event_time=1704067200000,
        )


def test_record_signal_event_zero_time_falls_back() -> None:
    """event_time=0 -> transition_time 用墙钟 now()。"""
    conn = MockConn()
    signal = _make_signal(status="confirmed")
    written = record_signal_event(
        conn,
        signal,
        prev_status="structure_ready",
        code="600519",
        event_time=0,
    )
    assert written is True
    sql, params = conn.cursors[0].executed[0]
    assert isinstance(params[7], datetime)
    assert params[7].tzinfo == UTC


# --------------------------------------------------------------------------- #
# Integration: _derive_first_buy_signal with client
# --------------------------------------------------------------------------- #


def test_derive_signal_with_client_invokes_load(monkeypatch) -> None:
    """传入 client 时，_derive_first_buy_signal 调 load_previous_signal。"""
    from cpt.application import a_share_snapshot as mod

    # Mock RulesConfig 让默认 level = 0（匹配测试数据）
    monkeypatch.setattr(
        mod.RulesConfig,
        "__init__",
        lambda self, **kwargs: None,
    )
    monkeypatch.setattr(
        mod.RulesConfig,
        "levels",
        property(lambda self: (0,)),
    )

    # Mock derive_first_buy_facts 返回一个有效结构
    FakeFacts = type(
        "FakeFacts",
        (),
        {
            "structure_id": "level0:zs1",
            "center_ids": ("zs0", "zs1"),
            "has_two_centers": True,
            "has_divergence_leg": True,
            "has_reversal_bi": True,
            "divergence_status": "detected",
        },
    )
    monkeypatch.setattr(
        mod,
        "derive_first_buy_facts",
        lambda level, trend_direction, bis, zhongshus: FakeFacts(),
    )

    # Mock assess_first_buy 返回一个信号
    out_signal = _make_signal(status="structure_ready")
    monkeypatch.setattr(
        mod,
        "assess_first_buy",
        lambda **kwargs: out_signal,
    )

    load_calls: list[str] = []
    record_calls: list[dict[str, Any]] = []

    def fake_load(conn, signal_id):
        load_calls.append(signal_id)
        return None  # 首次评估

    def fake_record(conn, signal, prev_status, code, event_time):
        record_calls.append(
            {
                "signal_id": signal.signal_id,
                "prev_status": prev_status,
                "status": signal.status,
            }
        )
        return True

    monkeypatch.setattr(mod, "load_previous_signal", fake_load)
    monkeypatch.setattr(mod, "record_signal_event", fake_record)

    # Fake client — 返回一个 MockConn（mock 函数不使用它）
    class FakeClient:
        def _get_conn(self) -> MockConn:
            return MockConn()

    bis = _make_bis_up_down()
    zhongshus = _make_two_centers()
    bars = _make_bars(40)

    result = mod._derive_first_buy_signal(
        bis,
        zhongshus,
        bars,
        client=FakeClient(),
        code="600519",
    )

    # 验证 load 被调用
    assert len(load_calls) == 1
    assert load_calls[0] == "first_buy:0:level0:zs1"
    # 验证 record 被调用（首次评估，prev_status=None）
    # has_reversal_bi=True 触发状态推进：structure_ready → confirmed
    assert len(record_calls) == 1
    assert record_calls[0]["prev_status"] is None
    assert record_calls[0]["status"] == "confirmed"
    assert result is not out_signal  # 推进后是新对象


def _make_bars(n: int) -> list[Any]:
    from cpt.domain.models import CanonicalBar

    end_ms = int(datetime(2026, 9, 24, tzinfo=UTC).timestamp() * 1000)
    bars = []
    for i in range(n):
        t = end_ms - (n - 1 - i) * 24 * 3600 * 1000
        bars.append(
            CanonicalBar(
                open_time=t,
                open=10.0,
                high=11.0,
                low=9.0,
                close=10.5,
                volume=100.0,
                close_time=t + 24 * 3600 * 1000 - 1,
                quote_volume=200.0,
                trade_count=1,
                taker_buy_base_volume=0.0,
                taker_buy_quote_volume=0.0,
                is_closed=True,
            )
        )
    return bars


def _make_two_centers() -> tuple[Any, ...]:
    from cpt.domain.models import ZhongShu

    t0 = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp() * 1000)
    day = 24 * 3600 * 1000
    return (
        ZhongShu(
            level=0,
            start_time=t0,
            end_time=t0 + 10 * day,
            high=12.0,
            low=8.0,
            bi_ids=("bi0", "bi1"),
        ),
        ZhongShu(
            level=0,
            start_time=t0 + 15 * day,
            end_time=t0 + 25 * day,
            high=11.0,
            low=7.0,
            bi_ids=("bi2", "bi3"),
        ),
    )


def _make_bis_up_down() -> tuple[Any, ...]:
    from cpt.domain.models import Bi

    t0 = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp() * 1000)
    day = 24 * 3600 * 1000
    return (
        Bi(
            level=0,
            direction=-1,
            start_time=t0,
            end_time=t0 + 3 * day,
            high=12.0,
            low=10.0,
            source_ids=("fx:0", "fx:3"),
        ),
        Bi(
            level=0,
            direction=1,
            start_time=t0 + 3 * day,
            end_time=t0 + 6 * day,
            high=11.0,
            low=9.0,
            source_ids=("fx:3", "fx:6"),
        ),
        Bi(
            level=0,
            direction=-1,
            start_time=t0 + 6 * day,
            end_time=t0 + 9 * day,
            high=10.0,
            low=8.0,
            source_ids=("fx:6", "fx:9"),
        ),
        Bi(
            level=0,
            direction=1,
            start_time=t0 + 9 * day,
            end_time=t0 + 12 * day,
            high=9.0,
            low=7.0,
            source_ids=("fx:9", "fx:12"),
        ),
        Bi(
            level=0,
            direction=-1,
            start_time=t0 + 12 * day,
            end_time=t0 + 18 * day,
            high=8.0,
            low=6.0,
            source_ids=("fx:12", "fx:18"),
        ),
        Bi(
            level=0,
            direction=1,
            start_time=t0 + 18 * day,
            end_time=t0 + 24 * day,
            high=7.0,
            low=5.0,
            source_ids=("fx:18", "fx:24"),
        ),
        Bi(
            level=0,
            direction=-1,
            start_time=t0 + 24 * day,
            end_time=t0 + 30 * day,
            high=6.0,
            low=4.0,
            source_ids=("fx:24", "fx:30"),
        ),
        Bi(
            level=0,
            direction=1,
            start_time=t0 + 30 * day,
            end_time=t0 + 36 * day,
            high=5.0,
            low=3.0,
            source_ids=("fx:30", "fx:36"),
        ),
    )
