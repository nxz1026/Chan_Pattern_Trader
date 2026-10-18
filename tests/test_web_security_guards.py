"""读/写端点的三个**安全面**契约（R67 / 审计 P2-7、P2-8、P2-9）。

三个条目同一个形状：**入口没有把关，而失败模式是静默的**。

- **P2-9**：`GET /api/dashboard/track/maintenance?apply=1` 是全表全用户的破坏性
  清理，服务又在 loopback 上不带凭据 ⇒ 第三方页面的
  `<img src="http://127.0.0.1:8010/api/dashboard/track/maintenance?apply=1">`
  就能让用户的浏览器替它删数据。GET 是安全方法，浏览器也不会为它做预检。
- **P2-7**：`POST /api/dashboard/track/{code}/speak` 是第三个**付费** LLM 端点，
  而 `llm/summarize`、`llm/explain` 都在付费调用前显式限流，只有它没有 ——
  唯一的护栏是 6h bucket 去重，改一个字符就是一次新的付费调用。
- **P2-8**：`GET …/a-share/recommendation` 的 `level` 是**客户端可控的写键**
  （进 `_persist_recommendation` 的去重键与 `cpt_recommendation.level` 列），
  却改变不了任何计算 ⇒ `?level=<随机串>` 每次都是一次无条件 INSERT+COMMIT。

这里全部打真 HTTP（真 server、真状态码、真 Content-Type），只在**副作用边界**
上打桩：维护端点与付费端点都用替身记录调用，既证明「该拦的拦住了」，
也证明「放行时确实走到了处理函数」。
"""

from __future__ import annotations

import itertools
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from cpt.web.rate_limit import SlidingWindowLimiter

APP_PY = Path(__file__).resolve().parents[1] / "cpt" / "web" / "app.py"


