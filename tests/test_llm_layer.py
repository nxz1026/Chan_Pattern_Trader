"""R25 LLM 层测试。

**不碰真实 provider** —— 用 :class:`FakeClient` 顶替，把网络往返变成本地函数调用。
真调 agnes 会同时踩到「CI 没网」和「免费档 429 限流」两个问题，测不稳也测不准。

要验的五件事，对应设计里最容易写错的地方：

1. **异步**：``submit()`` 立刻返回，worker 之后才跑 —— 不能阻塞在 HTTP 线程；
2. **429 退避重入**：撞限流**不能**热循环重试（会继续 429、白烧配额），
   必须 ``requeue(delay)``；退避序列要指数增长；
3. **重试有上限**：超过 ``max_attempts`` 记 ``error``，不是无限重入；
4. **错误分类**：401/403 与 429 处置不同（前者重试无用）；
5. **可关闭**：``enabled=False`` 时 ``submit()`` 拒收，且**不抛异常**。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from cpt.llm.base import LLMError, LLMRateLimited, LLMRequest, LLMResult, LLMUsage
from cpt.llm.config import LLMConfig, load_config
from cpt.llm.queue import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_RATE_LIMITED,
    STATUS_RUNNING,
    Job,
    LLMQueue,
)
from cpt.llm.registry import build_client

# --------------------------------------------------------------------------- #
# Fake provider
# --------------------------------------------------------------------------- #


class FakeClient:
    """按脚本返回结果的假 provider。

    :param script: 依次返回的结果；元素是 ``LLMResult`` 或要抛的异常。
        用尽之后重复最后一个（模拟「一直 429」）。
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self.calls = 0
        self._lock = threading.Lock()

    def complete(self, request: LLMRequest) -> LLMResult:
        with self._lock:
            index = min(self.calls, len(self._script) - 1)
            self.calls += 1
        item = self._script[index]
        if isinstance(item, Exception):
            raise item
        return item


def _ok(text: str = "解释完毕") -> LLMResult:
    return LLMResult(
        text=text,
        model="agnes-3.0-flash",
        usage=LLMUsage(prompt_tokens=120, completion_tokens=88),
        latency_ms=430,
    )


def _config(**overrides: Any) -> LLMConfig:
    base = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "https://example.invalid/v1/chat/completions",
        "model": "agnes-3.0-flash",
        "api_key": "k",
        "backoff_base": 0.01,
        "backoff_max": 0.05,
        "max_attempts": 3,
    }
    base.update(overrides)
    return LLMConfig(**base)  # type: ignore[arg-type]


def _request() -> LLMRequest:
    return LLMRequest(purpose="explain_structure", system="s", user="u")


# --------------------------------------------------------------------------- #
# 1. 异步：submit 立刻返回
# --------------------------------------------------------------------------- #


def test_submit_returns_before_the_call_finishes() -> None:
    """**异步是这个层存在的理由**（provider 实测延迟 0.3–7.4s）。

    用一个卡住 0.3s 的假 provider：``submit()`` 必须在它完成**之前**返回。
    """
    release = threading.Event()
    observed: list[str] = []

    class SlowClient:
        def complete(self, request: LLMRequest) -> LLMResult:
            release.wait(timeout=2.0)
            observed.append("done")
            return _ok()

    queue = LLMQueue(SlowClient(), _config())  # type: ignore[arg-type]
    try:
        started = time.monotonic()
        result = queue.submit(Job(request=_request(), call_id="c1"))
        elapsed_ms = (time.monotonic() - started) * 1000
        assert result.accepted is True
        assert elapsed_ms < 100, f"submit() 阻塞了 {elapsed_ms:.0f}ms —— 那就不叫异步"
        assert observed == [], "provider 在 submit() 返回前就被调用了"
        release.set()
        assert queue.drain(timeout=5.0)
        assert observed == ["done"]
    finally:
        release.set()
        queue.stop()


# --------------------------------------------------------------------------- #
# 2. 429 退避重入
# --------------------------------------------------------------------------- #


