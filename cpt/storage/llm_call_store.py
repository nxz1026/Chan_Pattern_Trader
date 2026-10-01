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
    "TERMINAL_STATUSES",
    "call_row",
    "enqueue_call",
    "finish_call",
    "find_by_id",
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

    :returns: ``True`` = 真的插进去了；``False`` = 同一提示词已在途或已成功
        （被 ``ON CONFLICT DO NOTHING`` 挡掉），或写入失败。
    """
    params = tuple(row.get(col) for col in _COLUMNS)
    try:
        with conn.cursor() as cur:
            cur.execute(_INSERT_SQL + _ON_CONFLICT, params)
            return bool(cur.rowcount > 0)
    except Exception as exc:
        _LOG.warning("写入 LLM 调用记录失败: %s", exc)
        return False


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
    """更新终态。``finished_at`` 只在终态写。"""
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


def find_by_id(conn: Any, call_id: str) -> dict[str, Any] | None:
    """按 ``call_id`` 取一行；不存在 → ``None``。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM public.cpt_llm_call WHERE call_id = %s",
                (call_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        _LOG.warning("读取 LLM 调用记录失败 %s: %s", call_id, exc)
        return None
    return dict(zip(_COLUMNS, row, strict=False)) if row else None


def recent_calls(
    conn: Any, *, limit: int = 20, subject_id: str | None = None
) -> tuple[dict[str, Any], ...]:
    """最近若干次调用，**时间倒序**。可选按 ``subject_id`` 过滤。"""
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
        return ()
    return tuple(dict(zip(_COLUMNS, row, strict=False)) for row in rows)


def mark_interrupted(conn: Any, before: datetime | None = None) -> int:
    """把 ``queued`` / ``running`` 且**早于 ``before``** 的行标成 ``interrupted``。

    **进程重启时调用** —— 在途任务随进程一起没了，不标的话调用方会永远等一个
    不会来的结果。

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
        _LOG.warning("标记中断的 LLM 调用失败: %s", exc)
        return 0
