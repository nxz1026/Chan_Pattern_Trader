"""``run_metric_store`` 的**失败语义与 prune 窗口**（R45 补）。

## 为什么现在才补

R45 storage 复盘把这个 store 改了三处（``prune`` 接进 cron、失败语义收紧），
却**全仓没有一个测试引用它** —— 实测::

    $ grep -rl run_metric_store tests/*.py | wc -l
    0

而 ``prune`` 已经在**生产 cron 里每天跑**（``04:10 UTC``，
``deploy/cron/run-metric-prune-daily.sh``）**删行**。
一个每天在生产上 ``DELETE`` 的函数，零测试覆盖 —— 本轮全量扫描时才撞见。

## 本文件钉住什么

1. **失败 ≠ 没东西可删**：``prune`` 失败必须抛，不能返回 0
   （返回 0 会让日志打出「已删除 0 行」，看着像成功的空转）。
2. **按 kind 过滤**：``cpt_run_metric`` 一张表混了 ``KIND_RUN`` 与
   ``KIND_INSPECTION``，两者窗口不同（cron 里配 90 天 / 30 天）。
   prune **不传 kinds 时删全部**，而巡检行是每天状态比对的依据。
3. **不 commit**：store 层一律不 commit（见 ``cpt/storage/__init__.py``），
   漏了就是静默回滚。

全程离线：用假连接，不碰 DB。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpt.storage import run_metric_store as rms  # noqa: E402


class _Cur:
    rowcount = 0

    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.sql = ""
        self.params: tuple = ()

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.sql = " ".join(sql.split())
        self.params = params
        self.conn.calls.append(self.sql)
        self.rowcount = self.conn.deleted
        if self.conn.raise_on_delete and "DELETE" in self.sql.upper():
            raise self.conn.raise_on_delete


class _Conn:
    def __init__(self, *, raise_on_delete: Exception | None = None, deleted: int = 0) -> None:
        self.calls: list[str] = []
        self.raise_on_delete = raise_on_delete
        self.deleted = deleted
        self.commits = 0
        self._cur = _Cur(self)

    def cursor(self) -> _Cur:
        return self._cur

    def commit(self) -> None:
        self.commits += 1


def test_prune_returns_deleted_count() -> None:
    conn = _Conn(deleted=7)
    assert rms.prune(conn, keep_days=90, kinds=[rms.KIND_RUN]) == 7
    assert any("DELETE" in c for c in conn.calls)


def test_prune_filters_by_kind() -> None:
    """kind 过滤必须体现在**参数**里。

    ⚠️ 第一版断言「两个 kind 的 SQL 文本应当不同」—— **这是错的**：
    实现用的是同一个 DELETE 模板，``kind = ANY(%s)`` 是同一条语句，
    差别只在**绑定的参数**（kind 列表）和 ``keep_days``。
    ⇒ 断言必须打在参数上；打在 SQL 文本上会把「正确实现」判成「过滤没生效」。
    """
    conn = _Conn()
    rms.prune(conn, keep_days=90, kinds=[rms.KIND_RUN])
    run_params = conn._cur.params
    conn2 = _Conn()
    rms.prune(conn2, keep_days=30, kinds=[rms.KIND_INSPECTION])
    insp_params = conn2._cur.params

    assert "kind = ANY" in conn._cur.sql, "SQL 里根本没有 kind 条件"
    assert [rms.KIND_RUN] in run_params
    assert [rms.KIND_INSPECTION] in insp_params
    assert 90 in run_params and 30 in insp_params
    assert rms.KIND_INSPECTION not in run_params, "run 那次混进了 inspection"


def test_prune_raises_on_failure_not_returning_zero() -> None:
    """⚠️ 核心：失败必须抛。

    返回 0 会让 cron 日志打出「已删除 run 行 0」——
    **看着像成功的空转，实际是 DELETE 失败**。这正是 R45 storage 复盘
    反复强调的那条：失败与「没东西可删」不能同码。
    """
    conn = _Conn(raise_on_delete=RuntimeError("connection lost"))
    # 实测：实现把底层异常**包成** RunMetricError 再抛（不是原样抛）
    with pytest.raises(rms.RunMetricError):
        rms.prune(conn, keep_days=90, kinds=[rms.KIND_RUN])


def test_prune_never_commits() -> None:
    """store 层不 commit —— 事务边界归调用方。"""
    conn = _Conn()
    rms.prune(conn, keep_days=90, kinds=[rms.KIND_RUN])
    assert conn.commits == 0, "prune 自己 commit 了 —— 违反 store 层约定"


def test_kind_constants_are_distinct() -> None:
    assert rms.KIND_RUN != rms.KIND_INSPECTION


# --------------------------------------------------------------------------- #
# R52：读失败不许冒充「没有上一轮」；批量写入的返回行数必须诚实
# --------------------------------------------------------------------------- #


class _AbortingCursor(_Cur):
    def execute(self, sql: str, params: tuple = ()) -> None:
        super().execute(sql, params)
        raise RuntimeError("simulated PG statement failure")

    def fetchall(self) -> list:
        raise RuntimeError("simulated PG statement failure")


class _AbortingConn(_Conn):
    def __init__(self) -> None:
        super().__init__()
        self.rollbacks = 0

    def cursor(self) -> _AbortingCursor:
        return _AbortingCursor(self)

    def rollback(self) -> None:
        self.rollbacks += 1


def test_latest_run_fingerprint_raises_on_read_failure() -> None:
    """**读失败 ≠ 从没跑过**。

    原来 ``latest_run_fingerprint`` catch 住异常返回 ``None``，而 ``None`` 的
    正式含义是「这个标的从没跑过」（函数自己的 docstring 就在讲怎么区分这两
    种情况）⇒ 库读不到被冒充成「第一轮」，:func:`explain_cause` 于是拿不到前
    值、归因静默留空。调用方 ``structure_event_recorder`` 本来就整段包在
    try/except 里，抛是安全的。
    """
    with pytest.raises(rms.RunMetricError, match="读取上一轮指纹失败"):
        rms.latest_run_fingerprint(_AbortingConn(), market="a_share", symbol="600519")


def test_latest_run_fingerprint_returns_none_when_no_rows() -> None:
    """对照组：真的没有上一轮 → ``None``，不抛。

    这两条一起，把「读不到」与「确实没有」钉成两种不同结果。
    """

    class _EmptyCursor(_Cur):
        def execute(self, sql: str, params: tuple = ()) -> None:
            super().execute(sql, params)

        def fetchall(self) -> list:
            return []

    conn = _Conn()
    conn.cursor = lambda: _EmptyCursor(conn)  # type: ignore[method-assign]
    assert rms.latest_run_fingerprint(conn, market="a_share", symbol="600519") is None


class _PartialFailCursor(_Cur):
    """第 ``fail_at`` 条 INSERT 起抛 —— 模拟批量写到一半失败。"""

    def __init__(self, conn: _Conn, fail_at: int) -> None:
        super().__init__(conn)
        self.inserts = 0
        self.fail_at = fail_at

    def execute(self, sql: str, params: tuple = ()) -> None:
        super().execute(sql, params)
        if "INSERT" in sql.upper():
            if self.inserts >= self.fail_at:
                raise RuntimeError("simulated PG statement failure")
            self.inserts += 1


def test_append_metrics_returns_rows_actually_inserted() -> None:
    """中途失败的返回值**不许**撒谎。

    第 n 行失败时前 n-1 行已经在**调用方的事务里**了（store 层不 commit），
    返回 0 会让人以为「一行都没写」→ 重跑整批 → 前 n-1 行变重复行。
    """
    conn = _Conn()
    conn.cursor = lambda: _PartialFailCursor(conn, 2)  # type: ignore[method-assign]
    rows = [{"observed_at": i, "market": "a_share", "symbol": "600519"} for i in range(5)]
    assert rms.append_metrics(conn, rows) == 2
    assert conn.commits == 0


def test_append_metrics_closes_cursor_as_context_manager() -> None:
    """游标必须进上下文管理器 —— 原来 ``conn.cursor()`` 拿到就丢，一路泄漏到 GC。"""
    closed: list[bool] = []

    class _TrackingCursor(_Cur):
        def __exit__(self, *exc: object) -> None:
            closed.append(True)
            return None

    conn = _Conn()
    conn.cursor = lambda: _TrackingCursor(conn)  # type: ignore[method-assign]
    rms.append_metrics(conn, [{"observed_at": 1, "market": "a_share", "symbol": "600519"}])
    assert closed == [True]
