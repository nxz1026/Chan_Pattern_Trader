"""R59 web 层加固回归：H1/H2/H3/H4/M1/M2/M3/M5/L5。

每个用例都钉**可观测契约**，而不是实现细节：

- H1：LLM 端点按「来源 IP + 端点」滑动窗口限流（429 + ``Retry-After``），
  且 LLM 的 ``subject_id`` 由服务端按路由语义构造，请求体无从指定。
- H2：``?symbol=`` / ``?interval_ms=`` 非法时**在到达上游之前**回 400。
- H3：``?include_quota=1`` 付费探活过闸；默认路径一次都不花额度。
- H4：access log 落 ``logging``（敏感 query 值脱敏，路径保留）；并发超限 503。
- M1：A 股 snapshot GET 默认只读，补因子必须显式 ``?ensure_factors=1``。
- M2：trade 回执 Content-Type 门禁、``for_date`` 校验、回执/幂等键有界保留。
- M3：非法 code 回 400 JSON（不是连接重置）。
- M5：``limit``/``days`` 夹到有界区间并**标注截断**。
- L5：204 无 body、trade health 不回绝对路径、自选按用户分文件。

限流用例全部注入假时钟（``clock=lambda: now[0]``），**不 sleep**：窗口推进靠改
闭包里的时间变量，测试既确定又快。HTTP 层用例用真 server（``serve_snapshot``）+
真实 socket，因为要验证的正是「写的响应头 / 状态行」。
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest

# ── 测试脚手架：可定制参数的真 server + 极简 HTTP 客户端 ──────────────


@contextmanager
def _serve(provider: Any = None, **kwargs: Any) -> Iterator[Any]:
    """起真 server 并 yield 它。

    ``tests/conftest.py::served`` 不接受 ``max_workers`` / ``llm_limiter`` /
    ``quota_limiter``，而这三样正是 H1/H3/H4 要注入的东西，所以这里另起一个
    局部夹具（仍然走生产入口 :func:`cpt.web.app.serve_snapshot`）。
    """
    from cpt.web.app import serve_snapshot

    if provider is None:
        provider = lambda: {"schema_version": "dashboard.v2"}  # noqa: E731
    server = serve_snapshot(provider, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _url(server: Any) -> str:
    return f"http://127.0.0.1:{server.server_port}"


class _Resp:
    """``urlopen`` 的极简包装：状态码 / 原始 body / 响应头。"""

    def __init__(self, status: int, body: bytes, headers: Any) -> None:
        self.status = status
        self.body = body
        self.headers = headers

    def json(self) -> Any:
        return json.loads(self.body or b"{}")


def _request(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    content_type: str | None = "application/json",
    headers: dict[str, str] | None = None,
) -> _Resp:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None and content_type is not None:
        req.add_header("Content-Type", content_type)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _Resp(resp.status, resp.read(), resp.headers)
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 是**被测契约**本身（415/429/503），不是测试错误。
        return _Resp(exc.code, exc.read(), exc.headers)


def _error_code(resp: _Resp) -> str:
    return str(resp.json()["error"]["code"])


# ── H1：LLM 端点限流 + subject_id 服务端构造 ─────────────────────────


def test_sliding_window_limiter_uses_injected_clock() -> None:
    """限流窗口靠注入时钟推进（不 sleep）：第 N+1 次拒、窗口过去后放行。"""
    from cpt.web.rate_limit import SlidingWindowLimiter

    now = [1000.0]
    limiter = SlidingWindowLimiter(max_events=2, window_seconds=30.0, clock=lambda: now[0])

    assert limiter.check("k").allowed is True
    assert limiter.check("k").allowed is True
    rejected = limiter.check("k")
    assert rejected.allowed is False
    assert rejected.retry_after == 30
    # 被拒的那次**不记账**：窗口一到就该立刻恢复，而不是被自己的重试拖长。
    now[0] += 30.0
    assert limiter.check("k").allowed is True


def test_llm_endpoint_returns_429_with_retry_after() -> None:
    """LLM 写端点超限回 429 + ``Retry-After``，且请求不再往下走。"""
    from cpt.web.rate_limit import SlidingWindowLimiter

    now = [500.0]
    limiter = SlidingWindowLimiter(max_events=1, window_seconds=45.0, clock=lambda: now[0])
    with _serve(llm_limiter=limiter) as server:
        url = f"{_url(server)}/api/dashboard/a-share/llm/explain?code=ZZZZZZ"
        first = _request(url, method="POST", body={"lines": []})
        # 第 1 次过了闸，才轮到 code 校验（M3）——证明限流确实拦在路由入口。
        assert first.status == 400
        assert _error_code(first) == "invalid_code"

        second = _request(url, method="POST", body={"lines": []})
        assert second.status == 429
        assert second.headers["Retry-After"] == "45"
        assert _error_code(second) == "rate_limited"

        # 假时钟推进一个窗口 → 立即恢复（同一把 limiter，同一 IP）。
        now[0] += 45.0
        third = _request(url, method="POST", body={"lines": []})
        assert third.status == 400


def test_llm_explain_subject_id_is_server_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """``subject_id`` 由服务端构造：body 里的 ``track:victim:...`` 进不了审计主体。"""
    from cpt.web import a_share_routes

    captured: dict[str, Any] = {}

    class _FakeClient:
        def _get_conn(self) -> object:
            return object()

        def close(self) -> None:
            return None

    def _fake_explain(_conn: object, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"available": True, "call_id": "call-1", "status": "queued"}

    monkeypatch.setattr(a_share_routes, "_names", lambda codes: {})
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", _FakeClient)
    monkeypatch.setattr("cpt.application.llm_cases.explain_structure", _fake_explain)

    out = a_share_routes.submit_llm_explain("600519", {"id": "track:victim:000002"})

    assert out["available"] is True
    assert captured["subject_id"] == "a_share:explain:600519"
    assert not str(captured["subject_id"]).startswith("track:")


def test_llm_summarize_subject_id_is_server_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """summarize 同款：body 里的 ``subject_id``/``id`` 一律不采信。"""
    from cpt.web import a_share_routes

    captured: dict[str, Any] = {}

    class _FakeClient:
        def _get_conn(self) -> object:
            return object()

        def close(self) -> None:
            return None

    def _fake_summarize(_conn: object, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"available": True, "call_id": "call-2", "status": "queued"}

    monkeypatch.setattr(a_share_routes, "_names", lambda codes: {})
    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", _FakeClient)
    monkeypatch.setattr("cpt.application.llm_cases.summarize_recommendation", _fake_summarize)

    out = a_share_routes.submit_llm_summarize(
        "600519",
        {
            "subject_id": "track:victim:000002",
            "id": "track:victim:000002",
            "headline": "结构向上",
        },
    )

    assert out["available"] is True
    assert captured["subject_id"] == "a_share:summarize:600519"
    assert not str(captured["subject_id"]).startswith("track:")


# ── H2 / M3：非法入参在到达上游之前被拒 ──────────────────────────────


def test_snapshot_rejects_invalid_symbol_before_provider() -> None:
    """非法 ``?symbol=`` 回 400，且 provider 一个方法都没被调用。"""
    touched: list[str] = []

    class _Provider:
        def snapshot_payload(self) -> dict[str, Any]:
            # 快照构建是**任何** /snapshot 请求都要走的读取路径（唯一的
            # ``?symbol=`` 写路径是下面的 select_symbol/force_refresh）。
            # 这两条用例要证明的正是：形状非法时**连切换都没发生**。
            return {"schema_version": "dashboard.v2"}

        def select_symbol(self, symbol: str, interval: str) -> None:
            touched.append(f"select:{symbol}:{interval}")

        def force_refresh(self) -> None:
            touched.append("refresh")

    with _serve(_Provider()) as server:
        resp = _request(f"{_url(server)}/api/dashboard/snapshot?symbol=../etc/passwd")

    assert resp.status == 400
    assert _error_code(resp) == "invalid_symbol"
    assert touched == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("symbol=BTCUSDT&interval_ms=abc", "interval_ms_not_int"),
        ("symbol=BTCUSDT&interval_ms=0", "invalid_interval_ms"),
        ("symbol=BTCUSDT&interval_ms=-5", "invalid_interval_ms"),
    ],
)
def test_snapshot_rejects_invalid_interval_ms(query: str, expected: str) -> None:
    touched: list[str] = []

    class _Provider:
        def snapshot_payload(self) -> dict[str, Any]:
            return {"schema_version": "dashboard.v2"}

        def select_symbol(self, symbol: str, interval: str) -> None:
            touched.append(f"select:{symbol}:{interval}")

        def force_refresh(self) -> None:
            touched.append("refresh")

    with _serve(_Provider()) as server:
        resp = _request(f"{_url(server)}/api/dashboard/snapshot?{query}")

    assert resp.status == 400
    assert _error_code(resp) == expected
    assert touched == []


def test_llm_summarize_invalid_code_returns_400_not_connection_reset() -> None:
    """M3：summarize 修前**完全不校验** code。现在回 400 JSON。"""
    with _serve() as server:
        resp = _request(
            f"{_url(server)}/api/dashboard/a-share/llm/summarize?code=ZZZZZZ",
            method="POST",
            body={"headline": "x"},
        )

    assert resp.status == 400
    assert _error_code(resp) == "invalid_code"


def test_ashare_snapshot_invalid_code_returns_400() -> None:
    """M3（GET 侧）：非法 code 不再直穿成连接重置。"""
    with _serve() as server:
        resp = _request(f"{_url(server)}/api/dashboard/a-share/snapshot?code=ZZZZZZ")

    assert resp.status == 400
    assert _error_code(resp) == "invalid_code"


def test_sources_rejects_unknown_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """H2（同文件另一处「原样透传」）：未知 markets 显式 400，不静默成"都没问题"。"""
    from cpt.adapters import source_registry

    monkeypatch.setattr(
        source_registry,
        "capabilities_payload",
        lambda *, include_quota, markets: {"sources": []},
    )
    with _serve() as server:
        resp = _request(f"{_url(server)}/api/dashboard/sources?markets=nowhere")

    assert resp.status == 400
    assert _error_code(resp) == "unknown_market"


# ── H3：付费探活过闸，默认路径不花额度 ───────────────────────────────


def _count_probe_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[bool, Any]]:
    """把付费探活换成假实现（真实现会打真实数据源），并返回调用记录。"""
    from cpt.adapters import source_registry

    calls: list[tuple[bool, Any]] = []

    def _fake_capabilities(*, include_quota: bool, markets: Any) -> dict[str, Any]:
        calls.append((include_quota, markets))
        return {"sources": []}

    monkeypatch.setattr(source_registry, "capabilities_payload", _fake_capabilities)
    return calls


def test_sources_default_path_never_spends_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """不带 ``include_quota`` 时：一次探活都不发，也不占用付费闸。"""
    from cpt.web.rate_limit import SlidingWindowLimiter

    calls = _count_probe_calls(monkeypatch)
    # 付费闸只允许 1 次/5 分钟；默认路径连跑 3 次都不该被限流（根本没花额度）。
    limiter = SlidingWindowLimiter(max_events=1, window_seconds=300.0, clock=lambda: 0.0)
    with _serve(quota_limiter=limiter) as server:
        url = f"{_url(server)}/api/dashboard/sources"
        for _ in range(3):
            assert _request(url).status == 200

    assert [flag for flag, _ in calls] == [False, False, False]


def test_sources_include_quota_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 ``?include_quota=1`` 才走付费闸：第 3 次 429，且不多探一次。"""
    from cpt.web.rate_limit import SlidingWindowLimiter

    calls = _count_probe_calls(monkeypatch)
    now = [10.0]
    limiter = SlidingWindowLimiter(max_events=2, window_seconds=300.0, clock=lambda: now[0])
    with _serve(quota_limiter=limiter) as server:
        url = f"{_url(server)}/api/dashboard/sources?include_quota=1"
        assert _request(url).status == 200
        assert _request(url).status == 200
        throttled = _request(url)
        assert throttled.status == 429
        assert throttled.headers["Retry-After"] == "300"
        # 真探活只发生了 2 次（被拒的那次绝不落到上游）。
        assert [flag for flag, _ in calls] == [True, True]

        now[0] += 300.0
        assert _request(url).status == 200
    assert [flag for flag, _ in calls] == [True, True, True]


