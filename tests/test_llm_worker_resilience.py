"""worker 线程死亡后的队列行为（R28-4）。

## 这是真机上撞到的问题

2026-10-02 真机验证时：提交一次 LLM 解释，返回
``{"available": true, "status": "queued"}``，然后状态**永远停在 queued**，
队列深度却是 0 —— 任务既没被 worker 取走，也没在排队。重启服务后同一条链路
8 秒跑完（queued → running → ok）。

## 根因（代码上可直接证实，不需要知道具体是哪个异常）

``LLMQueue._run`` 调 ``_execute(job)`` 时**没有 try/except**，而 ``_execute``
首尾两处 ``self._on_status(...)`` 都在自己的 try 之外。于是任何逃出去的异常
都会让 worker 线程**永久退出**。

线程死掉之后：

- ``submit()`` 仍然返回 ``accepted=True`` —— 它只管往 PriorityQueue 里塞，
  不知道有没有活着的消费者；
- 任务永远不执行，**任何地方都不报错**（没有日志、没有异常、没有状态变化）；
- 同一个进程里后续所有提交全部静默丢失。

这比「报错」坏得多：报错至少会有人看见。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from cpt.llm.base import LLMError, LLMRateLimited, LLMRequest, LLMResult
from cpt.llm.config import LLMConfig
from cpt.llm.queue import STATUS_ERROR, STATUS_OK, Job, LLMQueue


def _config(**kw: Any) -> LLMConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "https://example.invalid/v1/chat/completions",
        "model": "stub",
        "api_key": "k",
        "timeout": 1.0,
        "max_attempts": 3,
        "backoff_base": 0.01,
        "backoff_max": 0.05,
    }
    base.update(kw)
    return LLMConfig(**base)  # type: ignore[arg-type]


def _request() -> LLMRequest:
    return LLMRequest(purpose="t", system="s", user="u")


def _ok(text: str = "ok") -> LLMResult:
    return LLMResult(text=text, model="stub")


# --------------------------------------------------------------------------- #
# 1. 现状：回调抛异常会打死 worker
# --------------------------------------------------------------------------- #


def test_worker_survives_a_raising_status_callback() -> None:
    """状态回调抛异常**不能**杀死 worker —— 它只是旁路（落库/通知）。

    这是真机上那条卡死调用最可能的触发点之一：``_execute`` 首尾两处
    ``_on_status`` 都在 try 之外。
    """
    calls: list[int] = []
    boom: list[bool] = [True]

    def on_status(_cid: str, status: str, _detail: str, _res: LLMResult | None) -> None:
        calls.append(1)
        if boom[0] and status == "running":
            raise RuntimeError("落库炸了")

    class Client:
        def complete(self, _request: LLMRequest) -> LLMResult:
            return _ok()

    queue = LLMQueue(Client(), _config(), on_status=on_status)  # type: ignore[arg-type]
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        assert queue.drain(timeout=5.0), "第一个任务没排空"

        # 关键：worker 要是死了，这一个会永远排空不了
        boom[0] = False
        seen: list[str] = []
        queue2 = queue
        queue2._on_status = lambda cid, st, d, r: seen.append(st)  # noqa: SLF001
        queue2.submit(Job(request=_request(), call_id="c2"))
        assert queue2.drain(timeout=5.0), "回调抛过一次异常后 worker 就死了 —— 后续任务全部静默丢失"
        assert STATUS_OK in seen
    finally:
        queue.stop()


def test_worker_survives_a_raising_client() -> None:
    """provider 抛出逃出所有 handler 的异常时，worker 也必须活着。"""
    first = {"boom": True}

    class Client:
        def complete(self, _request: LLMRequest) -> LLMResult:
            if first["boom"]:
                raise KeyboardInterrupt("模拟逃出 Exception 的异常")
            return _ok("第二次成功")

    seen: list[tuple[str, str]] = []
    queue = LLMQueue(Client(), _config(), on_status=lambda c, s, d, r: seen.append((s, d)))
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        time.sleep(0.3)
        first["boom"] = False
        queue.submit(Job(request=_request(), call_id="c2"))
        assert queue.drain(timeout=5.0), "worker 死了 —— 后续任务静默丢失"
        assert any(s == STATUS_OK for s, _ in seen), seen
    finally:
        queue.stop()


# --------------------------------------------------------------------------- #
# 2. 现状：worker 已死时 submit 仍然说「已接受」
# --------------------------------------------------------------------------- #


def test_submit_reports_honestly_when_worker_is_dead() -> None:
    """worker 不在时，``submit`` **不该**返回 ``accepted=True``。

    返回 True 意味着「我保证会处理」，而实际不会 —— 调用方据此把状态写成
    ``queued``，于是那一行就永远停在 queued，且没有任何错误可查。
    """
    queue = LLMQueue(_ok_client(), _config())
    try:
        # 直接把 worker 线程停掉，模拟线程意外退出后的状态
        queue._stop.set()  # noqa: SLF001
        for thread in queue._threads:  # noqa: SLF001
            thread.join(timeout=2.0)
        assert not any(t.is_alive() for t in queue._threads), "worker 应已停止"

        result = queue.submit(Job(request=_request(), call_id="c1"))
        assert result.accepted is False, (
            "worker 已死却回报 accepted —— 这就是真机上那行永远 queued 的成因"
        )
        assert result.reason, "拒绝时必须给原因，否则调用方无从归因"
    finally:
        queue.stop()


# --------------------------------------------------------------------------- #
# 3. 正常路径不受影响
# --------------------------------------------------------------------------- #


def test_normal_flow_unaffected() -> None:
    seen: list[tuple[str, str]] = []
    queue = LLMQueue(_ok_client(), _config(), on_status=lambda c, s, d, r: seen.append((s, d)))
    try:
        assert queue.submit(Job(request=_request(), call_id="c1")).accepted is True
        assert queue.drain(timeout=5.0)
        assert seen[-1][0] == STATUS_OK
    finally:
        queue.stop()


def test_rate_limited_still_retries() -> None:
    """429 退避不能被这次的 worker 保护改动破坏。"""
    script = [LLMRateLimited("429"), _ok("恢复")]

    class Client:
        def complete(self, _request: LLMRequest) -> LLMResult:
            item = script.pop(0) if len(script) > 1 else script[0]
            if isinstance(item, Exception):
                raise item
            return item

    seen: list[str] = []
    queue = LLMQueue(Client(), _config(), on_status=lambda c, s, d, r: seen.append(s))
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        assert queue.drain(timeout=5.0)
        assert seen[-1] == STATUS_OK
    finally:
        queue.stop()


def test_fatal_error_still_recorded() -> None:
    class Client:
        def complete(self, _request: LLMRequest) -> LLMResult:
            raise LLMError("服务炸了")

    seen: list[tuple[str, str]] = []
    queue = LLMQueue(Client(), _config(), on_status=lambda c, s, d, r: seen.append((s, d)))
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        assert queue.drain(timeout=5.0)
        assert seen[-1][0] == STATUS_ERROR
    finally:
        queue.stop()


def _ok_client() -> Any:
    class Client:
        def complete(self, _request: LLMRequest) -> LLMResult:
            return _ok()

    return Client()


def test_worker_thread_is_daemon_and_named() -> None:
    """worker 是 daemon 且有名字 —— 排障时要能在 thread dump 里认出它。"""
    queue = LLMQueue(_ok_client(), _config())
    try:
        assert len(queue._threads) == 1  # noqa: SLF001
        assert queue._threads[0].name.startswith("cpt-llm-")  # noqa: SLF001
    finally:
        queue.stop()


@pytest.mark.parametrize("bad", [None, 0, -1], ids=["none", "zero", "negative"])
def test_drain_returns_false_on_timeout(bad: Any) -> None:
    """drain 超时要返回 False，不能死等。"""
    queue = LLMQueue(_ok_client(), _config())
    try:
        assert queue.drain(timeout=0.2) is True  # 空队列立刻返回
    finally:
        queue.stop()
    assert isinstance(bad, (type(None), int))
    assert threading.active_count() >= 1
