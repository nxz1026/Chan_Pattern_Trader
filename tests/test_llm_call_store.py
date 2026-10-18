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
    LLMCallError,
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


def test_enqueue_raises_on_db_error() -> None:
    """落库失败**抛**，不返回 ``False``（R45 改）。

    原来这里断言「落库失败不能让 HTTP 500 —— LLM 是旁路」并检查
    ``enqueue_call(...) is False``。诉求没错，**落点错了**：

    - store 返回 ``False`` 与「重复提交」共用同一个值，于是调用方
      ``explain_structure`` 对用户说「你已经问过了」，而真相是
      **一条都没写、LLM 从未被调用**（本模块头的审计约束被静默违反）；
    - 「别 500」的正确落点是**调用方**：
      ``explain_structure`` catch ``LLMCallError`` → rollback →
      回 ``{"available": false, "reason": "llm_audit_write_failed"}``，
      HTTP 照样 200。

    ⚠️ 外层的 ``llm_cases._write``（= ``fn()`` + ``commit()``）救不了：
    实测 PostgreSQL 18.6，事务在 aborted 态下 ``COMMIT`` **不抛**、等于
    ``ROLLBACK``，于是假 ``False`` 原样返回。那道防线只在 store 抛异常时有效。

    回归见 ``tests/test_llm_store_write_semantics.py``。
    """

    class Broken:
        def cursor(self) -> Any:
            raise RuntimeError("db down")

    row = call_row(purpose="explain_structure", subject_id="", request_hash_value="h")
    with pytest.raises(LLMCallError):
        enqueue_call(Broken(), row)  # type: ignore[arg-type]


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


def test_finish_raises_on_db_error() -> None:
    """R52：**写失败一律抛**（与 ``enqueue_call``、与本类的契约一致）。

    ``finish_call`` 原来只记一条 warning 就返回 —— 于是「模型已经调过、token
    已经花掉」这件事在审计表里消失了，调用方还当成功。类 docstring 明写
    「**写失败一律抛**」，实现与自己的契约矛盾。
    """

    class _BrokenConn:
        def cursor(self) -> Any:
            class _C:
                def __enter__(self) -> Any:
                    return self

                def __exit__(self, *a: Any) -> bool:
                    return False

                def execute(self, *a: Any, **k: Any) -> None:
                    raise RuntimeError("simulated PG statement failure")

            return _C()

    with pytest.raises(LLMCallError, match="c1"):
        finish_call(_BrokenConn(), "c1", status=STATUS_OK, result_text="x")


def test_finish_sql_guards_against_rewriting_terminal_rows() -> None:
    """审计 M16：晚到回调不能把已终态（如 interrupted）的行改回 ``ok``。

    多实例 / 滚动重启时 B 进程先标 ``interrupted``，A 进程的晚到 callback
    再走一次 ``finish_call`` —— 无守卫时把行拉回 ``ok``，看板把一次没跑完的
    调用显示成成功。守卫 = ``AND status NOT IN (终态)``（0 行更新）。
    """
    conn = FakeConn()
    finish_call(conn, "c1", status=STATUS_OK, result_text="late")
    sql, _params = conn.executed[0]
    assert "AND status NOT IN ('ok', 'error', 'interrupted')" in sql


# --------------------------------------------------------------------------- #
# mark_interrupted
# --------------------------------------------------------------------------- #


def test_mark_interrupted_only_touches_in_flight() -> None:
    conn = FakeConn()
    assert mark_interrupted(conn) == 1
    sql, params = conn.executed[0]
    assert "status = 'interrupted'" in sql
    # 审计 M15：清扫集合必须含 ``rate_limited`` —— 它真实落库但退避只在内存，
    # 进程被杀后原来会永停 rate_limited、finished_at 恒 NULL。
    assert "WHERE status IN ('queued', 'running', 'rate_limited')" in sql, "不能碰已终态的行"
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


def test_recent_calls_raises_on_db_error() -> None:
    """表不存在（迁移没跑）时**抛**，不返回空元组（R45 改）。

    原来断言「返回空元组，不抛 —— 面板显示『暂无』而不是 500」。诉求不错，
    但那样面板拿到的是 ``{"available": true, "count": 0}``：
    **把「库读不到」冒充成「确实没有调用过」**，UI 显示一个看起来正常的
    空列表，排查的人完全看不出是 DB 挂了。

    正确落点同样是**调用方**：``llm_cases.list_calls`` catch
    ``LLMCallError`` → rollback → 回 ``available: false`` +
    ``reason: llm_call_history_unavailable``，HTTP 照样 200。
    """

    class Broken:
        def cursor(self) -> Any:
            raise RuntimeError("relation does not exist")

    with pytest.raises(LLMCallError):
        recent_calls(Broken(), limit=5)  # type: ignore[arg-type]


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