# ── H4：access log + 并发上限 ────────────────────────────────────────


def test_access_log_redacts_secrets_but_keeps_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """access log 必须真接上：`?token=SECRET` 不落盘，路径与普通参数照记。"""
    _count_probe_calls(monkeypatch)
    # 日志器名以 app.py 的 ``_LOG`` 为准（``cpt.web.handler``）。
    with caplog.at_level(logging.INFO, logger="cpt.web.handler"):
        with _serve() as server:
            resp = _request(f"{_url(server)}/api/dashboard/sources?token=SECRET&include_quota=1")
            assert resp.status == 200

    text = caplog.text
    assert "SECRET" not in text
    # 值被换成 ``***``（``urlencode`` 后是 ``%2A%2A%2A``），键名与其余参数照记：
    # 「谁在什么时候打了哪个路由」这条排障线索不能因为脱敏一起没了。
    assert "token=%2A%2A%2A" in text
    assert "/api/dashboard/sources?" in text
    assert "include_quota=1" in text
    assert "access" in text


def test_bounded_server_returns_503_when_workers_exhausted() -> None:
    """并发上限：占满名额的第二个连接当场 503 + ``Retry-After``，不排队。"""
    started = threading.Event()
    release = threading.Event()

    def provider() -> dict[str, Any]:
        started.set()
        release.wait(10)
        return {"schema_version": "dashboard.v2"}

    with _serve(provider, max_workers=1) as server:
        url = f"{_url(server)}/api/dashboard/snapshot"
        first: dict[str, Any] = {}

        def _call() -> None:
            first["resp"] = _request(url)

        thread = threading.Thread(target=_call)
        thread.start()
        try:
            # 名额被第一条请求占住（provider 已在执行）后，第二条必被拒。
            assert started.wait(10)
            second = _request(url)
            assert second.status == 503
            assert second.headers["Retry-After"] == "1"
            assert _error_code(second) == "server_busy"
            assert server.rejected_connections == 1
        finally:
            release.set()
            thread.join(timeout=10)

    assert first["resp"].status == 200
    # 名额用完会归还：server_close 后不应抛 BoundedSemaphore 释放异常。
    assert server.max_workers == 1


