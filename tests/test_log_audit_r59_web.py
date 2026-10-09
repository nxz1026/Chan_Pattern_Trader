"""2026-10-09 运行日志巡检 A/B：CPT web 层两条护栏。

- **A**：``/api/dashboard/track/{add,remove,restore}`` 收到畸形 / 空 / 非对象 JSON
  必须回 400 ``invalid_json``。修前用 ``try/except Exception`` 包着
  ``_read_json_body()``，而它的契约是「读不到就返回 None、不抛」——None 直接传进
  ``handle_track_*`` 触发 AttributeError，再被下面的宽 except 变成 500。journal 里
  表现为客户端一字不写就收到 500。
- **B**：客户端提前断连（``BrokenPipeError`` / ``ConnectionResetError``）不再由
  ``socketserver.BaseServer.handle_error`` 往 stderr 打整段 Traceback；**其它异常
  必须仍然打出来**（这条断言同样重要：不能靠吞异常换安静）。

两个用例组都钉可观测行为：A 走真 server + 真 socket（验的是状态码），B 直接驱动
``handle_error``（验的是 stderr 与日志记录），不需要制造真的断连竞态。
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any

import pytest

from tests.conftest import served

_TRACK_POST_PATHS = (
    "/api/dashboard/track/add",
    "/api/dashboard/track/remove",
    "/api/dashboard/track/restore",
)

#: (标签, body)。空 body 走 ``Content-Length: 0`` 分支；``null`` 是合法 JSON 但
#: 解出来是 None；``[]`` / ``"abc"`` 解出来不是 dict ⇒ 三者都必须 400。
_BODIES: tuple[tuple[str, bytes], ...] = (
    ("empty", b""),
    ("invalid", b"{not json"),
    ("null", b"null"),
    ("array", b"[]"),
    ("string", b'"abc"'),
)


def _post(base: str, path: str, data: bytes) -> tuple[int, bytes]:
    """POST 一次，4xx/5xx 不抛出，统一返回 ``(状态码, body)``。"""
    req = urllib.request.Request(f"{base}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.mark.parametrize("path", _TRACK_POST_PATHS)
@pytest.mark.parametrize(("label", "data"), _BODIES, ids=[item[0] for item in _BODIES])
def test_malformed_track_body_is_400_not_500(path: str, label: str, data: bytes) -> None:
    with served() as base:
        status, raw = _post(base, path, data)
    assert status == 400, f"{path} body={label}: 期望 400，实得 {status} / {raw!r}"
    payload = json.loads(raw)
    assert payload["error"]["code"] == "invalid_json"


class _NullHandler(BaseHTTPRequestHandler):
    """仅用于构造 server；这些用例不真的处理请求。"""


def _bounded_server() -> Any:
    from cpt.web.app import _BoundedThreadingHTTPServer

    return _BoundedThreadingHTTPServer(("127.0.0.1", 0), _NullHandler)


@pytest.mark.parametrize("exc_type", [BrokenPipeError, ConnectionResetError])
def test_client_disconnect_is_not_printed_as_traceback(
    exc_type: type[OSError],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = _bounded_server()
    try:
        try:
            raise exc_type("client went away")
        except exc_type:
            with caplog.at_level(logging.DEBUG, logger="cpt.web.handler"):
                server.handle_error(None, ("127.0.0.1", 4321))
    finally:
        server.server_close()

    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Exception occurred during processing of request" not in err
    assert any("客户端断连" in record.getMessage() for record in caplog.records)


def test_other_errors_still_reach_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """反向断言：非断连异常必须照旧打出来，不能被这条护栏顺手吞掉。"""
    server = _bounded_server()
    try:
        try:
            raise ValueError("real bug")
        except ValueError:
            server.handle_error(None, ("127.0.0.1", 4321))
    finally:
        server.server_close()

    err = capsys.readouterr().err
    assert "Exception occurred during processing of request" in err
    assert "ValueError" in err


def _half_body_disconnect(base: str) -> None:
    """声明 ``Content-Length: 200`` 却只发 5 字节就断开（模拟客户端提前退出）。

    ``shutdown`` + ``close`` 会让内核回 RST：服务端随后写 400 响应时拿到
    ``BrokenPipeError`` —— 注意异常发生在 handler 内部（``_write_json_status``），
    会被 ``_handle_unexpected`` 兜住，**到不了** socketserver 的 ``handle_error``。
    """
    parts = urllib.parse.urlsplit(base)
    sock = socket.create_connection((parts.hostname, parts.port), timeout=5)
    try:
        sock.sendall(
            b"POST /api/dashboard/track/add HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 200\r\n"
            b"\r\n"
            b"12345"
        )
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    finally:
        sock.close()


def _wait_for_log(caplog: pytest.LogCaptureFixture, needle: str, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in caplog.text:
            return True
        time.sleep(0.02)
    return False


def test_real_disconnect_is_logged_as_one_line_not_a_traceback(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R59（审计 B）：真断连时 handler 兜底只记一行 WARNING，不打整段栈。

    改前 ``_handle_unexpected`` 是 ``_LOG.exception(...)``：同一场景下
    ``_BoundedThreadingHTTPServer.handle_error`` 不会被调用（已实测：spy 记录为空），
    噪声全部来自这一行 —— 所以护栏必须钉在 handler 这层。
    """
    with caplog.at_level(logging.DEBUG, logger="cpt.web.handler"):
        with served() as base:
            for _ in range(3):
                _half_body_disconnect(base)
                if _wait_for_log(caplog, "客户端提前断开"):
                    break
        err = capsys.readouterr().err

    assert "客户端提前断开" in caplog.text, f"没等到断连日志：{caplog.text!r} / stderr={err!r}"
    assert any(name in caplog.text for name in ("BrokenPipeError", "ConnectionResetError"))
    assert "未捕获异常" not in caplog.text
    assert "Traceback" not in caplog.text
    assert "Traceback" not in err