# ── R67 P2-14：find_conflicting_call 的条件必须与唯一索引同源 ────────


def test_find_conflicting_call_uses_the_unique_index_predicate() -> None:
    """查询条件必须**逐字**等于 ``idx_cpt_llm_call_request_hash`` 的谓词。

    为什么值得钉死：这个函数的整个存在理由就是「去重窗口是**永久**的」，
    而它要替代的旧实现（``recent_calls(limit=50)`` + Python 里比 hash）恰好
    只看得到最近 50 行。条件一旦与索引漂移，就退化成
    「真冲突了但查不到」（回幽灵 call_id）或「查到了但没冲突」（回错 id），
    两种都静默。

    索引谓词的事实源是**迁移 SQL**，所以这里从 SQL 文件正则抽出谓词与代码比
    —— 不抄一份常量进测试，否则测试自己就会漂。
    """
    from cpt.storage.llm_call_store import find_conflicting_call

    conn = FakeConn(rows=[("c-real",)])
    found = find_conflicting_call(conn, purpose="summarize_recommendation", request_hash_value="D")
    assert found == "c-real"
    sql, params = conn.executed[0]
    assert params == ("summarize_recommendation", "D"), (
        "purpose 必须进 WHERE，不能只在 Python 里过滤"
    )
    assert "purpose = %s" in sql
    assert "request_hash = %s" in sql
    assert "status IN ('queued','running','ok')" in sql, (
        "状态条件丢了 ⇒ 会捞到 error/interrupted 的历史行"
    )
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"idx_cpt_llm_call_request_hash.*?WHERE status IN \(([^)]*)\)",
        migration,
        re.S,
    )
    assert match, "迁移 SQL 里找不到那条唯一索引 —— 事实源变了，本测试要跟着改"
    # ⚠️ 比**状态集合**而不是字符串：迁移里写 `'queued', 'running', 'ok'`（分隔符带
    # 空格），代码常量里是 `'queued','running','ok'`。SQL 里逗号后的空格不改变语义，
    # 拿它当「漂移」判据只会逼出一次无意义的字符串对齐（第一版就是这么红的）。
    # 真正要保证的是**同一批状态**，那才是决定「谁挡住了我」的东西。
    sql_match = re.search(r"status IN \(([^)]*)\)", sql)
    assert sql_match, "代码 SQL 里没有 status IN (...) 条件"

    def _statuses(raw: str) -> set[str]:
        return {s.strip().strip("'") for s in raw.split(",")}

    assert _statuses(sql_match.group(1)) == _statuses(match.group(1)), (
        f"代码条件与索引谓词漂移：索引是 {match.group(1)}，代码是 {sql_match.group(1)}"
    )
    # 确定性取一条，不依赖「唯一索引保证至多一行」这种跨文件隐式契约
    assert "ORDER BY created_at DESC" in sql
    assert sql.rstrip().upper().endswith("LIMIT 1")


def test_find_conflicting_call_returns_none_when_no_conflict() -> None:
    """没有冲突行 ⇒ ``None``，不是空字符串（调用方靠 ``or`` 回退）。"""
    from cpt.storage.llm_call_store import find_conflicting_call

    assert find_conflicting_call(FakeConn(rows=[]), purpose="p", request_hash_value="D") is None
    assert (
        find_conflicting_call(FakeConn(rows=[(None,)]), purpose="p", request_hash_value="D") is None
    )


def test_find_conflicting_call_raises_on_db_error() -> None:
    """读失败**抛**，不能返回 ``None`` 冒充「没有冲突」。

    返回 ``None`` 会让调用方回退到刚生成、从未入库的 id —— 正是 P2-14 要修的
    那个幽灵 id。降级口径归调用方（``llm_cases._existing_call_id`` catch 它），
    与 ``recent_calls`` 同一纪律。
    """
    from cpt.storage.llm_call_store import find_conflicting_call

    class Broken:
        def cursor(self) -> Any:
            raise RuntimeError("relation does not exist")

    with pytest.raises(LLMCallError):
        find_conflicting_call(Broken(), purpose="p", request_hash_value="D")  # type: ignore[arg-type]


