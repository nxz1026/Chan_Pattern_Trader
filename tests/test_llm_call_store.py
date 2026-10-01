"""R25 ``cpt.storage.llm_call_store`` 测试。

**不连库** —— 用一个按列名投影的假 conn，验证 SQL 形状、参数顺序与「重复提交
不报错」这些**契约**，而不是 PG 的行为（PG 行为由 §6 的 oracle 部署验证兜底）。

为什么值得单独测：这张表是 R25 唯一的持久化出口，而
``architecture.md`` §4.1 约束 4 要求「每次调用的模型与 token 都落盘」——
token 列曾经建了却没人写（写路径只传了 result_text），这轮才补上。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from cpt.storage.llm_call_store import (
    STATUS_ERROR,
    STATUS_INTERRUPTED,
    STATUS_OK,
    STATUS_QUEUED,
    STATUS_RATE_LIMITED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    call_row,
    enqueue_call,
    finish_call,
    mark_interrupted,
    recent_calls,
    request_hash,
)

#: 迁移 SQL 路径。状态枚举拿它当事实源 —— 见下面那条测试的说明。
MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "migrations"
    / "2026-10-03_r25_llm_call.sql"
)


class FakeCursor:
    """记录 SQL 与参数，并按脚本返回行。

    ``rowcount`` 挂在 **cursor** 上（psycopg 就是这样），不是 conn。
    """

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self.rowcount = 0

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.executed.append((sql, params))
        if "INSERT" in sql:
            self.rowcount = 1 if self._conn.insert_allowed else 0
        else:
            self.rowcount = 1

    def fetchall(self) -> list[tuple]:
        return list(self._conn.rows)

    def fetchone(self) -> tuple | None:
        return self._conn.rows[0] if self._conn.rows else None


class FakeConn:
    def __init__(self, rows: list[tuple] | None = None, insert_allowed: bool = True) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.rows = rows or []
        self.insert_allowed = insert_allowed

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


# --------------------------------------------------------------------------- #
# 列与状态枚举
# --------------------------------------------------------------------------- #


def test_status_vocabulary_matches_migration_check_constraint() -> None:
    """状态枚举必须与迁移 SQL 里的 CHECK 约束**逐字一致**。

    枚举写错一个值，插入就被 DB 拒掉 —— 而这层是 best-effort，错误会被吞掉，
    表现是「LLM 一直在跑但永远不出结果」。所以拿迁移 SQL 当事实源来对。
    """
    with open(MIGRATION_PATH, encoding="utf-8") as handle:  # noqa: PTH123
        sql = handle.read()
    check = re.search(r"status\s+text[^,]*CHECK\s*\(status IN \(([^)]*)\)\)", sql, re.S)
    assert check, "迁移 SQL 里的 status CHECK 约束没找到 —— 迁移被改过？"
    declared = set(re.findall(r"'([a-z_]+)'", check.group(1)))
    ours = {
        STATUS_QUEUED,
        STATUS_RUNNING,
        STATUS_OK,
        STATUS_ERROR,
        STATUS_RATE_LIMITED,
        STATUS_INTERRUPTED,
    }
    assert ours == declared, f"枚举与 CHECK 不一致：多 {ours - declared} / 少 {declared - ours}"


def test_status_vocabularies_agree() -> None:
    """**llm 层 / storage 层 / 迁移 SQL 三份状态词汇必须一致。**

    为什么不是让 llm 直接 import storage：`.importlinter` 的
    ``llm-does-not-leak-into-storage`` 禁止 llm → storage（低层）。这条契约
    立完的当天就因为「从 storage 导入 STATUS_* 来消 vulture 告警」被违反过，
    CI 报 ``BROKEN``。所以跨层共享走**测试**而不是 import。

    这条测试就是那个「耦合机制」：谁改了枚举、忘了同步另外两份，这里会红。
    """
    from cpt.llm import queue as llm_queue
    from cpt.storage import llm_call_store as store_mod

    pairs = [
        ("QUEUED", llm_queue.STATUS_QUEUED, store_mod.STATUS_QUEUED),
        ("RUNNING", llm_queue.STATUS_RUNNING, store_mod.STATUS_RUNNING),
        ("OK", llm_queue.STATUS_OK, store_mod.STATUS_OK),
        ("ERROR", llm_queue.STATUS_ERROR, store_mod.STATUS_ERROR),
        ("RATE_LIMITED", llm_queue.STATUS_RATE_LIMITED, store_mod.STATUS_RATE_LIMITED),
        ("INTERRUPTED", llm_queue.STATUS_INTERRUPTED, store_mod.STATUS_INTERRUPTED),
    ]
    for name, from_llm, from_store in pairs:
        assert from_llm == from_store, f"{name} 两层不一致：llm={from_llm!r} storage={from_store!r}"

    # 而且 storage 的 TERMINAL_STATUSES 不能把 rate_limited 算进去
    assert llm_queue.STATUS_RATE_LIMITED not in store_mod.TERMINAL_STATUSES


def test_rate_limited_is_not_terminal_but_interrupted_is_not_error() -> None:
    """两个容易混的状态，各有各的语义：

    - ``rate_limited`` **不是终态** —— 它还会退避重入，所以不能写
      ``finished_at``（否则 UI 会以为这次调用已经结束）；
    - ``interrupted`` **是终态**（进程重启了，它不会再变），但它**不是
      ``error``** —— 混进 error 会让看板天天报红，而它其实是正常中断。
    """
    assert STATUS_RATE_LIMITED not in TERMINAL_STATUSES, "限流还在重试，不该算终态"
    assert STATUS_INTERRUPTED in TERMINAL_STATUSES, "进程重启后它不会再变，是终态"
    assert STATUS_INTERRUPTED != STATUS_ERROR
    assert STATUS_RATE_LIMITED != STATUS_ERROR


# --------------------------------------------------------------------------- #
# request_hash
# --------------------------------------------------------------------------- #


def test_request_hash_is_stable_and_purpose_scoped() -> None:
    a = request_hash("explain_structure", "sys", "user")
    b = request_hash("explain_structure", "sys", "  user\n")
    assert a == b, "首尾空白不该产生不同的 hash（否则同一提示词重复入队）"
    assert request_hash("other", "sys", "user") != a, "不同 purpose 必须分开"


# --------------------------------------------------------------------------- #
# enqueue
# --------------------------------------------------------------------------- #


def test_enqueue_writes_all_twelve_columns() -> None:
    conn = FakeConn()
    row = call_row(purpose="explain_structure", subject_id="bi:1", request_hash_value="h")
    assert enqueue_call(conn, row) is True

    sql, params = conn.executed[0]
    columns = sql[sql.index("(") + 1 : sql.index(")")]
    assert len(columns.split(",")) == 12, "R25 的表是 12 列"
    assert len(params) == 12
    # 重复提交靠部分唯一索引 + DO NOTHING，不是靠先 SELECT
    assert "ON CONFLICT (purpose, request_hash)" in sql
    assert "DO NOTHING" in sql


def test_enqueue_reports_false_when_conflict() -> None:
    """同一提示词在途/已成功时，插入被约束挡掉 → 返回 False 而不是抛。"""
    conn = FakeConn(insert_allowed=False)
    row = call_row(purpose="explain_structure", subject_id="", request_hash_value="h")
    assert enqueue_call(conn, row) is False


def test_enqueue_swallows_db_error() -> None:
    """落库失败不能让 HTTP 500 —— LLM 是旁路。"""

    class Broken:
        def cursor(self) -> Any:
            raise RuntimeError("db down")

    row = call_row(purpose="explain_structure", subject_id="", request_hash_value="h")
    assert enqueue_call(Broken(), row) is False  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# finish —— token / model 必须真的写进去
# --------------------------------------------------------------------------- #


def test_finish_writes_model_and_tokens() -> None:
    """**回归守卫**：token 列曾建了却没人写（写路径只传 result_text）。

    `architecture.md` §4.1 约束 4 要求模型与 token 可审计，所以这里断言
    UPDATE 语句里确实带上了这两列与对应参数。
    """
    conn = FakeConn()
    finish_call(
        conn,
        "c1",
        status=STATUS_OK,
        result_text="解释",
        model="agnes-3.0-flash",
        prompt_tokens=120,
        completion_tokens=88,
    )
    sql, params = conn.executed[0]
    for column in ("model", "prompt_tokens", "completion_tokens", "finished_at"):
        assert column in sql, f"UPDATE 缺 {column}"
    assert params[0] == STATUS_OK
    assert params[1] == "解释"
    assert params[2] is None, "ok 时 error_text 应为 NULL"
    assert params[3] == "agnes-3.0-flash"
    assert params[4] == 120
    assert params[5] == 88
    assert params[6] is not None, "终态必须写 finished_at"
    assert params[-1] == "c1"


def test_finish_sets_error_text_only_for_non_ok() -> None:
    conn = FakeConn()
    finish_call(conn, "c1", status=STATUS_ERROR, error_text="boom")
    _sql, params = conn.executed[0]
    assert params[1] is None, "失败时 result_text 应为 NULL"
    assert params[2] == "boom"
    assert params[6] is not None


# --------------------------------------------------------------------------- #
# mark_interrupted
# --------------------------------------------------------------------------- #


def test_mark_interrupted_only_touches_in_flight() -> None:
    conn = FakeConn()
    assert mark_interrupted(conn) == 1
    sql, params = conn.executed[0]
    assert "status = 'interrupted'" in sql
    assert "WHERE status IN ('queued', 'running')" in sql, "不能碰已终态的行"
    assert params


def test_mark_interrupted_respects_watermark() -> None:
    """**回归守卫**：带水位线时**不能误伤本进程刚入队的行**。

    实测事故：`_bootstrap()`（里面调 ``mark_interrupted``）恰好发生在
    ``enqueue_call`` **之后** —— 无差别清扫把刚写的行标成了 interrupted，
    现象是「提交返回 queued，5 秒后变 process_restarted / result_text 为空」。

    水位线 = 本进程启动时间，早于它的才是上一进程留下的孤儿。
    """
    from datetime import UTC, datetime, timedelta

    conn = FakeConn()
    mark_interrupted(conn, before=datetime.now(UTC) - timedelta(seconds=1))
    sql, params = conn.executed[0]
    assert "created_at < %s" in sql, "没带水位线条件 = 会清掉自己的行"
    assert isinstance(params[-1], datetime)
    assert params[0].tzinfo is not None, "时间戳必须是带时区的（timestamptz）"


# --------------------------------------------------------------------------- #
# 事务边界：application 层必须 commit
# --------------------------------------------------------------------------- #


class CommitCountingConn(FakeConn):
    """记录 commit 次数的假连接。"""

    def __init__(self) -> None:
        super().__init__()
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def test_store_never_commits_itself() -> None:
    """store 层**故意不 commit** —— 多个写入要能共享一个事务。

    所以「有没有提交」的责任全在调用方，这就是下面那条测试存在的意义。
    """
    conn = CommitCountingConn()
    enqueue_call(conn, call_row(purpose="p", subject_id="", request_hash_value="h"))
    finish_call(conn, "c1", status=STATUS_OK, result_text="x")
    mark_interrupted(conn)
    assert conn.commits == 0, "store 层提交了就等于替调用方做了事务决策"


def test_app_layer_always_commits() -> None:
    """**回归守卫（R23 与 R25 各漏过一次的那种 bug）**。

    两次事故长得一模一样：函数正常返回、HTTP 200、日志零告警，**但数据不在** ——
    因为 ``cpt/storage/*`` 刻意不 commit，而 application 层忘了提交，连接一关
    就回滚：

    - R23：``app.py::_persist_run`` 少一行 commit → ``cpt_dashboard_run`` 0 行
    - R25：``llm_cases.explain_structure`` 少一次 → 提交返回 queued 但表里没行；
      ``llm_cases.on_llm_status`` 又少一次 → 状态更新也回滚

    所以 application 层的每个写调用点都必须经过 ``llm_cases._write``。这里
    直接对着源码断言「不存在裸的写调用」——比逐个 mock 更有意义，因为它防的是
    **将来新加的**写调用点。
    """
    import inspect

    from cpt.application import llm_cases

    source = inspect.getsource(llm_cases)
    bare_calls = [
        name
        for name in ("enqueue_call(conn", "finish_call(\n", "mark_interrupted(client")
        if name in source and f"_write(conn, {name.split('(')[0]}" not in source
    ]
    assert not bare_calls, f"这些写调用没走 _write（会静默回滚）: {bare_calls}"

    # 并且 _write 本身确实会 commit
    conn = CommitCountingConn()
    llm_cases._write(
        conn,
        enqueue_call,
        call_row(  # noqa: SLF001
            purpose="p", subject_id="", request_hash_value="h"
        ),
    )
    assert conn.commits == 1


def test_write_passes_the_store_function_through_as_first_arg() -> None:
    """**回归守卫（真机部署抓到的第二个 R25 bug）**。

    上一版把 ``finish_call(conn, ...)`` 机械替换成 ``_write(conn, ...)``，**忘了把
    ``finish_call`` 作为参数加回去** —— 于是 ``_write`` 把 ``call_id``（一个 str）
    当函数调用，抛 ``'str' object is not callable``。

    现场表现极隐蔽：提交返回 ``queued``（入队那步是对的），但状态永远停在
    ``queued``，因为 worker 的每一次 ``on_status`` 落库都失败。只有 journalctl
    里的 ``LLM 状态落库失败 ... 'str' object is not callable`` 能看出问题。

    所以这里**真的调一遍** ``_write`` —— 上一条测试只查「有没有裸调用」，
    查不出「参数对不对」。
    """
    from cpt.application import llm_cases

    conn = CommitCountingConn()
    llm_cases._write(  # noqa: SLF001
        conn, finish_call, "c1", status=STATUS_OK, result_text="x"
    )
    sql, params = conn.executed[0]
    assert "UPDATE public.cpt_llm_call" in sql
    assert params[-1] == "c1"
    assert conn.commits == 1


def test_recent_calls_is_newest_first_and_capped() -> None:
    conn = FakeConn(rows=[("c1",), ("c2",)])
    recent_calls(conn, limit=5)
    sql, params = conn.executed[0]
    assert "ORDER BY created_at DESC" in sql
    assert params == (5,)


def test_recent_calls_filters_by_subject() -> None:
    conn = FakeConn()
    recent_calls(conn, limit=3, subject_id="bi:1")
    sql, params = conn.executed[0]
    assert "WHERE subject_id = %s" in sql
    assert params == ("bi:1", 3)


def test_recent_calls_degrades_to_empty() -> None:
    """表不存在（迁移没跑）时返回空元组，不抛 —— 面板显示「暂无」而不是 500。"""

    class Broken:
        def cursor(self) -> Any:
            raise RuntimeError("relation does not exist")

    assert recent_calls(Broken(), limit=5) == ()  # type: ignore[arg-type]


def test_time_columns_come_back_as_epoch_ms() -> None:
    """**回归守卫（真机部署抓到的第四个 bug）**。

    PG 的 ``timestamptz`` 直接进 ``json.dumps`` 会炸，路由回
    ``500 payload is not JSON-safe``。口径与 R21 ``signal_event_store`` 一致：
    出参统一 Unix 毫秒。

    所以这条断言**真的 json.dumps 一次** —— 只断言「是 int」不够，
    漏掉别的不可序列化对象照样会 500。
    """
    import json
    from datetime import UTC, datetime

    from cpt.storage.llm_call_store import _COLUMNS

    row = tuple(
        datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
        if name in ("created_at", "finished_at")
        else f"v{i}"
        for i, name in enumerate(_COLUMNS)
    )
    conn = FakeConn(rows=[row])
    calls = recent_calls(conn, limit=5)
    assert calls, "应该投影出一行"
    payload = calls[0]
    assert isinstance(payload["created_at"], int)
    assert isinstance(payload["finished_at"], int)
    # 真正序列化一次 —— 这才是当初 500 的地方
    assert json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("limit", [0, -1, 10_000])
def test_recent_calls_clamps_limit(limit: int) -> None:
    conn = FakeConn()
    recent_calls(conn, limit=limit)
    _sql, params = conn.executed[0]
    assert 1 <= params[0] <= 200
