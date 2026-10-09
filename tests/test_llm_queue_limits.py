"""审计 M6 / L6 回归测试：LLM 总时长与响应字节上限、队列有界、瞬时故障重试边界。

为什么单开一个文件：M6/L6 的病灶横跨 provider 与 queue 两层，而
``tests/test_llm_layer.py`` 已含大量既有断言，不动它以免互相踩。

- **provider（M6）**：``urlopen(timeout=...)`` 只约束**单次 socket 操作**，慢速
  吐字节的服务端可让 ``resp.read()`` 永不返回。现在按块读 + ``time.monotonic()``
  总 deadline + 512 KiB 字节上限；超限**抛 ``LLMError``，不静默截断**（截断文本
  会被当完整结论写进 ``result_text`` 审计列）。
- **queue（M6）**：``_pending`` 有界，超过 ``max_pending`` 如实拒绝
  ``llm_queue_full``，不再无界堆内存。
- **provider + queue（L6）**：5xx / 连接错打 ``retryable`` 标记 ⇒ 队列退避重入；
  **超时不打标记 ⇒ 一次终态**（at-most-once，避免服务商已计费的那次被重试成两次）。

**不连库、不发真网络**：provider 用假 ``urlopen`` 注入慢速/超长响应，队列用脚本化
的假 client。
"""

from __future__ import annotations

import email.message
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import pytest
from cpt.llm.base import LLMError, LLMRateLimited, LLMRequest, LLMResult, LLMUsage
from cpt.llm.config import LLMConfig
from cpt.llm.providers.openai_compatible import OpenAICompatibleClient
from cpt.llm.queue import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_QUEUED,
    STATUS_RUNNING,
    Job,
    LLMQueue,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _config(**overrides: Any) -> LLMConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "provider": "openai_compatible",
        "base_url": "https://example.invalid/v1/chat/completions",
        "model": "agnes-3.0-flash",
        "api_key": "k",
        "timeout": 5.0,
        "max_attempts": 3,
        "backoff_base": 0.01,
        "backoff_max": 0.05,
    }
    base.update(overrides)
    return LLMConfig(**base)


def _request() -> LLMRequest:
    return LLMRequest(purpose="explain", system="s", user="u")


def _client(timeout: float = 5.0) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="https://example.invalid/v1/chat/completions",
        model="agnes-3.0-flash",
        api_key="k",
        timeout=timeout,
    )


def _ok(text: str = "解释完毕") -> LLMResult:
    return LLMResult(text=text, model="m", usage=LLMUsage(), latency_ms=1, raw={})


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid", code, "boom", email.message.Message(), None
    )


class _ChunkedResp:
    """按块吐出预先给定的字节；``delay`` 模拟慢速服务端。"""

    def __init__(self, chunks: list[bytes], *, delay: float = 0.0) -> None:
        self._chunks = chunks
        self._delay = delay
        self._index = 0

    def __enter__(self) -> _ChunkedResp:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def read(self, size: int = -1) -> bytes:
        if self._delay:
            time.sleep(self._delay)
        if self._index >= len(self._chunks):
            return b""
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class _EndlessResp:
    """永远有下一块 —— 模拟「服务端无限吐字节」的恶意/异常响应。"""

    def __init__(self, *, delay: float = 0.0) -> None:
        self._delay = delay

    def __enter__(self) -> _EndlessResp:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def read(self, size: int = -1) -> bytes:
        if self._delay:
            time.sleep(self._delay)
        return b"x" * (size if size > 0 else 1)


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, resp: Any) -> None:
    """把 provider 用的 ``urllib.request.urlopen`` 换成返回假响应的桩。"""
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp)


# --------------------------------------------------------------------------- #
# M6：provider 的字节上限与总 deadline
# --------------------------------------------------------------------------- #


