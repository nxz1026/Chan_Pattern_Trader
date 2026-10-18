"""读路由的兜底契约（R67 / P1-3）。

## 防的是什么

``do_GET`` 里 A 股 / Trade / Track 三条**早期分发**原来不在任何 ``try`` 里
（写侧 ``do_POST`` 早在 R59 就接了 ``_handle_unexpected``）。路由体里漏一个校验，
异常就一路冒到 ``socketserver`` —— 客户端看到的是**空响应 / 连接被重置**
（``curl`` 报 ``rc=52 Empty reply``），而不是一个能读的 500，栈只落在 journal 里。

2026-10-18 现场实证：服务进程里 ``cpt.domain.market_time`` 还是启动时的旧模块，
A 股路由新加的 import 抛 ``ImportError`` ⇒ ``/api/dashboard/a-share/snapshot``
返回空响应，浏览器侧只看到「网络错误」。**这类故障的可观测性等于零**。

## 为什么用注入故障而不是等它自己复发

真实的崩溃入口（模块热更、DB 驱动版本、上游字段变化）没法在单测里自然复现，
但「路由体抛异常」这个**形状**可以精确注入。这里起真 server、打真请求、查真
状态码与 Content-Type —— 与本仓其它运行时契约门禁同一思路：测行为，不测写法。

``history_days`` 那条走**源码契约**：它的崩溃路径要先过参数解析、再进
``build_recommendation``，而后者在真库上会**写** ``cpt_recommendation``
（审计期间就曾因此误写生产 2 行）。为了不重蹈覆辙，这里只钉住「解析不再走
``str.isdigit()``」，不为了拿一个 200 去写库。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

APP_PY = Path(__file__).resolve().parents[1] / "cpt" / "web" / "app.py"


def _start_stub_server(monkeypatch: pytest.MonkeyPatch) -> tuple[object, threading.Thread]:
    """起一个 A 股读路由**必抛**的真 server（其余路由不受影响）。"""
    from cpt.web import app as web_app

    def boom(_self: object, _path: str, _query: object) -> None:
        raise RuntimeError("R67/P1-3 注入：路由体内未捕获异常")

    server = web_app.serve_snapshot(lambda: {"schema_version": "dashboard.v2"})
    monkeypatch.setattr(server.RequestHandlerClass, "_handle_a_share_get", boom, raising=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_read_route_exception_becomes_a_json_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """★回归：读路由抛异常必须变成 500 JSON，而不是断连接。

    修前：异常冒到 ``socketserver`` ⇒ ``urlopen`` 侧 ``RemoteDisconnected``
    （``http.client.RemoteDisconnected`` 是 ``ConnectionResetError`` 的子类）。
    修后：``_handle_unexpected`` 兜住并回结构化 500。
    """
    server, thread = _start_stub_server(monkeypatch)
    url = f"http://127.0.0.1:{server.server_port}/api/dashboard/a-share/snapshot?code=600519"
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url, timeout=10)  # noqa: S310 — 本机固定 127.0.0.1
        assert caught.value.code == 500
        assert caught.value.headers.get_content_type() == "application/json"
        body = json.loads(caught.value.read().decode("utf-8"))
        assert body["error"]["code"] == "internal_error"
    except urllib.error.URLError as exc:  # pragma: no cover — 老行为的形状
        if isinstance(exc, urllib.error.HTTPError):  # pragma: no cover
            raise
        pytest.fail(f"读路由又断连接了（P1-3 复发）：{exc!r}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_trade_and_track_read_routes_are_wrapped_too() -> None:
    """三条分发都要有兜底 —— 只包 A 股等于只修了 1/3。

    这条查写法（源码），与上面那条行为测试互补：行为测试证明「兜底真能回 500」，
    这里证明「三个入口都挂上了同一个兜底」。
    """
    src = APP_PY.read_text(encoding="utf-8")
    assert src.count('self._handle_unexpected("GET")') == 3, "读路由兜底必须覆盖 A 股/Trade/Track"
    for handler in ("_handle_a_share_get", "_handle_trade_get", "_handle_track_get"):
        assert f"self.{handler}(path.path, query)" in src


def test_history_days_no_longer_uses_isdigit() -> None:
    """``str.isdigit()`` 对 ``'²'`` 也返回 True，而 ``int('²')`` 抛 ``ValueError``。

    同一个坑在 ``interval_ms`` 上是用 ``try/except ValueError`` 处理的（见
    ``interval_ms_not_int``），这里也必须按同样的口径：先限 ASCII 再转。

    只看**代码行**（注释里会提到旧写法，那是留档，不算违规）。
    """
    code_lines = [
        line
        for line in APP_PY.read_text(encoding="utf-8").splitlines()
        if "history_days" in line and not line.lstrip().startswith("#")
    ]
    assert code_lines, "找不到 history_days 的赋值行 —— 路由被重构成别的形状了？"
    joined = "\n".join(code_lines)
    assert "isdigit()" in joined, "history_days 应当仍做整数形状校验"
    assert "isascii()" in joined, "必须限 ASCII：`'²'.isdigit()` 是 True 而 `int('²')` 抛"
    assert "int(hdays) if hdays.isascii() and hdays.isdigit() else 0" in joined
