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
from cpt.llm.prompts import (
    explain_request,
    summarize_request,
)
from cpt.storage.llm_call_store import (
    STATUS_OK,
    LLMCallError,
    call_row,
    enqueue_call,
    finish_call,
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
    "summarize_recommendation",
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
            finish_call,
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


def _rollback_quietly(conn: Any, tag: str) -> None:
    """把连接从 aborted 态救回来，失败也只记日志。

    PostgreSQL 语义：事务里一条语句失败后，**同一连接**的后续语句全部报
    ``current transaction is aborted``。所以任何 catch 住 DB 异常的分支都必须
    rollback，否则这个连接上后面每一个操作都失败。

    R45 新增：``enqueue_call`` 改为抛之后，调用方第一次拿到了「写失败」这个
    事实，也第一次有机会 rollback。
    """
    try:
        conn.rollback()
    except Exception as exc:  # noqa: BLE001 — 救不回来也不能把调用点带崩
        _LOG.warning("rollback 失败 %s: %s", tag, exc)


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
    return _enqueue_and_submit(conn, request, subject_id=subject_id)


def list_calls(conn: Any, *, limit: int = 20, subject_id: str | None = None) -> dict[str, Any]:
    """读最近若干次 LLM 调用（UI 轮询用）。

    顺带回一个**脱敏**的 ``config`` 块（``LLMConfig.redacted()``，**不含 key**）——
    UI 要能显示「为什么 LLM 不可用」，而不是只看到一个空列表。排查时最费时间
    的就是「到底是没 enable、还是没 key、还是 base_url 写错」，所以把
    ``missing_reason()`` 一起回给前端。
    """
    from cpt.llm.config import load_config  # noqa: PLC0415
    from cpt.llm.queue import LLMQueue  # noqa: PLC0415 — 读队列深度用

    config = load_config()
    # R45：``recent_calls`` 读失败改为抛。原先它返回空元组，这里就回
    # ``available: true, count: 0`` —— 把「库读不到」冒充成「确实没有调用过」，
    # UI 会显示一个**看起来正常的空列表**，排查的人完全看不出是 DB 挂了。
    try:
        rows = recent_calls(conn, limit=limit, subject_id=subject_id)
    except LLMCallError as exc:
        _LOG.warning("读取 LLM 调用列表失败: %s", exc)
        _rollback_quietly(conn, "llm:list_calls")
        return {
            "schema_version": "dashboard_llm_calls.v1",
            "available": False,
            "count": 0,
            "calls": [],
            "reason": "llm_call_history_unavailable",
            "unavailable_reason": config.missing_reason(),
        }
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
    """把在途任务标成 interrupted，返回被标记的行数。

    ## R32：docstring 原来写的是「进程启动时调用」，**那句话是错的**

    AST 引用普查（全仓 0 引用）翻出这个函数之后我一度以为是死接线，去日志里查
    证伪了：journal 里

        10-02 00:39:44,927  已把 1 条中断的 LLM 调用标记为 interrupted

    而表里那条 `d1775cb…` 的 `finished_at` 是 `00:39:44.924` —— **差 3 毫秒**。
    也就是说标记**确实在生产上生效了**，只是走的不是这个函数，而是
    :func:`_bootstrap` 里同样的 ``mark_interrupted``（每次 ``explain_structure``
    提交时都会调一次）。所以：

    - **效果**没丢：中断标记是活的，有时间戳证据；
    - **这个函数**确实是 0 引用，且它宣称的调用点不存在。

    保留它作为「独立可调」的同一动作（运维/测试可以单独触发），但把调用点改成
    事实：**当前生产路径由** :func:`_bootstrap` 触发，不是这里。

    教训与 R30 的 ``config.py``、R31 的「本地没有交易日历」同一类：
    **注释里的调用点是会过期的，而且过期之后没有任何机制会告诉你。**
    """
    from cpt.adapters.a_share_local import AShareLocalClient

    client = AShareLocalClient()
    try:
        return int(_write(client._get_conn(), mark_interrupted, _PROCESS_START))  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        _LOG.info("跳过 interrupted 标记: %s", exc)
        return 0
    finally:
        client.close()


def summarize_recommendation(
    conn: Any,
    *,
    code: str,
    name: str,
    action_label: str,
    headline: str,
    reason: str,
    price: float | None = None,
    disclaimer: str = "",
    subject_id: str = "",
) -> dict[str, Any]:
    """提交一次「给推荐配人话」的请求，**立刻返回**，不等模型。

    与 :func:`explain_structure` 同样的入队骨架（幂等 / 审计 / 预算 / 落库），
    差别只在**喂给模型的东西**：这里只给
    :func:`cpt.application.recommendation` 算出的三行事实，**不给结构明细** ——
    模型没有机会算出与确定性结果矛盾的判断。

    :returns: ``{"available", "call_id", "status", "reason"}``，语义同 explain。
    """
    request = summarize_request(
        code=code,
        name=name,
        action_label=action_label,
        headline=headline,
        reason=reason,
        price=price,
        disclaimer=disclaimer,
        subject_id=subject_id,
    )
    return _enqueue_and_submit(conn, request, subject_id=subject_id)


def _enqueue_and_submit(
    conn: Any,
    request: Any,
    *,
    subject_id: str = "",
) -> dict[str, Any]:
    """LLM 请求的**公共入队骨架**（R45 P0-3 抽出来的）。

    ## 为什么抽

    之前 ``explain_structure`` 与 ``summarize_recommendation`` **各抄了一份**
    完全相同的入队逻辑（76 行 / 68 行）。抄代码会连 bug 一起抄 ——
    R45 那个「重复提交返回**库里不存在**的 ``call_id``」的 bug
    **在两份里各有一份**，且 ``explain`` 早就中招了，是补 summarize 的测试
    才发现的。**只要骨架还是两份，下次改一边就还会漏另一边。**

    ## 骨架负责什么

        1. 算 request_hash（幂等键）
        2. 落库入队 —— **写失败要抛**，不能和「重复」共用一个 False
        3. 重复 ⇒ 回**已存在那条**的真实 call_id
        4. 拿队列（LLM 未启用/缺 key ⇒ 标终态并回 llm_unavailable）
        5. 提交 Job（被拒 ⇒ 标终态并回原因）

    :returns: ``{"available", "call_id", "status", "reason"}``。
    """
    digest = request_hash(request.purpose, request.system, request.user)
    row = call_row(
        # ⚠️ purpose 从 **request 自己**取，不再由调用方传 ——
        # 传的话两个用例各写一遍，抄错就静默归错类目。
        purpose=request.purpose,
        subject_id=subject_id,
        request_hash_value=digest,
    )
    try:
        enqueued = _write(conn, enqueue_call, row)
    except LLMCallError as exc:
        _LOG.warning("LLM 入队落库失败 %s: %s", row["call_id"], exc)
        _rollback_quietly(conn, f"enqueue:{row['call_id']}")
        return {
            "available": False,
            "call_id": row["call_id"],
            "status": "error",
            "reason": "llm_audit_write_failed",
        }

    if not enqueued:
        # ⚠️ R45 修：`row["call_id"]` 是**刚生成**的 id，而这一条**根本没进库**
        # （被 ``ON CONFLICT DO NOTHING`` 挡掉了）⇒ 回给前端后，它拿这个 id
        # 去轮询**永远查不到**，表现是「明明算过，刷新一下摘要就没了」。
        # ⇒ 重复时回**已存在那条**的真实 call_id。
        # ⚠️ **两处都有这个 bug**（explain + summarize），一起改。
        return {
            "available": False,
            "call_id": _existing_call_id(conn, digest) or row["call_id"],
            "status": "duplicate",
            "reason": "same_request_in_flight_or_done",
        }

    queue = _bootstrap()
    if queue is None:
        _write(conn, finish_call, row["call_id"], status="error", error_text="llm_unavailable")
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
            finish_call,
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


def _existing_call_id(conn: Any, digest: str) -> str | None:
    """按 ``request_hash`` 找出**已存在**那条调用的 ``call_id``；查不到返回 ``None``。

    为什么需要它：``enqueue_call`` 在重复时返回 ``False``，而调用方手里的
    ``row["call_id"]`` 是**新生成**的、库里根本不存在。直接回给前端，
    前端轮询时永远查不到 ⇒「明明算过，刷新就没了」。

    ⚠️ R45 补这个时才发现：**explain_structure 早就带着同一个 bug** ——
    同一段代码抄了两份，bug 也抄了两份。
    """
    from cpt.storage.llm_call_store import recent_calls  # noqa: PLC0415

    try:
        rows = recent_calls(conn, limit=50)
    except LLMCallError:
        return None
    for item in rows:
        if item.get("request_hash") == digest:
            call_id = item.get("call_id")
            return str(call_id) if call_id else None
    return None