def test_response_byte_cap_raises_instead_of_truncating(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计 M6：无限响应体必须在字节上限处抛错，不能一直读、也不能静默截断。"""
    _patch_urlopen(monkeypatch, _EndlessResp())
    with pytest.raises(LLMError, match="上限"):
        _client(timeout=5.0).complete(_request())


def test_total_deadline_stops_slow_drip_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计 M6：服务端每 50 ms 吐 1 字节，总 deadline 到点必须抛错返回。

    旧实现下这里会无限循环（`urlopen` 的单次 socket 超时每次都被新字节重置），
    把唯一 worker 永久卡死。
    """
    _patch_urlopen(monkeypatch, _EndlessResp(delay=0.05))
    started = time.monotonic()
    with pytest.raises(LLMError, match="总时长"):
        _client(timeout=0.2).complete(_request())
    assert time.monotonic() - started < 3.0, "总 deadline 没兜住，真的读下去了"


def test_chunked_reader_still_parses_a_normal_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：分块读不得破坏正常响应解析。"""
    payload = (
        b'{"model":"m","choices":[{"finish_reason":"stop",'
        b'"message":{"content":"hi"}}],"usage":{"prompt_tokens":1,"completion_tokens":2}}'
    )
    _patch_urlopen(
        monkeypatch,
        _ChunkedResp([payload[:10], payload[10:40], payload[40:]]),
    )
    result = _client().complete(_request())
    assert result.text == "hi"
    assert result.usage.prompt_tokens == 1
    assert result.usage.completion_tokens == 2


# --------------------------------------------------------------------------- #
# L6：provider 侧的可重试标记
# --------------------------------------------------------------------------- #


def test_5xx_is_marked_retryable() -> None:
    """审计 L6：5xx 是瞬时故障，必须能被队列识别为「安全可重试」。"""
    exc = OpenAICompatibleClient._classify_http_error(_http_error(503))
    assert isinstance(exc, LLMError)
    assert not isinstance(exc, LLMRateLimited)
    assert getattr(exc, "retryable", False) is True


def test_fatal_4xx_is_not_retryable() -> None:
    """审计 L6：401 等致命 4xx 重试无意义，不能打错标记。"""
    exc = OpenAICompatibleClient._classify_http_error(_http_error(401))
    assert getattr(exc, "retryable", False) is False


def test_429_still_rate_limited() -> None:
    """回归：429 仍走 ``LLMRateLimited`` 专用路径，不被 5xx 分支吞掉。"""
    assert isinstance(OpenAICompatibleClient._classify_http_error(_http_error(429)), LLMRateLimited)


def test_timeout_is_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计 L6：超时**刻意不重试** —— 服务商可能已受理并计费。"""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(LLMError) as caught:
        _client().complete(_request())
    assert "超时" in str(caught.value)
    assert getattr(caught.value, "retryable", False) is False


def test_connection_error_is_marked_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计 L6：连接错说明请求根本没送达，重试安全。"""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(LLMError) as caught:
        _client().complete(_request())
    assert getattr(caught.value, "retryable", False) is True


# --------------------------------------------------------------------------- #
# L6：queue 侧的重试边界
# --------------------------------------------------------------------------- #


class _ScriptClient:
    """按脚本依次返回结果或抛异常；用尽后重复最后一个。"""

    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResult:
        item = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, LLMResult), f"脚本返回值类型不对: {item!r}"
        return item


class _BlockingClient:
    """第一次 ``complete`` 会阻塞到 ``release``，用来压出队列深度。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResult:
        self.calls += 1
        self.entered.set()
        self.release.wait(5.0)
        return _ok()


#: provider 打在「安全可重试」异常上的标记名（与 openai_compatible 的实现同名）。
_RETRYABLE_ATTR = "retryable"


def _retryable(message: str) -> LLMError:
    exc = LLMError(message)
    setattr(exc, _RETRYABLE_ATTR, True)
    return exc


def _run_script(
    script: list[Any], **config_overrides: Any
) -> tuple[_ScriptClient, list[tuple[str, str]]]:
    client = _ScriptClient(script)
    seen: list[tuple[str, str]] = []
    queue = LLMQueue(
        client,
        _config(**config_overrides),
        on_status=lambda _id, status, detail, _res: seen.append((status, detail)),
    )
    try:
        queue.submit(Job(_request(), "c1"))
        assert queue.drain(timeout=5.0), "队列没有排空"
    finally:
        queue.stop()
    return client, seen


def test_retryable_transient_error_is_retried_then_succeeds() -> None:
    """审计 L6：5xx / 连接错标记过的错误要退避重入，而不是一次终态。"""
    client, seen = _run_script([_retryable("HTTP 503"), _ok()])
    assert client.calls == 2
    assert seen[-1] == (STATUS_OK, "解释完毕")
    assert any(status == STATUS_QUEUED and "retry_in=" in detail for status, detail in seen)


def test_retryable_error_gives_up_after_max_attempts() -> None:
    client, seen = _run_script([_retryable("boom")], max_attempts=2)
    assert client.calls == 2
    assert seen[-1][0] == STATUS_ERROR
    assert seen[-1][1].startswith("retryable_exhausted")


def test_plain_llm_error_is_not_retried() -> None:
    """回归：未标记的 LLMError（如 401）仍是一次终态。"""
    client, seen = _run_script([LLMError("HTTP 401 —— key 无效")])
    assert client.calls == 1
    assert seen == [(STATUS_RUNNING, ""), (STATUS_ERROR, "HTTP 401 —— key 无效")]


def test_timeout_error_is_not_retried_at_queue_level() -> None:
    """审计 L6：超时无标记 ⇒ 队列只调一次（at-most-once，避免重复计费）。"""
    client, seen = _run_script([LLMError("LLM 请求超时（总时长 30s）")])
    assert client.calls == 1
    assert seen[-1][0] == STATUS_ERROR


# --------------------------------------------------------------------------- #
# M6：队列有界
# --------------------------------------------------------------------------- #


def test_bounded_queue_rejects_when_full() -> None:
    """审计 M6：``_pending`` 满时如实拒绝 ``llm_queue_full``，不无界堆积。"""
    client = _BlockingClient()
    queue = LLMQueue(client, _config(), max_pending=1)
    try:
        assert queue.submit(Job(_request(), "running")).accepted
        assert client.entered.wait(2.0), "worker 必须已进入第一次调用"
        assert queue.submit(Job(_request(), "queued")).accepted  # 占满唯一的槽位
        rejected = queue.submit(Job(_request(), "overflow"))
        assert rejected.accepted is False
        assert rejected.reason == "llm_queue_full"
        assert rejected.call_id == "overflow"
    finally:
        client.release.set()
        queue.stop()


def test_max_pending_is_clamped_to_at_least_one() -> None:
    """``max_pending=0`` 不能变成「永远拒绝」这种自锁配置。"""
    client = _ScriptClient([_ok()])
    queue = LLMQueue(client, _config(), max_pending=0)
    try:
        assert queue.submit(Job(_request(), "c1")).accepted
    finally:
        queue.stop()