def test_rate_limited_requeues_instead_of_hot_looping() -> None:
    """撞 429 必须**重新排队**并等待，不能原地再打。

    原地重试对令牌桶毫无意义 —— 只会继续 429、白烧配额，还把 worker 占死。
    """
    client = FakeClient([LLMRateLimited("429"), LLMRateLimited("429"), _ok("第三次成功")])
    seen: list[tuple[str, str]] = []
    queue = LLMQueue(
        client,  # type: ignore[arg-type]
        _config(max_attempts=5),
        on_status=lambda cid, st, detail, res: seen.append((st, detail)),
    )
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        assert queue.drain(timeout=10.0)
    finally:
        queue.stop()

    statuses = [s for s, _ in seen]
    assert statuses.count(STATUS_RATE_LIMITED) == 2, statuses
    assert statuses[-1] == STATUS_OK
    assert client.calls == 3, "应该正好打了 3 次（前两次 429，第三次成功）"
    # 退避信息要能被 UI 读到
    assert any("retry_in=" in detail for _, detail in seen)


def test_backoff_grows_exponentially() -> None:
    """退避序列必须**包住** ``base × 2^n``：抖动在 0~30% 之间，不是固定倍数。

    所以断言区间而不是「相邻比值」—— jitter 会让相邻比值低到
    ``2/1.3 ≈ 1.54``，拿固定倍数断言会假红。
    """
    base = 1.0
    queue = LLMQueue(  # type: ignore[arg-type]
        FakeClient([_ok()]), _config(backoff_base=base, backoff_max=1000.0)
    )
    try:
        samples = [queue._backoff_delay(n) for n in range(5)]  # noqa: SLF001
    finally:
        queue.stop()
    for n, value in enumerate(samples):
        floor = base * (2**n)
        ceiling = floor * 1.3 + 1e-9  # +epsilon 防浮点边界
        assert floor <= value <= ceiling, (
            f"第 {n} 次退避 {value:.3f} 不在 [{floor:.1f}, {ceiling:.1f}]"
        )


def test_backoff_is_capped() -> None:
    """退避要有上限，否则第 10 次会等到几十分钟。"""
    queue = LLMQueue(  # type: ignore[arg-type]
        FakeClient([_ok()]), _config(backoff_base=1.0, backoff_max=5.0)
    )
    try:
        for n in range(12):
            assert queue._backoff_delay(n) <= 5.0 * 1.3  # noqa: SLF001
    finally:
        queue.stop()


# --------------------------------------------------------------------------- #
# 3. 重试上限
# --------------------------------------------------------------------------- #


def test_gives_up_after_max_attempts() -> None:
    """一直 429 时必须有终点 —— 否则任务永远在队列里转。"""
    client = FakeClient([LLMRateLimited("429")])  # 永远 429
    seen: list[tuple[str, str]] = []
    queue = LLMQueue(
        client,  # type: ignore[arg-type]
        _config(max_attempts=3),
        on_status=lambda cid, st, detail, res: seen.append((st, detail)),
    )
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        assert queue.drain(timeout=10.0)
    finally:
        queue.stop()

    assert client.calls == 3, f"应该只打 3 次，实际 {client.calls}"
    assert seen[-1][0] == STATUS_ERROR
    assert "rate_limited_exhausted" in seen[-1][1]


# --------------------------------------------------------------------------- #
# 4. 错误分类
# --------------------------------------------------------------------------- #


def test_non_429_failure_is_not_retried() -> None:
    """401/403 重试一万次也没用（key 无效），不该占着队列退避。"""
    client = FakeClient([LLMError("HTTP 401 —— key 无效")])
    seen: list[tuple[str, str]] = []
    queue = LLMQueue(
        client,  # type: ignore[arg-type]
        _config(),
        on_status=lambda cid, st, detail, res: seen.append((st, detail)),
    )
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        assert queue.drain(timeout=5.0)
    finally:
        queue.stop()

    assert client.calls == 1, "非 429 错误不该重试"
    # running 是每个任务的前置状态，所以序列是 [running, error]
    assert seen == [
        (STATUS_RUNNING, ""),
        (STATUS_ERROR, "HTTP 401 —— key 无效"),
    ]