def test_serve_snapshot_defaults_to_32_workers() -> None:
    """生产入口默认必须有并发上限（默认 32），不能退回无界的 ThreadingHTTPServer。"""
    from cpt.web.app import _DEFAULT_MAX_WORKERS, serve_snapshot

    # 返回标注是 ``ThreadingHTTPServer``（子类满足该类型），max_workers 在子类上。
    server: Any = serve_snapshot(lambda: {"schema_version": "dashboard.v2"})
    try:
        assert _DEFAULT_MAX_WORKERS == 32
        assert server.max_workers == 32
    finally:
        server.server_close()


# ── M1：GET 默认只读，补因子必须显式 ─────────────────────────────────


def test_ashare_snapshot_get_factor_ensure_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """三档语义：不带宽参数 → ``None``（默认只读）；``1`` → True；其它非空 → False。"""
    from cpt.web import a_share_routes

    seen: list[bool | None] = []

    def _fake_snapshot(
        code: str, *, width_k: int, ensure_factors: bool | None = None
    ) -> dict[str, Any]:
        seen.append(ensure_factors)
        return {"schema_version": "ashare_snapshot.v1", "code": code}

    monkeypatch.setattr(a_share_routes, "snapshot_payload", _fake_snapshot)
    with _serve() as server:
        base = _url(server)
        for query in ("", "&ensure_factors=1", "&ensure_factors=0"):
            resp = _request(f"{base}/api/dashboard/a-share/snapshot?code=600519{query}")
            assert resp.status == 200

    assert seen == [None, True, False]


