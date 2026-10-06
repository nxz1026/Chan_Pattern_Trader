"""LLM 调用审计表 ``public.cpt_llm_call`` 的读写（R25）。

**这一层不 commit** —— 与 `signal_event_store` / `dashboard_run_store` 同一约定，
事务边界归调用方。

关于**连接从哪来**：`LLM` 的调用方在 HTTP 线程（只做入队），落库发生在 worker
线程。worker 是 daemon 线程，不能复用 HTTP 请求里的连接，所以这里自己开一条：
`queue.get_queue(on_status=...)` 的回调里现场借一条、用完即关。这也意味着
**每条状态变更一次 DB 往返** —— 对于「解释一次结构」这种低频操作完全够用，
而且换来了「状态实时可见」，比攒批写入更符合这个用例的语义。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

_LOG = logging.getLogger(__name__)

__all__ = [
    "LLMCallError",
    "TERMINAL_STATUSES",
    "call_row",
    "enqueue_call",
    "finish_call",
    "mark_interrupted",
    "request_hash",
    "recent_calls",
    "STATUS_ERROR",
    "STATUS_INTERRUPTED",
    "STATUS_OK",
    "STATUS_QUEUED",
    "STATUS_RATE_LIMITED",
    "STATUS_RUNNING",
]


class LLMCallError(RuntimeError):
    """调用审计表**写**失败（R45 新增）。

    只用于写路径。读路径的降级语义各不相同（见各函数 docstring），
    但**写失败一律抛** —— 审计记录写不进去却假装成功，比没有审计更糟：
    它让人以为「没调用过」，而实际上模型已经被调过、token 已经花掉了。
    """


#: ``status`` 列的取值。**定义在这里而不是 ``cpt/llm/queue.py``** ——
#: 这张表是 storage 的，枚举就归 storage。反过来说 storage → llm 是低层依赖高层，
#: 会踩 layers 契约（见 .importlinter）。
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"
#: 限流。**不是 error** —— 混进去会让看板天天报红，而它其实在正常退避重试。
STATUS_RATE_LIMITED = "rate_limited"
#: 进程重启时在途任务的终态。也不是 error。
STATUS_INTERRUPTED = "interrupted"

#: 这些状态说明「这个任务不会再变了」，进程重启时可以把在途的标成 interrupted
TERMINAL_STATUSES = frozenset({STATUS_OK, STATUS_ERROR, STATUS_INTERRUPTED})

_COLUMNS = (
    "call_id",
    "purpose",
    "subject_id",
    "status",
    "request_hash",
    "result_text",
    "error_text",
    "model",
    "prompt_tokens",
    "completion_tokens",
    "created_at",
    "finished_at",
)

_INSERT_SQL = f"""
INSERT INTO public.cpt_llm_call ({", ".join(_COLUMNS)})
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_FINISH_SQL = """
UPDATE public.cpt_llm_call
   SET status = %s,
       result_text = %s,
       error_text = %s,
       model = %s,
       prompt_tokens = %s,
       completion_tokens = %s,
       finished_at = %s
 WHERE call_id = %s
"""


#: 这些列在库里是 ``timestamptz``，出参统一转 Unix 毫秒。
#:
#: 不转的话 ``cpt/web/app.py`` 的 ``_write_json`` 会在 ``json.dumps(datetime)``
#: 上炸掉，整页回 ``500 payload is not JSON-safe``（真机部署踩过）。口径与
#: R21 ``signal_event_store`` 一致，前端时间轴也都是毫秒。
_TIME_COLUMNS = frozenset({"created_at", "finished_at"})


def _to_ms(value: Any) -> Any:
    """``datetime`` → Unix 毫秒；``None`` 与非 datetime 原样返回。"""
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return value


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        name: _to_ms(value) if name in _TIME_COLUMNS else value
        for name, value in zip(_COLUMNS, row, strict=False)
    }


