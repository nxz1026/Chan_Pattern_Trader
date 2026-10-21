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
        self._fetched: list[tuple] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.executed.append((sql, params))
        self._conn.rows = list(self._conn.next_rows)
        self._fetched = list(self._conn.next_rows)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        self._conn.executed.append((sql, rows))
        self.rowcount = len(rows)
        self._conn.written.extend(rows)

    def fetchone(self) -> tuple | None:
        return self._fetched[0] if self._fetched else None

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
    sid = structure_id_of("cn", "600000", kind, 5, start)  # type: ignore[arg-type]
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


def _insert_call(conn: FakeConn) -> tuple[str, list[tuple]]:
    """取出 ``executemany`` 那次调用（前面还有序列自愈的探测查询）。"""
    for sql, payload in conn.executed:
        if "INSERT INTO public.cpt_structure_event" in sql:
            assert isinstance(payload, list)
            return sql, payload
    raise AssertionError("没有发出 INSERT：append_events 可能在序列探测处提前返回了")


def test_append_writes_all_columns() -> None:
    conn = FakeConn()
    state = _state()
    events = diff_states({}, [state])
    assert store.append_events(conn, events) == 1

    sql, rows = _insert_call(conn)
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
    sql, rows = _insert_call(conn)
    assert rows[0][5] == 1_700_000_000_000
    assert "to_timestamp(" in sql


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
    sid = structure_id_of("cn", "600000", "bi", 5, 1_700_000_000_000)
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


def test_latest_events_raises_instead_of_lying() -> None:
    """2026-10-06 改契约：``latest_events`` 从「降级成空」改成「原样抛出」。

    原来这条断言锁的是「降级成 ``{}``」。改掉的理由比 ``timeline`` 更硬：

    - recorder 传给 store 的**不是**它自己开的连接，而是调用方
      （``a_share_snapshot``）**共享**的那条。PG 语义下任何一条语句失败都会把
      整个事务打成 ``aborted``，而吞掉异常意味着这条连接带着 aborted 态交还给
      调用方 —— 它后面十余处查询全部撞 ``current transaction is aborted``，
      整轮快照逐只降级。一次读函数的降级炸穿了整张快照的构造。
    - 降级成 ``{}`` 还会让 recorder 的 ``previous`` 变空，``diff_states`` 于是
      把**全部**结构当成新建重写一遍 —— 「读失败」被当成了「首次」。

    降级义务仍在 recorder：它 catch 后照常返回算出的事件（快照不该因 DB 故障
    少显示一批变化），但**必须先把传入的连接 rollback 救回来**。
    见 ``test_structure_event_recorder.py``。
    """

    class Broken:
        def cursor(self) -> Any:
            raise RuntimeError("relation does not exist")

    with pytest.raises(RuntimeError, match="relation does not exist"):
        store.latest_events(Broken(), ["bi:5:1"])  # type: ignore[arg-type]

    # current_states 是它的薄封装，必须同进同退 —— 不能一边抛一边降级。
    with pytest.raises(RuntimeError, match="relation does not exist"):
        store.current_states(Broken(), ["bi:5:1"])  # type: ignore[arg-type]


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


# --------------------------------------------------------------------------- #
# ensure_id_sequence：序列落后于 max(id) 的自愈
# --------------------------------------------------------------------------- #
# 生产上真实撞过两次（2026-10-20 / 10-21）：
#   psycopg.errors.UniqueViolation: duplicate key value violates unique
#   constraint "cpt_structure_event_pkey" / DETAIL: Key (id)=(900008) already exists
# 根因是有代码带 id 显式写入（迁移 INSERT...SELECT id、测试 setval 回滚），
# 一旦超过序列当前值，后续每次 nextval 都撞主键，且**不会自愈**。


class SeqConn:
    """按 SQL 内容分发结果的桩：pg_sequences / max(id) / setval 各回各的。"""

    def __init__(
        self,
        *,
        last_value: int | None,
        max_id: int | None,
        sequence_exists: bool = True,
    ) -> None:
        self.last_value = last_value
        self.max_id = max_id
        self.sequence_exists = sequence_exists
        self.statements: list[tuple[str, Any]] = []

    def cursor(self) -> SeqCursor:
        return SeqCursor(self)