def test_snapshot_payload_maps_ensure_factors_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ensure_factors`` 三档映射到 ensurer：None 默认只读（default=False）。"""
    from cpt.application import a_share_snapshot
    from cpt.web import a_share_routes

    defaults: list[bool] = []
    used: list[Any] = []
    sentinel = object()

    def _ensurer(*, default: bool) -> Any:
        defaults.append(default)
        return sentinel

    def _fake_build(code: str, *, width_k: int, ensure_factors: Any) -> dict[str, Any]:
        used.append(ensure_factors)
        return {"code": code}

    monkeypatch.setattr(a_share_snapshot, "factor_ensurer_from_env", _ensurer)
    monkeypatch.setattr(a_share_snapshot, "build_ashare_snapshot", _fake_build)

    a_share_routes.snapshot_payload("600519", ensure_factors=None)
    a_share_routes.snapshot_payload("600519", ensure_factors=True)
    a_share_routes.snapshot_payload("600519", ensure_factors=False)

    # None → default=False（GET 默认只读）；True → default=True（显式要补）；
    # False → 连 ensurer 都不构造（ensurer=None）。
    assert defaults == [False, True]
    assert used == [sentinel, sentinel, None]


# ── M2：trade 回执门禁 + 有界保留 ───────────────────────────────────


def test_trade_results_rejects_non_json_content_type() -> None:
    with _serve() as server:
        resp = _request(
            f"{_url(server)}/api/trade/results",
            method="POST",
            body={"batch_id": "b1", "for_date": "2026-10-08"},
            content_type="application/x-www-form-urlencoded",
        )

    assert resp.status == 415
    assert _error_code(resp) == "content_type_required"


def test_trade_results_validates_for_date(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``for_date`` 必须可解析且在保留窗口内；合法回执照旧落盘一次。"""
    monkeypatch.setenv("CPT_TRADE_DIR", str(tmp_path))
    with _serve() as server:
        url = f"{_url(server)}/api/trade/results"
        bad = _request(url, method="POST", body={"batch_id": "b1", "for_date": "2026-13-40"})
        assert bad.status == 400
        assert bad.json()["error"] == "invalid for_date"

        future = _request(url, method="POST", body={"batch_id": "b2", "for_date": "2099-01-01"})
        assert future.status == 400
        assert future.json()["error"] == "for_date out of retention window"

        ok = _request(
            url,
            method="POST",
            body={"batch_id": "b3", "for_date": date.today().isoformat(), "trades": []},
        )
        assert ok.status == 200 and ok.json()["batch_id"] == "b3"

    assert len(list(tmp_path.glob("results_*.json"))) == 1