def request_hash(purpose: str, system: str, user: str) -> str:
    """提示词规范化后的 sha256。

    规范化只做**换行与首尾空白**归一，不动正文 —— 模型对内容敏感，
    过度归一会让不相关的请求撞到同一个缓存键。
    """
    normalized = f"{purpose}\x00{system.strip()}\x00{user.strip()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def call_row(
    *,
    purpose: str,
    subject_id: str,
    request_hash_value: str,
) -> dict[str, Any]:
    """构造一条 ``status='queued'`` 的初始行。"""
    return {
        "call_id": uuid.uuid4().hex,
        "purpose": purpose,
        "subject_id": subject_id or None,
        "status": "queued",
        "request_hash": request_hash_value,
        "result_text": None,
        "error_text": None,
        "model": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "created_at": datetime.now(UTC),
        "finished_at": None,
    }


#: ``ON CONFLICT`` 的部分唯一索引只覆盖在途/已成功的状态，所以重复提交
#: （同 purpose + 同 request_hash）会撞约束 —— 安静地变成 no-op，
#: 调用方据此回 ``duplicate`` 而不是报错。
_ON_CONFLICT = (
    "\nON CONFLICT (purpose, request_hash) WHERE status IN ('queued','running','ok') DO NOTHING"
)


def enqueue_call(conn: Any, row: dict[str, Any]) -> bool:
    """写入一条 ``queued`` 记录。

    :returns: ``True`` = 真的插进去了；``False`` = **同一提示词已在途或已成功**
        （被 ``ON CONFLICT DO NOTHING`` 挡掉）。
    :raises LLMCallError: **写入失败**。

    ## R45 修：写失败必须抛，不能和「重复」共用一个 ``False``

    原实现在 ``except`` 里 ``return False``，于是「重复提交」和「DB 写失败」
    返回**同一个值**。调用方 ``llm_cases.explain_structure`` 据此回
    ``{"status": "duplicate", "reason": "same_request_in_flight_or_done"}``，
    也就是对用户说「你���经问过了」—— 而真相是「一条都没写进去」。

    更糟的是它**静默违反了审计约束**（本模块头：「每次调用的模型与 token 用量
    必须落盘」）：DB 一抖，这条调用凭空消失，无人知晓。

    特别注意 ``application/llm_cases._write``（= ``fn()`` + ``conn.commit()``）
    **救不了这个**。实测 PostgreSQL 18.6：

        ① 语句失败: UndefinedTable
        ② commit(): **没抛** ← 事务在 aborted 态下 COMMIT 等于 ROLLBACK

    所以只要 store 函数吞掉异常，外层 commit 会静默回滚并原样返回那个假
    ``False``。``_write`` 那道「防忘记提交」的防线**只在 store 抛异常时有效**。

    ⇒ 「重复」是**业务事实**（可预期、可返回给用户）；
      「写失败」是**故障**（必须响亮）。两者不能共用一个返回值。
    """
    params = tuple(row.get(col) for col in _COLUMNS)
    try:
        with conn.cursor() as cur:
            cur.execute(_INSERT_SQL + _ON_CONFLICT, params)
            return bool(cur.rowcount > 0)
    except Exception as exc:
        _LOG.warning("写入 LLM 调用记录失败: %s", exc)
        raise LLMCallError(f"写入 LLM 调用记录失败: {exc}") from exc


def finish_call(
    conn: Any,
    call_id: str,
    *,
    status: str,
    result_text: str | None = None,
    error_text: str | None = None,
    model: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
) -> None:
    """更新终态。``finished_at`` 只在终态写。

    :raises LLMCallError: 写失败。**与 :func:`enqueue_call` 同口径**：本模块的
        契约是「写失败一律抛」（见 :class:`LLMCallError`），``finish_call`` 原来
        只记 warning 就返回 —— 于是「模型已经调过、token 已经花掉」这件事在库里
        消失了，调用方还当成功。审计记录写不进去却假装成功，比没有审计更糟。

        调用方义务（三处都在 ``cpt/application/llm_cases.py`` / ``cpt/web``）：
        ① 自己决定是吞是抛；② 吞的话必须 ``rollback``（事务里一条语句失败后，
        同一连接后续语句全部报 ``current transaction is aborted``）。
    """
    finished = datetime.now(UTC) if status in TERMINAL_STATUSES else None
    try:
        with conn.cursor() as cur:
            cur.execute(
                _FINISH_SQL,
                (
                    status,
                    result_text,
                    error_text,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    finished,
                    call_id,
                ),
            )
    except Exception as exc:
        _LOG.warning("更新 LLM 调用状态失败 %s: %s", call_id, exc)
        raise LLMCallError(f"更新 LLM 调用状态失败 {call_id}: {exc}") from exc


