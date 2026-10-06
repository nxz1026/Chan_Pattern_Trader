"""``cpt.application.structure_event_recorder`` 测试。

这是 A 股与加密**共用**的一条路径，所以三条降级纪律在这里一次测全：

1. **全程 best-effort**：任何一步失败都返回空元组，**不抛** —— 事件流是旁路，
   快照必须照常出图；
2. **提交义务在这里**（``cpt/storage/*`` 一律不 commit）—— 忘了就是静默丢数据，
   R23/R25 各栽过；
3. **无变化就不写**：空批次不发 SQL、不 commit。
"""

from __future__ import annotations

from typing import Any

import pytest
from cpt.application import structure_event_recorder as rec
from cpt.domain.models import Bi, Fractal


def _fractal(start: int = 1_700_000_000_000) -> Fractal:
    return Fractal(
        kind="top",
        level=5,
        bar_index=0,
        start_time=start,
        end_time=start + 86_400_000,
        high=10.0,
        low=9.0,
        source_ids=("a",),
    )


def _bi(start: int = 1_700_000_000_000) -> Bi:
    return Bi(
        level=5,
        direction=1,
        start_time=start,
        end_time=start + 86_400_000,
        high=10.0,
        low=9.0,
        source_ids=("a",),
        power_price=1.0,
        power_volume=1.0,
        length=5,
    )


