"""LLM 用例编排（R25 首版：规则解释）。

**这一层是唯一 import ``cpt.llm`` 的地方。** `architecture.md` §4.1 约束 1：
「只有 ``application/`` 的 LLM 用例会调用它」—— domain / storage / adapters /
web 都不该 import。

它做三件事，按顺序：

1. 把领域对象（结构 JSON + 规则片段）拼成提示词（``cpt.llm.prompts``）；
2. 落一条 ``queued`` 审计记录（``cpt.storage.llm_call_store``）；
3. **入队并立刻返回** —— HTTP 不等模型。

落库与入队的顺序有讲究：先落库拿到 ``call_id``，再入队。反过来的话 worker
可能在记录写好之前就完成，UI 查不到这次调用。

LLM 不可用时**优雅降级**：``submit()`` 返回 ``accepted=False`` 或队列是
``None``，都只是把 ``status`` 标成对应原因，HTTP 照常 200 —— 旁路增强不该
拖垮主视图。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from cpt.llm import get_queue
from cpt.llm.base import LLMResult
from cpt.llm.prompts import PURPOSE_EXPLAIN, explain_request
from cpt.storage.llm_call_store import (
    STATUS_OK,
    call_row,
    enqueue_call,
    mark_interrupted,
    recent_calls,
    request_hash,
)

_LOG = logging.getLogger(__name__)

#: 本进程的启动水位线。`mark_interrupted` 只清**早于它**的行 —— 不设水位线的话，
#: `_bootstrap()` 恰好发生在 `enqueue_call` 之后，一清扫就会把本进程刚入队的
#: 行标成 interrupted（实测踩过：提交返回 queued，5 秒后变 process_restarted）。
_PROCESS_START = datetime.now(UTC)

__all__ = [
    "explain_structure",
    "list_calls",
    "on_llm_status",
    "recover_interrupted",
]


def _write(conn: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """跑一个 store 写操作**并 commit**。

    **为什么需要这个包装器**：``cpt/storage/*`` 一律**不 commit**（事务边界归调用方，
    与 R21 ``signal_event_store`` 同一约定）。好处是多个写入能共享一个事务；
    代价是**每个调用点都必须自己 commit**，漏了就是静默回滚。

    R23 漏过一次（``_persist_run`` 少一行 ``conn.commit()``，HTTP 全 200、日志零
    告警、表 0 行）；**R25 又漏了一次** —— 第一次是 ``explain_structure`` 没提交
    入队行，第二次是 ``on_llm_status`` 没提交状态更新。两次都是「函数返回了、
    数据却不在」。

    所以这里把 commit 收进包装器：application 层**只调 ``_write``**，不给「忘记
    提交」留位置。``tests/test_llm_layer.py::test_app_layer_always_commits``
    钉住它。
    """
    result = fn(conn, *args, **kwargs)
    conn.commit()
    return result


def on_llm_status(call_id: str, status: str, detail: str, result: LLMResult | None = None) -> None:
    """``LLMQueue`` 的状态回调 —— worker 线程调它，落库在这里。

    每条状态变更一次 DB 往返。对「解释一个结构」这种低频操作够用，而且换来
    状态实时可见（UI 能显示「正在跑 / 限流退避中 / 完成」）。

    ``result`` 只在成功时非 None —— model / token 从它取（审计约束 4：
    每次调用的模型与 token 用量必须落盘）。
    """
    from cpt.adapters.a_share_local import AShareLocalClient

    client = AShareLocalClient()
    try:
        _write(
            client._get_conn(),  # noqa: SLF001
            call_id,
            status=status,
            result_text=detail or None,
            error_text=None if status == STATUS_OK else (detail or None),
            model=result.model if result is not None else None,
            prompt_tokens=result.usage.prompt_tokens if result is not None else None,
            completion_tokens=result.usage.completion_tokens if result is not None else None,
        )
    except Exception as exc:  # noqa: BLE001 — 落库失败绝不能杀死 worker
        _LOG.warning("LLM 状态落库失败 %s/%s: %s", call_id, status, exc)
    finally:
        client.close()


def _bootstrap() -> Any:
    """启动时把在途任务标成 interrupted，并拿到带落库回调的队列。"""
    from cpt.adapters.a_share_local import AShareLocalClient

    client = AShareLocalClient()
    try:
        marked = _write(client._get_conn(), mark_interrupted, _PROCESS_START)  # noqa: SLF001
        if marked:
            _LOG.info("已把 %s 条中断的 LLM 调用标记为 interrupted", marked)
    except Exception as exc:  # noqa: BLE001 — 表可能还没建，不该挡住启动
        _LOG.info("跳过 interrupted 标记（表不可用？）: %s", exc)
    finally:
        client.close()
    return get_queue(on_status=on_llm_status)


def explain_structure(
    conn: Any,
    *,
    code: str,
    name: str,
    market: str,
    structure: dict[str, Any],
    rules: dict[str, Any] | None = None,
    subject_id: str = "",
) -> dict[str, Any]:
    """提交一次「规则解释」请求，**立刻返回**，不等模型。

    :returns: ``{"available": bool, "call_id": str, "status": str, "reason": str}``。
        ``available=False`` 时 ``reason`` 说明为什么（未启用 / 缺 key / 重复提交…）。
    """
    request = explain_request(
        code=code,
        name=name,
        market=market,
        structure=structure,
        rules=rules,
        subject_id=subject_id,
    )
    digest = request_hash(request.purpose, request.system, request.user)
    row = call_row(
        purpose=PURPOSE_EXPLAIN,
        subject_id=subject_id,
        request_hash_value=digest,
    )
    if not _write(conn, enqueue_call, row):
        return {
            "available": False,
            "call_id": row["call_id"],
            "status": "duplicate",
            "reason": "same_request_in_flight_or_done",
        }

    queue = _bootstrap()
    if queue is None:
        _write(
            conn,
            row["call_id"],
            status="error",
            error_text="llm_unavailable",
        )
        return {
            "available": False,
            "call_id": row["call_id"],
            "status": "error",
            "reason": "llm_unavailable",
        }

    from cpt.llm.queue import Job  # noqa: PLC0415

    submitted = queue.submit(
        Job(request=request, call_id=row["call_id"], metadata={"digest": digest})
    )
    if not submitted.accepted:
        _write(
            conn,
            row["call_id"],
            status="error",
            error_text=submitted.reason or "submit_rejected",
        )
        return {
            "available": False,
            "call_id": row["call_id"],
            "status": "error",
            "reason": submitted.reason or "submit_rejected",
        }

    return {
        "available": True,
        "call_id": row["call_id"],
        "status": "queued",
        "reason": "",
    }


def list_calls(conn: Any, *, limit: int = 20, subject_id: str | None = None) -> dict[str, Any]:
    """读最近若干次 LLM 调用（UI 轮询用）。

    顺带回一个**脱敏**的 ``config`` 块（``LLMConfig.redacted()``，**不含 key**）——
    UI 要能显示「为什么 LLM 不可用」，而不是只看到一个空列表。排查时最费时间
    的就是「到底是没 enable、还是没 key、还是 base_url 写错」，所以把
    ``missing_reason()`` 一起回给前端。
    """
    from cpt.llm.config import load_config  # noqa: PLC0415
    from cpt.llm.queue import LLMQueue  # noqa: PLC0415 — 读队列深度用

    rows = recent_calls(conn, limit=limit, subject_id=subject_id)
    config = load_config()
    queue = get_queue()
    return {
        "schema_version": "dashboard_llm_calls.v1",
        "available": True,
        "count": len(rows),
        "calls": [dict(row) for row in rows],
        "config": config.redacted(),
        "unavailable_reason": config.missing_reason(),
        # 队列里还有几个（含退避等待中的）—— UI 显示「排队中，前面还有 N 个」
        "queued": queue.depth if isinstance(queue, LLMQueue) else 0,
    }


def recover_interrupted() -> int:
    """进程启动时调用：把在途任务标成 interrupted。"""
    from cpt.adapters.a_share_local import AShareLocalClient

    client = AShareLocalClient()
    try:
        return int(_write(client._get_conn(), mark_interrupted, _PROCESS_START))  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        _LOG.info("跳过 interrupted 标记: %s", exc)
        return 0
    finally:
        client.close()