def test_unexpected_exception_does_not_kill_worker() -> None:
    """provider 抛出任何异常都不能让 worker 线程死掉。"""
    client = FakeClient([RuntimeError("炸了"), _ok("后面还能跑")])
    seen: list[tuple[str, str]] = []
    queue = LLMQueue(
        client,  # type: ignore[arg-type]
        _config(),
        on_status=lambda cid, st, detail, res: seen.append((st, detail)),
    )
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        queue.submit(Job(request=_request(), call_id="c2"))
        assert queue.drain(timeout=5.0)
    finally:
        queue.stop()

    # 每个任务先发 running 再发终态：job1 → [running, error]，job2 → [running, ok]
    assert [s for s, _ in seen] == [STATUS_RUNNING, STATUS_ERROR, STATUS_RUNNING, STATUS_OK]
    assert "unexpected" in seen[1][1]


def test_result_carries_model_and_tokens_to_callback() -> None:
    """token / model 必须送到落库回调 —— 审计约束 4，不是可选项。"""
    captured: list[Any] = []
    queue = LLMQueue(
        FakeClient([_ok("中文解释")]),  # type: ignore[arg-type]
        _config(),
        on_status=lambda cid, st, detail, res: captured.append((st, detail, res)),
    )
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        assert queue.drain(timeout=5.0)
    finally:
        queue.stop()

    final = captured[-1]
    assert final[0] == STATUS_OK
    assert final[1] == "中文解释"
    assert final[2] is not None
    assert final[2].model == "agnes-3.0-flash"
    assert final[2].usage.prompt_tokens == 120
    assert final[2].usage.completion_tokens == 88


# --------------------------------------------------------------------------- #
# 5. 可关闭 / 配置诊断
# --------------------------------------------------------------------------- #


def test_disabled_queue_rejects_without_raising() -> None:
    """关掉是**常态**，不是异常 —— 不该抛，该拒收。"""
    queue = LLMQueue(FakeClient([_ok()]), _config(enabled=False))  # type: ignore[arg-type]
    try:
        result = queue.submit(Job(request=_request(), call_id="c1"))
        assert result.accepted is False
        assert result.reason == "llm_disabled"
    finally:
        queue.stop()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"enabled": False}, "llm_disabled"),
        ({"provider": "langchain"}, "llm_unknown_provider:langchain"),
        ({"base_url": ""}, "llm_missing_base_url"),
        ({"model": ""}, "llm_missing_model"),
        ({"api_key": ""}, "llm_missing_api_key"),
        ({}, ""),
    ],
)
def test_missing_reason_is_specific(overrides: dict[str, Any], expected: str) -> None:
    """诊断必须**具体**。「LLM 未配置」是最难排查的一句话。"""
    assert _config(**overrides).missing_reason() == expected


def test_redacted_never_leaks_the_key() -> None:
    view = _config(api_key="super-secret-key").redacted()
    assert view["has_api_key"] is True
    assert "super-secret-key" not in repr(view)


def test_load_config_reads_environ() -> None:
    cfg = load_config(
        {
            "CPT_LLM_ENABLED": "1",
            "CPT_LLM_MODEL": "agnes-3.0-flash",
            "CPT_LLM_BASE_URL": "https://x.invalid/v1/chat/completions",
            "CPT_LLM_API_KEY": "k",
            "CPT_LLM_MAX_ATTEMPTS": "7",
        }
    )
    assert cfg.enabled is True
    assert cfg.model == "agnes-3.0-flash"
    assert cfg.max_attempts == 7


def test_build_client_rejects_incomplete_config() -> None:
    with pytest.raises(ValueError, match="llm_missing_api_key"):
        build_client(_config(api_key=""))


# --------------------------------------------------------------------------- #
# 6. HTTP 错误分类（不打网络，只测分类函数）
# --------------------------------------------------------------------------- #


def test_http_error_classification() -> None:
    """429 与「重试无用的 4xx」必须分开，否则会对无效 key 疯狂退避。"""
    import urllib.error

    from cpt.llm.providers.openai_compatible import OpenAICompatibleClient

    def _err(status: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("u", status, "m", {}, None)  # type: ignore[arg-type]

    classify = OpenAICompatibleClient._classify_http_error  # noqa: SLF001
    assert isinstance(classify(_err(429)), LLMRateLimited)
    for status in (400, 401, 403, 404, 422):
        err = classify(_err(status))
        assert isinstance(err, LLMError)
        assert not isinstance(err, LLMRateLimited), f"{status} 不该被当成限流"
    assert isinstance(classify(_err(500)), LLMError)
    assert not isinstance(classify(_err(500)), LLMRateLimited)
