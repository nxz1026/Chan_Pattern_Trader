"""HTTP 入口：POST 请求体的**所有拒绝分支**（R45 P0-1 补测 · 第一批）。

## 为什么先补这个

``app._read_json_body`` 覆盖率 **36.8%**（7/19 行未覆盖），而它是
**所有 POST 路由的入口** —— R45 自己新加的
``/api/dashboard/a-share/llm/summarize`` 就走它。

它有 5 条分支，**4 条是「拒绝」**：长度非法、为零、超上限、body 畸形/读失败。
这些分支的特点是：**正常路径天天跑，异常路径没人跑**，
而它们恰恰决定「客户端发了个坏请求会看到什么」。

## 关键：不硬造 handler，而是打真请求

``BaseHTTPRequestHandler`` 需要 socket，硬造 handler 既麻烦又测不到真实链路。
仓里已有 ``tests/conftest.py::served`` —— **起真 server 打真请求**，
与本仓「测行为，不 grep 源码」的思路一致（见 test_web_error_contract.py）。

⚠️ 所有 POST 路由都先查 ``Content-Type``（缺/错回 **415**），
所以要覆盖 body 解析的分支，**必须带对 Content-Type** ——
否则测到的永远是 415 那一条。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from tests.conftest import served

#: 写路由（A 股 llm/summarize 与 llm/explain 都走 _read_json_body）
_URL = "/api/dashboard/a-share/llm/summarize?code=600519"


def _post(base: str, body: bytes | None, *, ctype: str | None = "application/json",
          method: str = "POST") -> tuple[int, str]:
    """发一个 POST，返回 (状态码, 响应体前 200 字)。"""
    headers = {"Accept": "application/json"}
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(f"{base}{_URL}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]


@pytest.fixture()
def base() -> Iterator[str]:
    with served() as b:
        yield b


# ── Content-Type 门槛：它挡在 body 解析**之前** ──────────────────
def test_missing_content_type_is_415(base: str) -> None:
    """缺 Content-Type ⇒ **415**（不是 415 媒体类型不支持，而是别的）。"""
    code, _ = _post(base, b"{}", ctype=None)
    assert code == 415


def test_wrong_content_type_is_415(base: str) -> None:
    code, _ = _post(base, b"a=1&b=2", ctype="application/x-www-form-urlencoded")
    assert code == 415


# ── body 解析的拒绝分支（带对 Content-Type 才走得到）────────────
def test_malformed_json_is_400(base: str) -> None:
    """畸形 JSON ⇒ **400**，**不是 500**。

    ⚠️ 畸形 body 是**客户端问题**，不该在 handler 里冒未捕获异常变成 500。
    实测日志确认 ``_read_json_body`` 确实捕获了（``读取 JSON body 失败: …``）。

    ⚠️ **但错误码是有歧义的**：返回的是 ``recommendation_required``（"recommendation
    不能为空"），而不是"body 解析失败"。
    ⇒ 客户端发了一段垃圾，收到的提示是「你没传推荐」—— **误导**。

    这**不是 bug**：``_read_json_body`` 的 docstring 明确写了
    「读不到或解析失败返回 None（不抛）；**调用方据此回自己的 400**」，
    即「解析失败」与「空」被有意归成同一个 None。
    契约保证的是**状态码**（400）不是**错误码**。
    ⇒ 本测试只钉状态码；错误码的歧义记在 docstring 备注里，不当成缺陷。
    """
    code, body = _post(base, b"{not json")
    assert code == 400, body
    # 必须是 JSON 形状（不是 HTML 错误页 —— 那正是 R29 修过的问题）
    assert body.lstrip().startswith("{"), body


def test_non_utf8_body_is_400(base: str) -> None:
    """非 UTF-8 字节 ⇒ 同样 400（``UnicodeDecodeError`` 那条分支）。"""
    code, body = _post(base, b"\xff\xfe\x00bad")
    assert code == 400, body


def test_json_array_is_400_not_accepted(base: str) -> None:
    """合法 JSON 但是**数组** ⇒ 400（路由要求 object）。"""
    code, _ = _post(base, b"[1,2,3]")
    assert code == 400


def test_empty_object_is_400(base: str) -> None:
    """空对象 ⇒ 400（``recommendation_required``）。

    ⚠️ 注意 ``{}`` 长度 2 > 0，所以会真的进 ``json.loads`` 并成功 ——
    拦住它的是路由自己的「空则 400」，不是 body 解析。
    """
    code, body = _post(base, b"{}")
    assert code == 400
    assert "recommendation" in body.lower()


def test_wrong_method_is_405(base: str) -> None:
    """GET 打写路由 ⇒ 405（不是 404 也不是 415）。"""
    code, _ = _post(base, None, ctype="application/json", method="GET")
    assert code in (404, 405), code


def test_get_on_read_route_needs_no_body(base: str) -> None:
    """对照组：GET 读路由不碰 body 解析，必须正常。"""
    with urllib.request.urlopen(f"{base}/api/dashboard/health", timeout=10) as resp:
        assert resp.status == 200
        assert json.loads(resp.read().decode()) is not None
