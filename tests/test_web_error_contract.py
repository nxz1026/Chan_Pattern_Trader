"""API 错误响应必须是 JSON（R29 web 层复盘）。

## 这条约束防的是什么

2026-10-02 真机实测发现：JSON API 有一半错误响应**是 HTML 错误页**。最刺眼的是
**同一个路由里两种形状并存**：

    /api/dashboard/a-share/snapshot?width_k=abc  ->  400  Content-Type: text/html
    /api/dashboard/a-share/snapshot?code=ZZZZZZ  ->  400  Content-Type: application/json

客户端 `await response.json()` 遇到 HTML 会直接抛 `SyntaxError` —— 而前端
恰恰是靠 `error.code` 做分支的（`invalid_code` / `not_json_safe` / …）。
一半错误走 JSON、一半走 HTML，等于让前端的错误处理随机失效。

> **R51 留档**：当年钉住这条契约的样本是 `/api/canvas/wbt?start_ms=abc&end_ms=def`
> 与 `?code=ZZZZZZ` 这对「同路由两种形状」。画布 D 已下线，该端点除名（回 404），
> 样本换成仍在线的 `/api/dashboard/a-share/snapshot` 的 `width_k` 与 `code` 两条
> 错误路径，契约本身不变；除名后的 404 也一并留在 `_BAD_REQUESTS` 里盯着。

## 为什么不用 `send_error` 就算完

``BaseHTTPRequestHandler.send_error`` 生成的是标准 HTTP 错误页。对**网页**路由
合理，对 **JSON API** 不合理。而本仓的 `_write_json_error` docstring 已经记了
`send_error` 的另一个坑：非 ASCII 消息进状态行会触发 `UnicodeEncodeError`
**直接断连接**（实测踩过）。所以统一走 JSON 是双赢。

## 门禁形态：运行时契约，不只是源码 grep

源码 grep 只能证明「没调用」，证明不了「真的返回了 JSON」。所以这里起真 server、
打真请求、查真 Content-Type —— 与本仓的 SQL 分层门禁同一思路：**测行为，不测写法**。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import served

APP_PY = Path(__file__).resolve().parents[1] / "cpt" / "web" / "app.py"

#: 覆盖到每一条「参数不是整数 / 缺参数 / 不支持的方法」的错误路径。
#: 曾经吐 HTML 的那几条刻意排在前面 —— 它们就是这次要钉住的目标。
_BAD_REQUESTS = [
    ("/api/dashboard/snapshot?interval_ms=abc", 400),
    ("/api/dashboard/snapshot?start_ms=abc&end_ms=def", 400),
    ("/api/dashboard/snapshot?start_ms=5&end_ms=1", 400),
    ("/api/dashboard/inspect?bar_index=abc", 400),
    ("/api/dashboard/a-share/snapshot?width_k=abc", 400),
    ("/api/dashboard/a-share/snapshot?code=ZZZZZZ", 400),
    ("/api/dashboard/export?start_ms=abc", 400),
    ("/api/dashboard/compare?left=1", 400),
    ("/api/dashboard/structure-events/timeline", 400),
    ("/api/dashboard/signal-stats?days=abc", 400),
    ("/api/dashboard/nope", 404),
    # R51：画布 D 下线 ⇒ 端点除名。删路由后若错落到非 JSON 分支（send_error /
    # 静态目录兜底），这里立刻红 —— 正是本文件要守的那条契约。
    ("/api/canvas/wbt", 404),
]

#: **刻意不在上面那张表里**的路径：`?level=abc`。它的行为**随模式而变** ——
#: level 分支被 ``isinstance(provider, MultiLevelSource)`` 门控：
#:
#: - realtime（线上）：是 MultiLevelSource → ``int("abc")`` 抛 ValueError → **400**；
#: - demo / fixture（本测试用的 provider）：不是 → **整段跳过**，静默返回 200。
#:
#: 同一个坏参数，两种模式两种结果。与 range 的处理也不一致 ——
#: range 不可用时回 ``available:false`` + reason（明说），level 却静默忽略。
#: 见 ``test_level_param_is_silently_ignored_in_non_multilevel_mode``。
#: 本轮**不改**这个行为（改动它要先决定「demo 模式收到不支持的参数该怎么办」，
#: 那是产品口径），只把它写成显式测试，免得下一个���以为它是有意的。
_LEVEL_IN_REALTIME_ONLY = "/api/dashboard/snapshot?level=abc"


def _request(base: str, path: str) -> tuple[int, str, str]:
    """返回 ``(status, content_type, body)``。HTTPError 也要拿响应头。"""
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=15) as fh:
            return fh.status, fh.headers.get("Content-Type", ""), fh.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")


@pytest.fixture(scope="module")
def base() -> str:
    with served() as url:
        yield url


@pytest.mark.parametrize(
    "path,expected", _BAD_REQUESTS, ids=[p.split("?")[0] for p, _ in _BAD_REQUESTS]
)
def test_error_responses_are_json(base: str, path: str, expected: int) -> None:
    """每个出错路径都必须回 JSON，且带 ``error.code`` 供前端分支。"""
    status, ctype, body = _request(base, path)
    assert status == expected, f"{path} 期望 {expected}，实际 {status}"
    assert "application/json" in ctype, (
        f"{path} 返回了非 JSON：{ctype}（body 前 80 字 {body[:80]!r}）"
    )
    payload = json.loads(body)  # 解析失败本身就是这个测试要抓的
    assert "error" in payload, f"{path} 的 JSON 里没有 error 键：{list(payload)[:6]}"
    assert payload["error"].get("code"), f"{path} 的 error 缺 code，前端无法分支"


def test_error_code_is_machine_readable(base: str) -> None:
    """``error.code`` 必须是稳定的机器可读串，不是中文散文。"""
    _, ctype, body = _request(base, "/api/dashboard/a-share/snapshot?code=ZZZZZZ")
    assert "application/json" in ctype
    code = json.loads(body)["error"]["code"]
    assert re.fullmatch(r"[a-z0-9_]+", code), f"code 不是 slug 形态：{code!r}"


def test_level_param_is_silently_ignored_in_non_multilevel_mode(base: str) -> None:
    """把「坏参数随模式静默消失」这件事**显式钉住**，而不是当成不存在。

    本测试用的 provider 不是 ``MultiLevelSource``，于是 ``?level=abc`` 整段被
    ``isinstance`` 门控跳过，返回 200。线上 realtime 模式会回 400。

    **这不是本轮要改的**，但它是个真实的口径不一致：range 在模式不支持时回
    ``available:false`` + reason（明说），level 却一声不响。把不一致写下来，
    比留一个「看起来像有意为之」的坑要好。
    """
    status, ctype, body = _request(base, _LEVEL_IN_REALTIME_ONLY)
    assert status == 200, "本测试的 provider 不是 MultiLevelSource，level 分支应被跳过"
    assert "application/json" in ctype
    assert json.loads(body)  # 仍然是合法 JSON —— 跳过的只是校验，不是响应格式


def test_no_send_error_left_in_web_app() -> None:
    """源码级守卫：``send_error`` 不该再出现在 JSON API 的 handler 里。

    运行时契约测试覆盖的是「我列出的这些路径」，这条覆盖的是**将来新增**的路径 ——
    有人再写一次 ``send_error`` 时立刻红。
    """
    src = APP_PY.read_text(encoding="utf-8")
    hits = [
        (i, line.strip())
        for i, line in enumerate(src.splitlines(), 1)
        if "self.send_error(" in line
    ]
    assert not hits, "web/app.py 仍在用 send_error（会回 HTML 错误页）：\n" + "\n".join(
        f"  行{i}: {line}" for i, line in hits
    )