class SeqCursor:
    def __init__(self, conn: SeqConn) -> None:
        self._conn = conn
        self._result: tuple | None = None

    def __enter__(self) -> SeqCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.statements.append((sql, params))
        if "pg_sequences" in sql:
            # 序列不存在时 PG 返回零行；存在但无 SELECT 权限时 last_value 为 NULL
            self._result = (self._conn.last_value,) if self._conn.sequence_exists else None
        elif "max(id)" in sql:
            self._result = (self._conn.max_id,)
        elif "setval" in sql:
            self._result = (params[0],)
        else:  # pragma: no cover — 出现未预期的 SQL 就红
            raise AssertionError(f"未预期的 SQL: {sql}")

    def fetchone(self) -> tuple | None:
        return self._result


def test_ensure_id_sequence_pushes_when_lagging() -> None:
    """last_value < max(id) ⇒ setval 到 max(id)，返回值是新值。"""
    conn = SeqConn(last_value=900_009, max_id=903_138)
    assert store.ensure_id_sequence(conn) == 903_138  # type: ignore[arg-type]

    setval = [(sql, p) for sql, p in conn.statements if "setval" in sql]
    assert len(setval) == 1
    sql, params = setval[0]
    assert "public.cpt_structure_event_id_seq" in sql
    assert params == (903_138,)
    # 第三参必须是 true（下一个 nextval 返回 max+1），写成 false 会重发一个已用号
    assert ", true)" in sql


def test_ensure_id_sequence_is_noop_when_already_ahead() -> None:
    """已对齐就不写 —— 不然每批都去拿序列上的锁。"""
    conn = SeqConn(last_value=903_138, max_id=903_138)
    assert store.ensure_id_sequence(conn) == 0  # type: ignore[arg-type]
    assert not [s for s, _ in conn.statements if "setval" in s]


def test_ensure_id_sequence_noop_on_empty_table() -> None:
    """空表没有 max(id)，别 setval —— 也从不用 NULL 调 setval。"""
    conn = SeqConn(last_value=1, max_id=None)
    assert store.ensure_id_sequence(conn) == 0  # type: ignore[arg-type]
    assert not [s for s, _ in conn.statements if "setval" in s]


def test_ensure_id_sequence_ignores_missing_sequence() -> None:
    """序列不存在（迁移没跑）时早退，不自作主张建 —— 那不是本函数该修的。"""
    conn = SeqConn(last_value=None, max_id=100, sequence_exists=False)
    assert store.ensure_id_sequence(conn) == 0  # type: ignore[arg-type]
    assert not [s for s, _ in conn.statements if "setval" in s]


def test_ensure_id_sequence_treats_null_last_value_as_lagging() -> None:
    """有 USAGE 无 SELECT 权限时 pg_sequences.last_value 是 NULL。

    这时按「可能落后」处理：多做一次无效 setval 的代价，远小于漏修后每次
    跑批都 UniqueViolation。
    """
    conn = SeqConn(last_value=None, max_id=903_138)
    assert store.ensure_id_sequence(conn) == 903_138  # type: ignore[arg-type]
    assert [s for s, _ in conn.statements if "setval" in s]


def test_append_events_calls_ensure_before_insert() -> None:
    """自愈必须在 INSERT 之前 —— 撞了再修，这批已经废了还得重跑。"""
    conn = FakeConn()
    store.append_events(conn, [StructureEvent("created", "bi:5:1", 1, {}, 1_700_000_000_000)])
    statements = [sql for sql, _ in conn.executed]
    seq_at = next(i for i, s in enumerate(statements) if "pg_sequences" in s)
    insert_at = next(i for i, s in enumerate(statements) if "INSERT INTO" in s)
    assert seq_at < insert_at


def test_append_events_does_not_commit() -> None:
    """store 层不 commit —— 事务边界归调用方（序列自愈也不能例外）。"""
    conn = FakeConn()
    store.append_events(conn, [StructureEvent("created", "bi:5:1", 1, {}, 1_700_000_000_000)])
    assert conn.commits == 0
