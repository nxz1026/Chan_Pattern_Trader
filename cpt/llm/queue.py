"""异步队列：fire-and-forget + 指数退避重入。

## 为什么必须是异步

实测 `agnes-3.5-flash` / `agnes-3.0-flash` 延迟 **320 ms – 7.4 s**（冷启动）。
同步等一次 = 看板 HTTP 请求卡住最多 7 秒，用户会以为服务挂了。所以
`architecture.md` §4.1 第 1 条约束「永不阻塞核心」不是锦上添花，是硬需求。

## 为什么是「进程内 daemon 线程」而不是 asyncio 或独立 worker 进程

- 整个 CPT 是同步的（``ThreadingHTTPServer`` + ``threading.Lock``），引 asyncio
  会把 web 层一起拖进异步，代价远大于收益；
- 不需要新的 systemd unit，不用管两个进程的部署顺序与启停顺序。

**代价必须说清**：web 进程重启时**在途任务会丢**。所以 worker 启动时会把库里
``status`` 处于 ``queued`` / ``running`` / ``rate_limited`` 的行标成
``interrupted``，让 UI 能如实显示「这次没跑完」，而不是让调用方永远等一个
不会来的结果（``rate_limited`` 是 2026-10-08 审计 M15 补上的：退避等待只在
**内存队列**里，进程一被杀那行就永远停在 ``rate_limited``、``finished_at`` 恒 NULL）。

## 429 的处置（实测特征决定的设计）

`agnes-ai.cn` 的 429：**响应体为空**、**无 ``Retry-After``、恢复后仍零星出现**
（令牌桶，不是硬冷却）。所以：

1. **不能热循环重试** —— 撞 429 立刻再打只会继续 429、白烧配额。必须
   ``requeue(delay)`` 重新排队，让出配额窗口。
2. **退避 = 基数 × 2^attempt + jitter**。jitter 防惊群：多个 worker 同时醒来
   再一起打，又是一轮 429。
3. **限流不是失败**。``rate_limited`` 是独立状态，UI 显示「排队中（服务商限流）」，
   混进 ``error`` 会让看板天天报红。
4. **401/403 等 4xx 不重试** —— key 无效重试一万次也没用。
5. **有次数上限**，超过记 ``error`` + ``rate_limited_exhausted``，不无限重入。

## 瞬时故障的重试边界（审计 L6）

原来 ``max_attempts`` / 退避**只对 429 生效**，5xx / 连接错一次就终态，与
provider 文档自相矛盾。现在只对**明确安全可重试**的错误退避重入 —— 判据是
provider 打了 ``retryable`` 标记（见 ``providers/openai_compatible.py``）：

- **5xx**：服务端瞬时故障，重试安全 ⇒ 重试；
- **连接错**：请求根本没送达 ⇒ 重试；
- **超时**：provider **刻意不打标记，不重试** —— 服务商可能已受理并计费，
  重试会把一次调用算成两次（at-most-once 优先于可用性）。

重试状态**复用 ``queued``**（而不是新造一个状态）：``queued`` 非终态，能被
``mark_interrupted`` 一起清扫、能直接显示「排队中」，且不用改状态词表/迁移
CHECK 约束。耗尽后记 ``error`` + ``retryable_exhausted``。

## 队列是有界的（审计 M6）

``_pending`` 原来是**无界** ``PriorityQueue``：worker 一旦卡死（例如服务端慢速
吐字节），后续请求只会无限堆积内存。``submit()`` 现在超过 ``max_pending`` 就
如实拒绝 ``llm_queue_full``。退避重入仍走无界 ``put`` —— 那是 worker 自己排回
自己，限流会死锁。
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cpt.llm.base import LLMClient, LLMError, LLMRateLimited, LLMRequest, LLMResult
from cpt.llm.config import LLMConfig

_LOG = logging.getLogger(__name__)

#: 本层会**发射**的状态。
#:
#: **刻意不从 ``cpt.storage`` 导入** —— ``.importlinter`` 的
#: ``llm-does-not-leak-into-storage`` 禁止 llm → storage（storage 是低层）。
#: 跨层共享同一份词汇靠**测试**而不是 import：
#: ``tests/test_llm_call_store.py::test_status_vocabularies_agree``
#: 把这里、``storage.llm_call_store``、以及迁移 SQL 的 CHECK 约束三者对齐。
#:
#: （这正是那条契约第一次派上用场的地方 —— 写完契约的同一天就违反了它。）
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"
#: 限流。**不是 error** —— 混进去会让看板天天报红，而它其实在正常退避重试。
STATUS_RATE_LIMITED = "rate_limited"
#: 进程重启时在途任务的终态。也不是 error。
STATUS_INTERRUPTED = "interrupted"

__all__ = ["Job", "LLMQueue", "SubmitResult"]


@dataclass(slots=True)
class Job:
    """一个待办 / 在途的 LLM 调用。

    :param request: 实际请求。
    :param call_id: 审计表主键。
    :param attempt: 已重试次数（429 退避重入会 +1）。
    """

    request: LLMRequest
    call_id: str
    attempt: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """``submit()`` 的返回值。HTTP 层拿它立刻回响应，不等模型。"""

    accepted: bool
    call_id: str
    reason: str = ""


class LLMQueue:
    """单 worker 的异步队列。**进程内、全局单例**（由 ``get_queue`` 维护）。

    刻意用**单 worker**：provider 是免费档、限流阈值实测在 6~15 并发之间，
    串行是唯一稳的形态。要提并发得先解决配额，不是加线程能解决的。

    :param client: provider 实现。
    :param config: 运行配置（退避参数从这里读）。
    :param on_status: 状态变更回调，签名
        ``(call_id, status, detail, result) -> None``；``result`` 只在
        ``status='ok'`` 时非 ``None``，落库方从它取 model / token 用量。
        落库由调用方在回调里做 —— **本模块不碰 SQL**（R24 门禁）。
    :param max_pending: 待处理队列的硬上限（审计 M6）。超过时 ``submit()``
        返回 ``accepted=False, reason='llm_queue_full'``，不再无界堆积内存。
        退避重入不受此限 —— 见模块 docstring「队列是有界的」。
    """

    def __init__(
        self,
        client: LLMClient,
        config: LLMConfig,
        *,
        on_status: Callable[[str, str, str, LLMResult | None], None] | None = None,
        worker_count: int = 1,
        max_pending: int = 100,
    ) -> None:
        self._client = client
        self._config = config
        self._on_status = on_status or (lambda _id, _st, _detail, _res: None)
        #: 保护 ``_on_status``：worker 线程读、HTTP 线程可能补注册（见 set_on_status）
        self._callback_lock = threading.Lock()
        self._pending: queue.PriorityQueue[tuple[float, int, Job]] = queue.PriorityQueue()
        # 审计 M6：至少为 1，避免把队列配成"永远拒绝"这种自锁配置
        self._max_pending = max(1, int(max_pending))
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        for index in range(worker_count):
            thread = threading.Thread(target=self._run, name=f"cpt-llm-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def set_on_status(self, callback: Callable[[str, str, str, LLMResult | None], None]) -> None:
        """**补注册**状态回调（后设的覆盖先设的）。

        R45 新增。存在的理由：``cpt.llm.get_queue`` 是进程内单例，而
        ``application/llm_cases`` 有两个调用点 —— ``list_calls()`` 调
        ``get_queue()``（不传回调，看板一打开就会走到）、
        ``_bootstrap()`` 调 ``get_queue(on_status=on_llm_status)``（要落库）。
        谁先跑谁定回调，于是先打开过看板的用户**永远注册不上审计回调**，
        LLM 调用跑完但状态不落库、每条卡在 ``queued``。

        **线程安全**：worker 线程会在 ``_execute`` 里读 ``_on_status``，
        而这里可能由**另一个** HTTP 线程同时写。用 ``_stop`` 同款锁保护，
        并让 worker 侧走 ``_emit()`` 取快照，避免「读到一半被换掉」。
        """
        with self._callback_lock:
            self._on_status = callback

    def _emit(self, call_id: str, status: str, detail: str, result: LLMResult | None) -> None:
        """取回调快照再调 —— 避免持锁调用（回调会开 DB 连接，不能阻塞别���）。"""
        with self._callback_lock:
            callback = self._on_status
        callback(call_id, status, detail, result)

    # ---------------------------------------------------------------- 公开

    @property
    def depth(self) -> int:
        """待处理任务数（含正在退避等待的）。

        ``submit()`` 之后立刻读它，就能告诉用户「已排队，前面还有 N 个」——
        比返回一个干巴巴的 accepted=True 有用。
        """
        return self._pending.qsize()

    def drain(self, timeout: float = 30.0) -> bool:  # noqa: ARG002
        """测试专用：轮询等队列排空。返回是否排空。

        **不能用 ``queue.join()``**：worker 取走任务后还没执行完，join() 会返回；
        这里等的是「真的处理完」，所以按 depth 轮询。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._pending.empty():
                time.sleep(0.05)  # 让最后一个任务的收尾跑完
                if self._pending.empty():
                    return True
            time.sleep(0.01)
        return False

    def submit(self, job: Job) -> SubmitResult:
        """入队并**立刻返回**。这是「不阻塞核心」的落点。

        R28-4：worker 不可用时**必须如实拒绝**。原先只查 ``enabled``，于是
        worker 线程万一死掉（本该由 ``_run`` 的兜底 try 兜住，但那是最后一道
        防线），``submit`` 仍会返回 ``accepted=True`` —— 调用方据此把状态写成
        ``queued``，那一行就永远停在 queued 且**没有任何错误可查**。
        真机上就是这么丢过一次调用。

        审计 M6：队列**有界**。worker 被慢速服务端卡住时，无界 ``_pending``
        只会把内存吃光；超过 ``max_pending`` 就返回 ``llm_queue_full``，让
        调用方如实告诉用户「忙」，而不是假装排上了队。
        """
        if not self._config.enabled:
            return SubmitResult(False, job.call_id, "llm_disabled")
        if not self._worker_alive():
            return SubmitResult(False, job.call_id, "llm_worker_unavailable")
        if self._pending.qsize() >= self._max_pending:
            return SubmitResult(False, job.call_id, "llm_queue_full")
        self._enqueue(job)
        return SubmitResult(True, job.call_id)

    def _worker_alive(self) -> bool:
        """worker 线程是否还活着。

        刻意**不**只看 ``_stop``：那只能反映「被人正常停掉」，反映不了
        「线程意外死了」—— 而后者才是真机上遇到的那种。
        """
        if self._stop.is_set():
            return False
        return any(thread.is_alive() for thread in self._threads)

    def stop(self, timeout: float = 5.0) -> None:
        """停 worker（测试与优雅退出用）。"""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)

    # ---------------------------------------------------------------- 内部

    def _enqueue(self, job: Job, delay: float = 0.0) -> None:
        with self._seq_lock:
            self._seq += 1
            order = self._seq
        self._pending.put((time.monotonic() + delay, order, job))

    def _backoff_delay(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的等待秒数。

        ``min(base × 2^attempt, cap) + jitter``。jitter 取满量程的 0~30%，
        避免多 worker 同时醒来再一起撞 429。
        """
        base = float(self._config.backoff_base)
        cap = float(self._config.backoff_max)
        raw: float = min(base * (2**attempt), cap)
        return float(raw + random.uniform(0.0, raw * 0.3))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                due, _order, job = self._pending.get(timeout=0.2)
            except queue.Empty:
                continue

            now = time.monotonic()
            if due > now:
                # 还没到退避时间：放回去，别空转
                self._pending.put((due, _order, job))
                time.sleep(min(0.2, due - now))
                continue

            # R28-4：这一层 try/except 是**必须的**，不是保险。
            #
            # 没有它时，任何从 ``_execute`` 逃出去的异常都会让 worker 线程
            # **永久退出**；而 ``submit()`` 只管往队列里塞，不知道还有没有
            # 活着的消费者，于是继续返回 ``accepted=True``。后果是任务永远
            # 不执行、状态永远停在 ``queued``、**且任何地方都不报错** ——
            # 比直接抛异常坏得多（抛异常至少会有人看见）。
            #
            # 真机撞到过：2026-10-02 一次提交永久 ``queued``、队列深度 0，
            # 重启服务后同一条链路 8 秒跑完。具体触发异常未捕获到，但
            # 「线程一死就永久静默丢任务」这个结构本身已足够严重。
            try:
                self._execute(job)
            except BaseException as exc:  # noqa: BLE001 — worker 绝不能因为一个任务死掉
                _LOG.exception(
                    "LLM worker 执行任务时未捕获异常（call_id=%s attempt=%s）：%r",
                    job.call_id,
                    job.attempt,
                    exc,
                )
                # 尽力把这次调用标成失败：worker 活着但这次死了，不标的话
                # 调用方会永远等一个不会来的结果。
                try:
                    self._emit(
                        job.call_id,
                        STATUS_ERROR,
                        f"worker_exception: {type(exc).__name__}",
                        None,
                    )
                except Exception as status_exc:  # noqa: BLE001 — 连标记都失败就算了
                    _LOG.warning("标记 worker 异常失败时又出错 %s: %s", job.call_id, status_exc)

    def _execute(self, job: Job) -> None:
        self._emit(job.call_id, STATUS_RUNNING, "", None)
        try:
            result = self._client.complete(job.request)
        except LLMRateLimited as exc:
            delay = self._retry_within_limit(job)
            if delay is None:
                self._emit(
                    job.call_id,
                    STATUS_ERROR,
                    f"rate_limited_exhausted: {exc}",
                    None,
                )
                return
            self._emit(
                job.call_id,
                STATUS_RATE_LIMITED,
                f"retry_in={delay:.1f}s attempt={job.attempt}",
                None,
            )
            self._enqueue(job, delay=delay)
            return
        except LLMError as exc:
            # 审计 L6：只有 provider **明确**打了 retryable 标记的瞬时故障才重入
            # （5xx / 连接错）。超时**不打标记** → 一次终态，避免重复计费。
            if not getattr(exc, "retryable", False):
                self._emit(job.call_id, STATUS_ERROR, str(exc), None)
                return
            delay = self._retry_within_limit(job)
            if delay is None:
                self._emit(
                    job.call_id,
                    STATUS_ERROR,
                    f"retryable_exhausted: {exc}",
                    None,
                )
                return
            # 重试复用 queued（非终态、可被 mark_interrupted 清扫），不新造状态
            self._emit(
                job.call_id,
                STATUS_QUEUED,
                f"transient: {exc}; retry_in={delay:.1f}s attempt={job.attempt}",
                None,
            )
            self._enqueue(job, delay=delay)
            return
        except Exception as exc:  # noqa: BLE001 — worker 绝不能因为一个任务死掉
            self._emit(job.call_id, STATUS_ERROR, f"unexpected: {exc!r}", None)
            return

        # 结果整份传给回调：text 落 result_text 列，model / token 各有各的列
        # （architecture.md §4.1 约束 4：token 用量必须可审计）。
        self._emit(job.call_id, STATUS_OK, result.text, result)

    def _retry_within_limit(self, job: Job) -> float | None:
        """还有重试余量就 ``attempt += 1`` 并返回本次退避秒数；否则 ``None``。

        ``None`` 表示已达到 ``max_attempts``，调用方按各自的终态文案记 ``error``。
        429 与瞬时故障共用它，保证两条路径的重试计数与退避曲线完全一致。
        """
        if job.attempt + 1 >= self._config.max_attempts:
            return None
        job.attempt += 1
        return self._backoff_delay(job.attempt)