def recent_calls(
    conn: Any, *, limit: int = 20, subject_id: str | None = None
) -> tuple[dict[str, Any], ...]:
    """最近若干次调用，**时间倒序**。可选按 ``subject_id`` 过滤。

    R45 修：读失败**抛**，不能返回 ``()``。

    原实现返回空元组，而调用方 ``llm_cases.list_calls`` 直接
    ``len(rows)`` 拼进响应、回 ``{"available": true, "count": 0}`` ——
    那是把「库读不到」冒充成「确实没有调用过」。这正是本模块另一处
    （``load_signal_events``）docstring 里明文禁止的事（「前者不能冒充后者」），
    这里自己却犯了。
    """
    capped = max(1, min(int(limit), 200))
    sql = f"SELECT {', '.join(_COLUMNS)} FROM public.cpt_llm_call"
    params: list[Any] = []
    if subject_id:
        sql += " WHERE subject_id = %s"
        params.append(subject_id)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(capped)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or ()
    except Exception as exc:
        _LOG.warning("读取 LLM 调用列表失败: %s", exc)
        raise LLMCallError(f"读取 LLM 调用列表失败: {exc}") from exc
    return tuple(_row_to_dict(row) for row in rows)


def mark_interrupted(conn: Any, before: datetime | None = None) -> int:
    """把 ``queued`` / ``running`` 且**早于 ``before``** 的行标成 ``interrupted``。

    **进程重启时调用** —— 在途任务随进程一起没了，不标的话调用方会永远等一个
    不会来的结果。

    # gate: allow-silent: 启动期 best-effort —— 失败不该挡住进程起来。
    # 调用方 ``llm_cases._bootstrap`` 整段包在 try/except 里、且用**独立连接**，
    # 所以这里吞掉不会污染别人的事务；``marked`` 回 0 时它只记一条 info。

    :param before: 截止时刻，**只清更早的**。必须是「本进程启动时间」这类水位线：
        不带这个条件就会**误伤本进程刚入队的行** —— 首次调用 ``_bootstrap()`` 恰好
        发生在 ``enqueue_call`` 之后，一清扫就把自己刚写的行标成中断了
        （实测踩过：提交返回 queued，5 秒后变 ``interrupted / process_restarted``）。
        传 ``None`` 表示不设水位线（只应在测试与手工排查时用）。

    :returns: 被标记的行数。
    """
    params: list[Any] = []
    where = "status IN ('queued', 'running')"
    if before is not None:
        where += " AND created_at < %s"
        params.append(before)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.cpt_llm_call "
                "   SET status = 'interrupted', "
                "       error_text = COALESCE(error_text, 'process_restarted'), "
                "       finished_at = %s "
                f" WHERE {where}",
                (datetime.now(UTC), *params),
            )
            return int(cur.rowcount or 0)
    except Exception as exc:
        # ⚠️ ``exc_info=True`` 不是可选项：本仓自己在
        # ``structure_event_store.py`` 写过同一条纪律 ——「原来这里是 warning
        # 且不带 exc_info，栈被丢掉，「表没建」和「DB 挂了」两种现场在日志里
        # 长得一模一样」。这里同样：返回 0 与「没有待标记的行」同值，
        # **只有栈能区分这两种**，而栈一旦丢了就再也补不回来。
        _LOG.warning("标记中断的 LLM 调用失败: %s", exc, exc_info=True)
        return 0
