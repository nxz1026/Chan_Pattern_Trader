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

import http.server
import json
import threading
import time
import urllib.error
from typing import Any

import pytest
from cpt.llm.base import LLMError, LLMRateLimited, LLMRequest, LLMResult, LLMUsage
from cpt.llm.config import LLMConfig, load_config
from cpt.llm.providers.openai_compatible import OpenAICompatibleClient
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


# --------------------------------------------------------------------------- #
# 4. 端到端：真 HTTP 429 -> 退避重入
# --------------------------------------------------------------------------- #
#
# ## 为什么要有这组
#
# 上面的分类单测是**手工构造** `urllib.error.HTTPError` 喂给
# `_classify_http_error` 的 —— 它绕过了 `complete()` 里那段
# `except urllib.error.HTTPError`。也就是说：把那个 except 删掉 / 改成
# 宽泛捕获，**现有全部测试依然全绿**，而生产环境的 429 退避会静默失效。
#
# 这组用**真 HTTP server**（`http.server`，不联网、不装依赖）跑完整链路：
# urlopen -> 真实 HTTPError -> 分类 -> LLMRateLimited -> 队列退避重入。
#
# 桩刻意复刻 agnes 的实测特征：**空响应体 + 无 Retry-After**。
# 这一点很要紧 —— 带响应体的 429 会走上一条完全不同的代码路径。


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """前 `fail_first` 次回 429（空体、无 Retry-After），之后回 200。"""

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 约定
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        state = self.server.stub  # type: ignore[attr-defined]
        with state["lock"]:
            state["calls"] += 1
            n = state["calls"]
        if n <= state["fail_first"]:
            self.send_response(429)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(
            {
                "choices": [{"message": {"content": "桩内容"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                "model": "stub-model",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def _stub_provider(fail_first: int) -> tuple[OpenAICompatibleClient, dict[str, Any]]:
    state: dict[str, Any] = {"calls": 0, "fail_first": fail_first, "lock": threading.Lock()}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    srv.stub = state  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = OpenAICompatibleClient(
        base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions",
        model="stub-model",
        api_key="stub-key",
        timeout=5.0,
    )
    return client, state


def test_real_http_429_triggers_backoff_and_recovers() -> None:
    """真 HTTP 429 必须走退避重入，并在限流解除后**真的恢复**。

    「耗尽重试」和「恢复成功」在退避次数上表现一样，只验前者验不出这条路径
    是不是真的能救回来 —— 而生产要的正是后者。
    """
    client, state = _stub_provider(fail_first=2)
    queue = LLMQueue(
        client,
        _config(max_attempts=5, backoff_base=0.05, backoff_max=0.2),
        on_status=lambda cid, st, detail, res: state.setdefault("seen", []).append((st, detail)),
    )
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        assert queue.drain(timeout=20.0), "队列没排空"
    finally:
        queue.stop()

    seen: list[tuple[str, str]] = state["seen"]
    statuses = [s for s, _ in seen]
    assert statuses.count(STATUS_RATE_LIMITED) == 2, statuses
    assert statuses[-1] == STATUS_OK, f"限流两次后应该恢复，实际 {statuses}"
    assert state["calls"] == 3, f"应正好打 3 次，实际 {state['calls']}"


def test_real_http_429_until_exhausted_terminates_cleanly() -> None:
    """一直 429 时必须**收尾**成 error，而不是无限重试或卡死 worker。"""
    client, state = _stub_provider(fail_first=99)
    queue = LLMQueue(
        client,
        _config(max_attempts=3, backoff_base=0.05, backoff_max=0.2),
        on_status=lambda cid, st, detail, res: state.setdefault("seen", []).append((st, detail)),
    )
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        assert queue.drain(timeout=20.0), "队列没排空 —— 说明退避收不了尾"
    finally:
        queue.stop()

    seen: list[tuple[str, str]] = state["seen"]
    assert seen[-1][0] == STATUS_ERROR
    assert seen[-1][1].startswith("rate_limited_exhausted"), seen[-1]
    assert state["calls"] == 3, f"应正好打 max_attempts=3 次，实际 {state['calls']}"


def test_real_http_401_is_not_retried() -> None:
    """真 HTTP 401 必须**不重试** —— 重试只是白烧配额，且永远不会成功。

    走队列而不是直接调 ``complete()``：要验的是「生产上会不会烧掉 5 次重试」，
    那是队列的职责，不是 client 的。
    """

    class _AuthHandler(_StubHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AuthHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    auth_client = OpenAICompatibleClient(
        base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions",
        model="stub-model",
        api_key="bad",
        timeout=5.0,
    )
    seen: list[tuple[str, str]] = []
    queue = LLMQueue(
        auth_client,
        _config(max_attempts=5, backoff_base=0.05, backoff_max=0.2),
        on_status=lambda cid, st, detail, res: seen.append((st, detail)),
    )
    try:
        queue.submit(Job(request=_request(), call_id="c1"))
        assert queue.drain(timeout=20.0)
    finally:
        queue.stop()
        srv.shutdown()
        srv.server_close()

    statuses = [s for s, _ in seen]
    assert statuses.count(STATUS_ERROR) == 1, f"401 应一次就结束，实际 {statuses}"
    assert STATUS_RATE_LIMITED not in statuses, "401 不该被当成限流"
    assert "HTTP 401" in seen[-1][1], seen[-1]


# --------------------------------------------------------------------------- #
# 5. 截断/空回答**不是**答案（R59 审计 M10）
# --------------------------------------------------------------------------- #
#
# 病灶：``_extract_text`` 只看 ``choices[0].message.content`` 在不在，
# **从不看 ``finish_reason``**。于是被 ``max_tokens`` 截断的半句话照常返回
# ``LLMResult``，落库时是 ``STATUS_OK`` —— 看板上一条读不通的"结论"和真结论
# 长得一模一样。同一段里 ``int(raw.get(...))`` 还会把 provider 回的 ``"N/A"``
# 变成 ``ValueError``，打穿「``complete()`` 只抛 ``LLMError``」的契约。


class _TruncatedHandler(http.server.BaseHTTPRequestHandler):
    """真 HTTP 200 + ``finish_reason=length`` + ``usage`` 里塞 ``"N/A"``。"""

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps(
            {
                "choices": [
                    {"message": {"content": "这是一句被截断的"}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": "N/A", "completion_tokens": 3},
                "model": "stub-model",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def test_truncated_answer_never_becomes_an_ok_result() -> None:
    """端到端：截断的回答必须炸成 LLMError，且队列记 ``error`` 而不是 ``ok``。"""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TruncatedHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = OpenAICompatibleClient(
        base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions",
        model="stub-model",
        api_key="k",
        timeout=5.0,
    )
    try:
        with pytest.raises(LLMError, match="finish_reason=length"):
            client.complete(_request())

        seen: list[tuple[str, str]] = []
        queue = LLMQueue(
            client,
            _config(max_attempts=2, backoff_base=0.05, backoff_max=0.2),
            on_status=lambda cid, st, detail, res: seen.append((st, detail)),
        )
        try:
            queue.submit(Job(request=_request(), call_id="c1"))
            assert queue.drain(timeout=20.0)
        finally:
            queue.stop()
    finally:
        srv.shutdown()
        srv.server_close()

    statuses = [s for s, _ in seen]
    assert statuses, "队列什么都没记"
    assert STATUS_OK not in statuses, f"截断的回答被当成成功了：{seen}"


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": [{"message": {"content": "  你好  "}, "finish_reason": "stop"}]},
        # 老网关不给 finish_reason：不能凭缺失就判失败（会误伤整条产线）
        {"choices": [{"message": {"content": "你好"}}]},
    ],
)
def test_complete_answer_still_passes(payload: dict[str, Any]) -> None:
    assert (
        OpenAICompatibleClient._extract_text(payload)  # noqa: SLF001
        == "你好"
    )


@pytest.mark.parametrize("reason", ["length", "content_filter", "tool_calls", "unknown_future"])
def test_non_stop_finish_reason_is_an_error(reason: str) -> None:
    """任何非 ``stop`` 的 `finish_reason` 都不算「正常结束的回答」。"""
    with pytest.raises(LLMError, match="finish_reason="):
        OpenAICompatibleClient._extract_text(  # noqa: SLF001
            {"choices": [{"message": {"content": "半句话"}, "finish_reason": reason}]}
        )


@pytest.mark.parametrize("content", ["", "   \n\t "])
def test_blank_content_is_an_error(content: str) -> None:
    """空回答按错误落库 —— 否则看板上一条空白"结论"与真失败不可区分。"""
    with pytest.raises(LLMError, match="为空"):
        OpenAICompatibleClient._extract_text(  # noqa: SLF001
            {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        )


def test_unparseable_usage_does_not_escape_as_value_error() -> None:
    """``"N/A"`` 这类 usage 必须降级成 0，而不是以 ``ValueError`` 逃出本模块。"""
    usage = OpenAICompatibleClient._extract_usage(  # noqa: SLF001
        {"usage": {"prompt_tokens": "N/A", "completion_tokens": None}}
    )
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert OpenAICompatibleClient._extract_usage({}).prompt_tokens == 0  # noqa: SLF001