def test_prune_results_files_keeps_within_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """过期回执被删；文件名不像回执的一律不动（删除操作宁可少删）。"""
    from cpt.web import trade_api

    monkeypatch.setenv("CPT_TRADE_DIR", str(tmp_path))
    old = tmp_path / "results_2020-01-01_000000.json"
    old.write_text("{}", encoding="utf-8")
    junk = tmp_path / "results_not-a-date_000000.json"
    junk.write_text("{}", encoding="utf-8")
    fresh = tmp_path / f"results_{date.today().isoformat()}_120000.json"
    fresh.write_text("{}", encoding="utf-8")

    removed = trade_api._prune_results_files(today=date(2026, 10, 8))

    assert removed == 1
    assert old.exists() is False
    assert junk.exists() is True
    assert fresh.exists() is True


def test_prune_processed_caps_and_keeps_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    """``processed`` 幂等键有上限（只留最新），不会无限增长。"""
    from cpt.web import trade_api

    monkeypatch.setattr(trade_api, "_MAX_PROCESSED_BATCHES", 3)
    state: dict[str, Any] = {"processed": ["a", "b", "c", "d", "e"]}

    trade_api._prune_processed(state)

    assert state["processed"] == ["c", "d", "e"]


# ── M5：limit / days 有界 + 截断可见 ─────────────────────────────────