def test_conflict_row_outside_the_recent_50_window_is_still_found() -> None:
    """**P2-14 的原始病案**：冲突行落在最近 50 行之外时，旧实现查不到。

    造一张 500 行的表，目标冲突行 ``created_at`` 最旧（第 1 行），后面压着
    499 行别的调用（模拟同一个热点提示词引起的堆积，或别的 purpose 把时间轴
    挤满）。``recent_calls(limit=50)`` 按时间倒序只看得到最新 50 行 ⇒
    老实现返回 ``None`` ⇒ 调用方回退到刚生成、从未入库的 id ⇒ 前端轮询幽灵 id。

    这里用**同一个假表**把新旧两种查法各跑一遍：新口径查到、老口径查不到。
    只验新口径能查到是不够的 —— 那证明不了「旧实现真的会错」，也就证明不了
    这次改动修掉了什么。
    """
    from cpt.storage.llm_call_store import _COLUMNS, find_conflicting_call

    def _row(call_id: str, purpose: str, digest: str, status: str, created: int) -> dict[str, Any]:
        """一行 12 列。**必须按真 ``_COLUMNS`` 个数吐全** —— ``_row_to_dict`` 里是
        ``zip(_COLUMNS, row)``，只吐 1 个值的话 12 个列名全对到那一个值上，结果
        只剩 ``call_id`` 一个键（第一版就是这样踩的 ``KeyError: 'request_hash'``）。
        ``created_at`` 用整数：只用于排序，免去时区口舌。
        """
        return {
            "call_id": call_id,
            "purpose": purpose,
            "subject_id": None,
            "status": status,
            "request_hash": digest,
            "result_text": None,
            "error_text": None,
            "model": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "created_at": created,
            "finished_at": None,
        }

    #: 目标冲突行 ``created_at`` 最旧（0），后面 499 行全是别的 purpose。
    target = _row("c-the-real-one", "summarize_recommendation", "DIGEST", "ok", 0)
    noise = [
        _row(f"c-other-{i}", "explain_structure", f"other-{i}", "ok", i + 1) for i in range(499)
    ]
    #: 真行池，按 ``ORDER BY created_at DESC`` 排好。
    ordered = sorted([target, *noise], key=lambda row: row["created_at"], reverse=True)

    class TableCursor:
        def __init__(self, conn: TableConn) -> None:
            self._conn = conn
            self.rowcount = 0
            self._rows: tuple[tuple, ...] = ()

        def __enter__(self) -> TableCursor:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def execute(self, sql: str, params: tuple = ()) -> None:
            self._conn.executed.append((sql, params))
            if "request_hash = %s" in sql:
                # find_conflicting_call：只选 call_id，按索引谓词过滤后取 1 条
                purpose, digest = params[0], params[1]
                hits = [
                    row
                    for row in ordered
                    if row["purpose"] == purpose
                    and row["request_hash"] == digest
                    and row["status"] in ("queued", "running", "ok")
                ]
                self._rows = tuple((row["call_id"],) for row in hits[:1])
            else:
                # recent_calls（老口径）：SQL 里**没有** purpose / request_hash /
                # 状态条件，只是全表按时间倒序截前 limit 行，选全部 12 列。
                self._rows = tuple(
                    tuple(row[col] for col in _COLUMNS) for row in ordered[: int(params[-1])]
                )

        def fetchall(self) -> tuple[tuple, ...]:
            return self._rows

        def fetchone(self) -> tuple | None:
            return self._rows[0] if self._rows else None

    class TableConn:
        def __init__(self) -> None:
            self.executed: list[tuple[str, tuple]] = []

        def cursor(self) -> TableCursor:
            return TableCursor(self)

    # 新口径：按索引谓词查，500 行里照样命中
    conn = TableConn()
    found = find_conflicting_call(
        conn, purpose="summarize_recommendation", request_hash_value="DIGEST"
    )
    assert found == "c-the-real-one"

    # 老口径：只拉最近 50 行、再在 Python 里比 hash ⇒ **查不到**
    conn2 = TableConn()
    recent = recent_calls(conn2, limit=50)
    assert len(recent) == 50
    old_hit = next((row for row in recent if row["request_hash"] == "DIGEST"), None)
    assert old_hit is None, (
        "这条断言失败**反而是好事**：说明 50 行窗口已经能覆盖目标行，本用例不再是 "
        "P2-14 的有效复现 —— 需要把表造得更大，别急着删断言"
    )