@contextmanager
def _running(monkeypatch: pytest.MonkeyPatch, **kwargs: Any):
    """起一个真 server（只在 127.0.0.1，端口由内核分配）。"""
    from cpt.web import app as web_app

    server = web_app.serve_snapshot(lambda: {"schema_version": "dashboard.v2"}, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(
    url: str, *, headers: dict[str, str] | None = None, method: str = "GET"
) -> tuple[int, dict[str, Any]]:
    data = b"" if method == "POST" else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — 本机
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ── P2-9：破坏性维护必须带确认头 ────────────────────────────────────────────


def _spy_maintenance(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from cpt.web import track_api

    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> tuple[dict[str, Any], int]:
        calls.append(kwargs)
        return {
            "ok": True,
            "dry_run": not kwargs.get("apply"),
            "applied": bool(kwargs.get("apply")),
        }, 200

    monkeypatch.setattr(track_api, "handle_track_maintenance", fake, raising=True)
    return calls


def test_maintenance_apply_without_confirm_header_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★回归：`?apply=1` 缺 `X-CPT-Maintenance: 1` ⇒ 403，**且不许走到 handler**。

    修前：第三方页面的 `<img src=…?apply=1>` 就能触发全表全用户删除。
    修后：浏览器带不上自定义头（`<img>`/表单都带不上）⇒ 该请求被挡在门外，
    而 urllib/curl 加一行即可通过（见 `deploy/cron/track-maintenance-daily.sh`）。
    """
    calls = _spy_maintenance(monkeypatch)
    with _running(monkeypatch) as server:
        status, body = _request(
            f"http://127.0.0.1:{server.server_port}/api/dashboard/track/maintenance?apply=1"
        )
    assert status == 403, body
    assert body["error"]["code"] == "maintenance_needs_confirmation", body
    assert calls == [], "缺确认头时绝不允许走到真删"


def test_maintenance_dry_run_needs_no_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """dry-run（不带 `apply`）不删东西 ⇒ 不需要头，也不能被这条修法误伤。"""
    calls = _spy_maintenance(monkeypatch)
    with _running(monkeypatch) as server:
        status, body = _request(
            f"http://127.0.0.1:{server.server_port}/api/dashboard/track/maintenance"
        )
    assert status == 200, body
    assert body["dry_run"] is True
    assert calls == [{"apply": False}]


def test_maintenance_apply_with_header_reaches_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带上确认头 ⇒ 真删照常（不能为了安全把 cron 的作业打瘸）。"""
    calls = _spy_maintenance(monkeypatch)
    with _running(monkeypatch) as server:
        status, body = _request(
            f"http://127.0.0.1:{server.server_port}/api/dashboard/track/maintenance?apply=1",
            headers={"X-CPT-Maintenance": "1"},
        )
    assert status == 200, body
    assert body["applied"] is True
    assert calls == [{"apply": True}]


def test_confirm_header_value_must_be_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """不认识的写法宁可少删一次；但头值的 incidental 空白照收。

    `X-CPT-Maintenance: 1 ` 会被 `.strip()`（以及 HTTP 头解析）归一成 `1` ⇒ 放行。
    这是**有意的宽松**：真正挡住第三方页面的是「浏览器带不上自定义头」，
    不是值本身多严；对 `1` 苛刻到连尾随空格都拒，只会让 cron/运维脚本白白踩坑。
    """
    calls = _spy_maintenance(monkeypatch)
    with _running(monkeypatch) as server:
        url = f"http://127.0.0.1:{server.server_port}/api/dashboard/track/maintenance?apply=1"
        statuses = [
            _request(url, headers={"X-CPT-Maintenance": value})[0]
            for value in ("true", "yes", "1 ", "")
        ]
    assert statuses == [403, 403, 200, 403], statuses
    # 被拒的三次一个都没走到 handler；只有归一后等于 "1" 的那次进去了
    assert calls == [{"apply": True}]


# ── P2-7：付费端点先限流 ────────────────────────────────────────────────────


def test_speak_is_rate_limited_before_the_paid_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """★回归：`speak` 超限回 429，且**被限流的那次不许走到付费调用**。"""
    from cpt.web import track_api

    paid: list[str] = []

    def fake_speak(user_id: str, code: str) -> tuple[dict[str, Any], int]:
        paid.append(code)
        return {"ok": True}, 200

    monkeypatch.setattr(track_api, "handle_track_speak", fake_speak, raising=True)

    ticks = itertools.count(0, 0.05)
    limiter = SlidingWindowLimiter(
        max_events=2, window_seconds=600.0, clock=lambda: float(next(ticks))
    )
    with _running(monkeypatch, llm_limiter=limiter) as server:
        url = f"http://127.0.0.1:{server.server_port}/api/dashboard/track/600519/speak"
        results = [_request(url, method="POST") for _ in range(3)]

    assert [status for status, _ in results] == [200, 200, 429], results
    assert results[-1][1]["error"]["code"] == "rate_limited", results[-1]
    assert paid == ["600519", "600519"], "被限流的那次绝不能走到付费调用"


def test_every_paid_llm_entry_point_limits_first() -> None:
    """源码契约：付费路由必须**都**在调用前过 limiter（漏一个就是白名单破洞）。

    行为测试只能证明 `speak` 这一次；这条钉住「三条付费入口都在」，新增付费端点
    时如果忘了限流，这里会红。
    """
    src = APP_PY.read_text(encoding="utf-8")
    for label in ("llm_summarize", "llm_explain", "track_speak"):
        assert f'_rate_limit_or_reject(_llm_limiter, "{label}")' in src, label


# ── P2-8：只读 GET 的客户端可控写键 ─────────────────────────────────────────


def test_recommendation_ignores_the_client_supplied_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★回归：`?level=<随机串>` 不许再进留痕路径（它是写键，却改不了任何计算）。

    修前：`level` 一路传到 `build_recommendation(level=…)` ⇒ 进
    `_persist_recommendation` 的去重键 `(code, level)` ⇒ 每个随机串都是一次
    无条件 `INSERT+COMMIT` 到共享生产库（只读 GET 被用来放大写入）。
    """
    from cpt.web import a_share_routes

    seen: list[dict[str, Any]] = []

    def fake_build(code: str, **kwargs: Any) -> dict[str, Any]:
        seen.append({"code": code, **kwargs})
        return {"available": True, "code": code}

    monkeypatch.setattr(a_share_routes, "build_recommendation", fake_build, raising=True)

    with _running(monkeypatch) as server:
        base = f"http://127.0.0.1:{server.server_port}/api/dashboard/a-share/recommendation"
        first = _request(f"{base}?code=600519&level=1d")
        second = _request(f"{base}?code=600519&level=zzz-%E9%9A%8F%E6%9C%BA")

    assert first[0] == 200 and second[0] == 200
    assert len(seen) == 2
    for call in seen:
        # 唯一被去掉的就是 level；history_days 是 int 窗口参数（不进任何写键）
        assert "level" not in call, f"客户端 level 又透传了：{call}"
        assert call == {"code": "600519", "history_days": 0}