def test_inspection_limit_clamped_and_truncation_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``?limit=99999`` 夹到 500；distinct (market,symbol) 超上限时标注截断。

    R59（审计 M5 收尾）：趋势读的是**一条**批量查询，不再每个 key 一次往返。
    """
    from cpt.storage import run_metric_store as rms

    class _FakeClient:
        def _get_conn(self) -> object:
            return object()

        def close(self) -> None:
            return None

    rows = [{"market": "a_share", "symbol": f"{600000 + i:06d}"} for i in range(25)]
    bulk_calls: list[tuple[list[tuple[str, str]], int]] = []

    def _fake_bulk(
        _conn: object, keys: list[tuple[str, str]], limit: int
    ) -> dict[tuple[str, str], dict[str, Any]]:
        bulk_calls.append((list(keys), limit))
        return {
            key: {"market": key[0], "symbol": key[1], "samples": 0, "changes": []} for key in keys
        }

    def _bomb(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("N+1 回归：inspection 又逐个 key 调 waterline_trend 了")

    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", _FakeClient)
    monkeypatch.setattr(rms, "latest_inspection", lambda conn: None)
    monkeypatch.setattr(rms, "recent_metrics", lambda conn, *, kind, limit: rows)
    monkeypatch.setattr(rms, "waterline_trends_bulk", _fake_bulk)
    monkeypatch.setattr(rms, "waterline_trend", _bomb)

    with _serve() as server:
        resp = _request(f"{_url(server)}/api/dashboard/inspection?limit=99999")

    assert resp.status == 200
    payload = resp.json()
    assert payload["available"] is True
    assert payload["limits"]["limit"] == 500
    assert payload["limits"]["max_limit"] == 500
    assert payload["limits"]["max_trend_keys"] == 20
    assert payload["limits"]["trend_keys_truncated"] == 5
    # 25 个 distinct key 被上限夹到 20 个，且只发一条 SQL（每个 key 各取 limit 行）。
    assert len(bulk_calls) == 1
    assert len(bulk_calls[0][0]) == 20
    assert bulk_calls[0][1] == 500
    assert len(payload["trends"]) == 20


def test_structure_events_and_signal_stats_clamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """structure-events / timeline 的 ``limit`` 与 signal-stats 的 ``days`` 都夹紧。"""
    from cpt.web import app as web_app

    seen: dict[str, int] = {}

    def _events(*, limit: int, event_type: Any, kind: Any) -> dict[str, Any]:
        seen["events_limit"] = limit
        return {"events": []}

    def _timeline(structure_id: str, *, limit: int) -> dict[str, Any]:
        seen["timeline_limit"] = limit
        return {"timeline": []}

    def _stats(days: int, code: str | None) -> dict[str, Any]:
        seen["days"] = days
        return {"stats": []}

    monkeypatch.setattr(web_app, "_structure_events_payload", _events)
    monkeypatch.setattr(web_app, "_structure_timeline_payload", _timeline)
    monkeypatch.setattr(web_app, "_signal_stats_payload", _stats)

    with _serve() as server:
        base = _url(server)
        assert _request(f"{base}/api/dashboard/structure-events?limit=99999").status == 200
        assert (
            _request(
                f"{base}/api/dashboard/structure-events/timeline?structure_id=x&limit=99999"
            ).status
            == 200
        )
        assert _request(f"{base}/api/dashboard/signal-stats?days=99999").status == 200
        # 非整数 days 仍是"调用错误"→ 400，而不是被当成降级。
        assert _request(f"{base}/api/dashboard/signal-stats?days=abc").status == 400

    assert seen == {"events_limit": 500, "timeline_limit": 500, "days": 3650}


# ── L5：204 无 body / health 不回绝对路径 / 自选按用户分文件 ──────────


def test_204_response_has_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """204 必须无 body、无 ``Content-Length``（RFC 7230 §3.3.2）。"""
    from cpt.web import track_api

    monkeypatch.setattr(track_api, "handle_track_remove", lambda user, body: ({"ok": True}, 204))
    with _serve() as server:
        resp = _request(
            f"{_url(server)}/api/dashboard/track/remove",
            method="POST",
            body={"code": "600519"},
        )

    assert resp.status == 204
    assert resp.body == b""
    assert resp.headers.get("Content-Length") is None


def test_watchlist_path_is_per_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """自选按用户分文件；老路径（default）零变化；user 不能穿越目录。"""
    from cpt.web import a_share_routes

    base = tmp_path / "watchlist.json"
    monkeypatch.setattr(a_share_routes, "DEFAULT_WATCHLIST_PATH", base)

    assert a_share_routes.watchlist_path_for("") == base
    assert a_share_routes.watchlist_path_for("default") == base

    alice = a_share_routes.watchlist_path_for("alice")
    bob = a_share_routes.watchlist_path_for("bob")
    assert alice == tmp_path / "watchlist-alice.json"
    assert bob != alice

    evil = a_share_routes.watchlist_path_for("../../etc/passwd")
    # 分隔符被换成 ``_``（``..`` 只是文件名的普通字符，不是路径分量），
    # 解析出来的父目录仍是自选目录 —— 这才是"穿越不出去"的可观测判据。
    assert evil.parent == tmp_path
    assert "/" not in evil.name
    assert evil.resolve().parent == tmp_path.resolve()


def test_trade_health_hides_absolute_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """health 不回绝对路径（无鉴权端点，不泄露主机布局）。"""
    monkeypatch.setenv("CPT_TRADE_DIR", str(tmp_path / "trade_cpt"))
    with _serve() as server:
        resp = _request(f"{_url(server)}/api/trade/health")

    assert resp.status == 200
    payload = resp.json()
    assert payload["state_dir"] == "trade_cpt"
    assert str(tmp_path) not in json.dumps(payload)