class FakeConn:
    """记录调用序列的假连接。"""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.queries: list[str] = []
        self.events_written = 0

    def cursor(self) -> Any:
        conn = self

        class C:
            def __enter__(self) -> C:
                return self

            def __exit__(self, *args: Any) -> None:
                pass

            def execute(self, sql: str, params: tuple = ()) -> None:
                conn.queries.append(sql)

            def executemany(self, sql: str, rows: list[tuple]) -> None:
                conn.queries.append(sql)
                conn.events_written += len(rows)

            def fetchall(self) -> list[tuple]:
                return []

        return C()

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class PgError(Exception):
    """带 ``sqlstate`` 的假 PG 异常。

    真实的 psycopg 异常**都**带 ``sqlstate``（驱动从服务端错误报文里取），
    ``cpt.storage.structure_event_store.is_missing_table_error`` 就是靠这个
    属性判断「表不存在」还是「真 DB 故障」。裸 ``RuntimeError`` 不带该属性，
    用它当表缺失的替身会让夹具**不忠实于现实**，于是真故障被误判成迁移没跑。
    """

    def __init__(self, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        if sqlstate is not None:
            self.sqlstate = sqlstate


#: PG「表不存在」的 SQLSTATE
_UNDEFINED_TABLE = "42P01"


class NoTable(FakeConn):
    """表不存在（迁移没跑）。**读和写都失败**。"""

    def cursor(self) -> Any:
        raise PgError('relation "public.cpt_structure_event" does not exist', _UNDEFINED_TABLE)


class DbDown(FakeConn):
    """真 DB 故障：连不通 / 权限不足 / 语句本身有问题。

    用来锁住「读失败**不等于**无历史」这条新契约：这种故障下 recorder
    **不能**把全部结构当成新建重写一遍，所以本轮不产出事件。
    """

    def cursor(self) -> Any:
        raise PgError("could not connect to server: Connection refused", "08006")


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


def test_records_created_events_and_commits() -> None:
    """首次跑 → 全部 created，且**必须 commit**（store 层不提交）。"""
    conn = FakeConn()
    events = rec.record_structure_events(
        market="crypto", fractals=[_fractal()], bis=[_bi()], conn=conn
    )
    assert len(events) > 0
    assert {e.event_type for e in events} == {"created"}
    assert conn.commits == 1, "没 commit = 这次写入会被 close() 回滚（R23/R25 栽过两次）"


def test_second_run_with_no_change_writes_nothing() -> None:
    """第二轮结构没变 → 空批次 → **不发 INSERT、不 commit**。

    做法：先真跑一次拿到 structure_id 与其 payload，再让 ``current_states``
    查到完全一致的历史行。
    """
    seed = FakeConn()
    first = rec.record_structure_events(market="crypto", bis=[_bi()], conn=seed)
    assert first
    rows = [
        (
            1,
            e.structure_id,
            e.event_type,
            str(e.payload.get("status")),
            e.revision,
            e.payload,
            1_700_000_000_000,
            1_700_000_000_000,
        )
        for e in first
    ]

    class SeededConn(FakeConn):
        def cursor(self) -> Any:
            outer = self

            class C:
                def __enter__(self) -> C:
                    return self

                def __exit__(self, *args: Any) -> None:
                    pass

                def execute(self, sql: str, params: tuple = ()) -> None:
                    outer.queries.append(sql)

                def fetchall(self) -> list[tuple]:
                    return outer._rows

            return C()

    conn = SeededConn()
    conn._rows = rows  # noqa: SLF001
    again = rec.record_structure_events(market="crypto", bis=[_bi()], conn=conn)
    assert again == (), "结构没变却产生了事件"
    assert conn.commits == 0, "无变化时不该 commit"
    assert not any("INSERT" in q for q in conn.queries)


# --------------------------------------------------------------------------- #
# 降级纪律
# --------------------------------------------------------------------------- #


def test_no_structures_is_noop() -> None:
    conn = FakeConn()
    assert rec.record_structure_events(market="crypto", conn=conn) == ()
    assert conn.queries == [] and conn.commits == 0


def test_db_failure_never_raises() -> None:
    """连接炸了 → 返回空，**不抛**。快照必须照常出图。"""

    class Broken:
        def cursor(self) -> Any:
            raise RuntimeError("db down")

    assert rec.record_structure_events(market="crypto", bis=[_bi()], conn=Broken()) == ()


def test_insert_failure_still_returns_events() -> None:
    """表不存在（迁移没跑）→ **不抛，且仍返回事件**。

    刻意设计：``snapshot.events`` 回答「本次算出了什么变化」，
    与「能不能落库」是两件事。表不存在时「无历史」是**真的**（事件流本来就是
    空的），降级不算撒谎。代价是这批事件**没进事件流**，跨重启追溯里查不到。

    2026-10-06 补：同时锁住**连接被救回来**。表缺失会让读写都失败，PG 语义下
    事务已进入 aborted；不 rollback 的话，调用方（``a_share_snapshot``）传进来的
    共享连接后面十余处查询全部撞 ``current transaction is aborted``，整轮快照
    逐只降级 —— 一次「表没建」炸穿整张快照。
    """
    conn = NoTable()
    events = rec.record_structure_events(market="crypto", bis=[_bi()], conn=conn)
    assert events, "写库失败不该吞掉已经算出来的事件"
    assert {e.event_type for e in events} == {"created"}
    # 表缺失 ⇒ 写也失败 ⇒ 必须回滚传入的连接，不许留在 aborted 态。
    assert conn.rollbacks == 1, "写侧降级后必须回滚传入的共享连接"


def test_real_db_failure_does_not_fake_first_run() -> None:
    """真 DB 故障**不产出事件**，也不把全部结构当成新建重写。

    2026-10-06 改契约的核心：``current_states`` 从「降级成 ``{}``」改成「抛」，
    因为降级会让 recorder 的 ``previous`` 变空，``diff_states`` 于是把**每一个**
    结构都当成首次创建 —— 把「读失败」谎报成「首次」，事件流被一次 DB 抖动
    整体重写。

    对照 ``test_insert_failure_still_returns_events``：同样是 cursor 失败，
    **只因 sqlstate 不同，处置就相反**。表不存在（42P01）= 真的没历史，可降级；
    连不通（08006）= 不知道历史长什么样，不能降级。
    """
    conn = DbDown()
    events = rec.record_structure_events(market="crypto", bis=[_bi()], conn=conn)
    assert events == (), "真 DB 故障时不应谎报「本次全部结构都是新建」"
    assert conn.rollbacks == 1, "读失败把事务打成 aborted，必须回滚救连接"
    assert conn.events_written == 0, "读失败就不该往下走去写库"


def test_commit_failure_never_raises() -> None:
    class BadCommit(FakeConn):
        def commit(self) -> None:
            raise RuntimeError("commit failed")

    assert rec.record_structure_events(market="crypto", bis=[_bi()], conn=BadCommit()) == ()


# --------------------------------------------------------------------------- #
# 两条连接路径
# --------------------------------------------------------------------------- #


def test_passes_connection_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """给了 conn 就用它，且**不再自己开一条**。"""
    conn = FakeConn()
    opened: list[str] = []

    @rec.contextmanager
    def _spy(c: Any) -> Any:
        if c is None:
            opened.append("self-opened")
        yield conn

    monkeypatch.setattr(rec, "_connection", _spy)
    rec.record_structure_events(market="crypto", bis=[_bi()], conn=conn)
    assert conn.commits == 1
    assert opened == [], "给了 conn 就不该走自开分支"


def test_opens_own_connection_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """没给 conn（加密路径）→ 走 ``_connection(None)`` 的自开分支。"""
    conn = FakeConn()

    @rec.contextmanager
    def _self_open(_c: Any) -> Any:
        yield conn

    monkeypatch.setattr(rec, "_connection", _self_open)
    events = rec.record_structure_events(market="crypto", bis=[_bi()], conn=None)
    assert events
    assert conn.commits == 1
