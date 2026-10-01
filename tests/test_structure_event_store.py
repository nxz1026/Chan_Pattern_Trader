"""``cpt.storage.structure_event_store`` 测试（不连库）。

核心要证明的是**闭环**：
``diff_states`` 产出事件 → ``append_events`` 落库 → ``current_states`` 派生回
``StructureState``。R26 之前 ``StructureState`` 全仓零生产者零消费者，这组
测试就是它的存在理由。

另外守两条 store 层约定（与 R23/R25 同一个教训）：
- **store 层不 commit**（事务边界归调用方）
- **空批次是 no-op**（每轮快照都 diff，绝大多数轮次无变化，不该发 SQL）
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from cpt.domain.models import StructureEvent, StructureState
from cpt.domain.structure_events import diff_states, state_to_payload, structure_id_of
from cpt.storage import structure_event_store as store


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self.rowcount = 0

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.executed.append((sql, params))
        self._conn.rows = list(self._conn.next_rows)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        self._conn.executed.append((sql, rows))
        self.rowcount = len(rows)
        self._conn.written.extend(rows)

    def fetchall(self) -> list[tuple]:
        return list(self._conn.rows)


class FakeConn:
    def __init__(self, next_rows: list[tuple] | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.written: list[tuple] = []
        self.rows: list[tuple] = []
        self.next_rows = next_rows or []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


def _state(
    kind: str = "bi", *, status: str = "forming", start: int = 1_700_000_000_000
) -> StructureState:
    sid = structure_id_of("cn", kind, 5, start)  # type: ignore[arg-type]
    return StructureState(
        id=sid,
        level=5,
        kind=kind,  # type: ignore[arg-type]
        direction=1,
        start_time=start,
        end_time=start + 3_600_000,
        status=status,  # type: ignore[arg-type]
        revision=1,
        first_seen_at=start,
        confirmed_at=start + 3_600_000 if status == "confirmed" else None,
        invalidated_at=None,
        source_ids=("x",),
    )


def _row(
    sid: str,
    *,
    event_type: str = "created",
    status: str = "forming",
    revision: int = 1,
    payload: Any = None,
) -> tuple:
    return (
        1,
        sid,
        event_type,
        status,
        revision,
        payload if payload is not None else state_to_payload(_state()),
        1_700_000_000,
        1_700_000_000,
    )


# --------------------------------------------------------------------------- #
# store 层约定
# --------------------------------------------------------------------------- #


def test_store_never_commits() -> None:
    conn = FakeConn()
    store.append_events(
        conn,
        [StructureEvent("created", "bi:5:1", 1, state_to_payload(_state()), 1)],
    )
    assert conn.commits == 0, "store 提交了就等于替调用方做了事务决策"


def test_empty_batch_never_touches_storage() -> None:
    """每轮快照都 diff，绝大多数轮次无变化 —— 这时不该发任何 SQL。"""
    conn = FakeConn()
    assert store.append_events(conn, []) == 0
    assert conn.executed == []


# --------------------------------------------------------------------------- #
# append
# --------------------------------------------------------------------------- #


def test_append_writes_all_columns() -> None:
    conn = FakeConn()
    state = _state()
    events = diff_states({}, [state])
    assert store.append_events(conn, events) == 1

    sql, rows = conn.executed[0]
    columns = sql[sql.index("(") + 1 : sql.index(")")]
    assert len(columns.split(",")) == 6
    assert len(rows[0]) == 6
    # payload 必须能直接进 jsonb
    assert json.loads(rows[0][4])["id"] == state.id


def test_append_converts_epoch_ms_to_timestamp() -> None:
    """``StructureEvent.occurred_at`` 是毫秒，列是 timestamptz。"""
    conn = FakeConn()
    store.append_events(
        conn,
        [StructureEvent("created", "bi:5:1", 1, {}, 1_700_000_000_000)],
    )
    _sql, rows = conn.executed[0]
    assert rows[0][5] == 1_700_000_000_000
    assert "to_timestamp(" in conn.executed[0][0]


def test_append_swallows_db_error() -> None:
    """best-effort：写事件失败不该让快照构造失败。"""

    class Broken:
        def cursor(self) -> Any:
            raise RuntimeError("db down")

    assert (
        store.append_events(  # type: ignore[arg-type]
            Broken(), [StructureEvent("created", "bi:5:1", 1, {}, 1)]
        )
        == 0
    )


# --------------------------------------------------------------------------- #
# latest / current —— StructureState 的出口
# --------------------------------------------------------------------------- #


def test_latest_events_uses_distinct_on() -> None:
    """一次查询拿完，不要拉全表再在 Python 里挑。"""
    sid = structure_id_of("cn", "bi", 5, 1_700_000_000_000)
    conn = FakeConn(next_rows=[_row(sid)])
    out = store.latest_events(conn, [sid])
    assert "DISTINCT ON (structure_id)" in conn.executed[0][0]
    assert "ORDER BY structure_id, revision DESC" in conn.executed[0][0]
    assert out[sid].structure_id == sid


def test_latest_events_empty_ids_skips_query() -> None:
    conn = FakeConn()
    assert store.latest_events(conn, []) == {}
    assert store.latest_events(conn, ["", "  "]) == {}
    assert conn.executed == []


def test_current_states_closes_the_loop() -> None:
    """**R26 的核心断言**：``StructureState`` 从事件流派生回来了。

    此前该类型全仓零生产者零消费者（progress-log R24 勘察记录）。
    """
    original = _state(status="confirmed")
    sid = original.id
    conn = FakeConn(
        next_rows=[
            _row(
                sid,
                event_type="confirmed",
                status="confirmed",
                revision=2,
                payload=state_to_payload(original),
            )
        ]
    )

    states = store.current_states(conn, [sid])
    assert states[sid] == original


def test_absent_structure_is_absent_from_result() -> None:
    """首次运行时库里没这批结构 → 结果里没有它们 → ``diff_states`` 全记 created。"""
    conn = FakeConn(next_rows=[])
    previous = store.current_states(conn, ["bi:5:1"])
    assert previous == {}
    events = diff_states(previous, [_state()])
    assert [e.event_type for e in events] == ["created"]


def test_timeline_is_oldest_first() -> None:
    """时间线按 revision 升序（最早在前），不是倒序。"""
    sid = "bi:5:1"
    conn = FakeConn(next_rows=[_row(sid, revision=1), _row(sid, revision=2)])
    rows = store.timeline(conn, sid)
    assert "ORDER BY revision ASC" in conn.executed[0][0]
    assert [r["revision"] for r in rows] == [1, 2]


def test_timestamps_come_back_as_epoch_ms() -> None:
    """timestamptz 直接进 json 会炸（LLM 那轮踩过）。"""
    import datetime

    conn = FakeConn(
        next_rows=[
            (
                1,
                "bi:5:1",
                "created",
                "forming",
                1,
                state_to_payload(_state()),
                datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.UTC),
                datetime.datetime(2026, 9, 30, 12, 0, tzinfo=datetime.UTC),
            )
        ]
    )
    rows = store.timeline(conn, "bi:5:1")
    assert isinstance(rows[0]["occurred_at"], int)
    assert json.dumps(rows[0], ensure_ascii=False, default=str)


def test_row_with_null_payload_still_usable() -> None:
    """payload 为 NULL 的行不能让读取崩（老数据 / 手工插入）。"""
    # 列序：id, structure_id, event_type, status, revision, payload, occurred_at, created_at
    conn = FakeConn(
        next_rows=[(1, "bi:5:1", "updated", "forming", 3, None, 1_700_000_000, 1_700_000_000)]
    )
    out = store.latest_events(conn, ["bi:5:1"])
    assert out["bi:5:1"].revision == 3
    assert out["bi:5:1"].payload == {}
    # 派生的状态仍合法（有 id、有 status），不抛
    assert store.current_states(conn, ["bi:5:1"])["bi:5:1"].id == "bi:5:1"


def test_latest_events_degrades_to_empty() -> None:
    class Broken:
        def cursor(self) -> Any:
            raise RuntimeError("relation does not exist")

    assert store.latest_events(Broken(), ["bi:5:1"]) == {}  # type: ignore[arg-type]


def test_timeline_raises_instead_of_lying() -> None:
    """R27-2 改契约：``timeline`` 从「降级为空」改成「原样抛出」。

    这条断言原本和 ``latest_events`` 捆在一起。改契约的理由是**调用方变了**：

    - ``latest_events`` 的唯一消费者是 ``current_states`` → recorder，而 recorder
      自带 try/except，降级在那里；
    - ``timeline`` 的唯一消费者是 ``/api/dashboard/structure-events/timeline``，
      一个**读接口**。它若降级成空元组，HTTP 层就只能报
      ``available=true, count=0`` —— 把「DB 挂了」谎报成「没有事件」。前端无法
      区分这两者，而处置完全不同（前者该重试/告警）。

    降级义务上移到 HTTP 层（回 ``available=false`` + reason），这才是前端能据以
    决策的形状。见 ``tests/test_structure_event_routes.py``。
    """

    class Broken:
        def cursor(self) -> Any:
            raise RuntimeError("relation does not exist")

    with pytest.raises(RuntimeError, match="relation does not exist"):
        store.timeline(Broken(), "bi:5:1")  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [0, -5, 10_000])
def test_timeline_clamps_limit(limit: int) -> None:
    conn = FakeConn()
    store.timeline(conn, "bi:5:1", limit=limit)
    assert 1 <= conn.executed[0][1][1] <= 500
