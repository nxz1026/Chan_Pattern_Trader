"""LLM 面板接线的**契约**测试（R28-3）。

与 R27-3 同款思路：面板是**拉到数据才渲染**的，静态 dump 看不见，所以用源码
契约钉住接线与关键行为（尤其是「轮询跟随」这个最容易写漏的点）。

## 钉住的核心性质

1. **接口早就存在**，本轮只是接 UI —— 所以必须钉住 URL 没写错（写错了面板
   永远降级成「暂无调用记录」，看起来像「没调用过」）；
2. **退避重入会等几十秒**（cap 默认 60s）。如果只拉一次就停，用户会看到
   `queued` 永远不动，误以为功能坏了。所以「还有在途任务就继续轮询」是本组
   测试的重点；
3. **解释按钮的可用条件**是「A 股 + 选中了结构」。无条件可点的话，会把
   crypto 侧的选中项 POST 到 A 股专用的 explain 端点上去。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import dashboard_js

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS_TEXT = dashboard_js()  # R45 拆分：读全部模块
CSS = ROOT / "dashboard" / "dashboard.css"
APP_PY = ROOT / "cpt" / "web" / "app.py"


@pytest.fixture(scope="module")
def js() -> str:
    return DASHBOARD_JS_TEXT


def test_calls_route_is_the_real_one(js: str) -> None:
    app = APP_PY.read_text(encoding="utf-8")
    assert '"/api/dashboard/llm/calls"' in app
    assert "/llm/calls" in js
    # 不能写死成别的前缀：看板可挂在任意 nginx 前缀下
    assert "DASHBOARD_BASE()" in js


def test_explain_endpoint_matches_backend(js: str) -> None:
    app = APP_PY.read_text(encoding="utf-8")
    assert '"/api/dashboard/a-share/llm/explain"' in app
    assert "a-share/llm/explain" in js


def test_explain_sends_json_content_type(js: str) -> None:
    """写接口要求 ``Content-Type: application/json``（审计 M1，否则 415）。"""
    assert '"Content-Type": "application/json"' in js
    assert 'method: "POST"' in js


def test_panel_polls_while_calls_are_in_flight(js: str) -> None:
    """**本组最关键的一条**。

    退避重入的 cap 默认 60s —— 只拉一次就停的话，用户会盯着一个永远不变的
    ``queued``，直接判定「功能坏了」。所以：还有非终态任务就 2s 后再拉。
    """
    assert "LLM_TERMINAL" in js
    assert "remoteOps.llmTimer" in js
    # 轮询条件的写法：任一调用不在终态集合里
    assert re.search(r"!LLM_TERMINAL\.has\(", js), "应有「非终态就继续轮询」的判断"
    assert "loadLlmCalls()" in js
    assert "2000" in js, "轮询间隔应有值"


def test_all_non_terminal_statuses_are_not_terminal(js: str) -> None:
    """终态集合必须含齐 ok / error / interrupted。

    漏一个的后果：该状态的任务会被永远当成「在途」→ 面板无限轮询打接口。
    """
    block = re.search(r"const LLM_TERMINAL = new Set\(\[([^\]]*)\]\)", js)
    assert block is not None, "找不到 LLM_TERMINAL 定义"
    for status in ("ok", "error", "interrupted"):
        assert f'"{status}"' in block.group(1), f"终态集合漏了 {status}"


def test_explain_button_requires_a_share_and_structure_selection(js: str) -> None:
    """解释端点是 A 股专用的，按钮不能无条件可点。"""
    assert "/^\\d{6}$/.test" in js, "必须校验 A 股代码格式（6 位数字）"
    for kind in ("bi", "zhongshu", "trend_type"):
        assert f'"{kind}"' in js
    assert "explain.disabled = !usable" in js


def test_unavailable_reason_is_surfaced(js: str) -> None:
    """「为什么 LLM 不可用」必须在面板上看得见。

    这是 ``list_calls`` 特意回 ``unavailable_reason`` + ``config`` 的原因 ——
    排查时最费时间的就是分不清「没 enable / 没 key / base_url 写错」。
    空列表 + 无说明 = 让用户以为「没调用过」。
    """
    assert "unavailable_reason" in js
    assert "llm_missing_api_key" in js
    assert "llm_disabled" in js
    assert "llm-config-line" in js


def test_loading_once_then_giving_up_is_not_allowed(js: str) -> None:
    """防回退：首次拉取失败不该把面板永久隐藏。

    ``section.hidden = true`` 只在「从未拿到过响应」时成立（和 R27 结构事件
    面板同一纪律）。拿到响应后即使 ``available=false`` 也要显示降级文案。
    """
    assert "llm-calls-empty" in js
    assert "暂无调用记录" in js


def test_panel_is_wired_into_boot(js: str) -> None:
    """不接进 boot 的 loader 永远不会被调用。"""
    assert "loadLlmCalls();" in js


def test_styles_exist() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert ".cpt-llm-calls" in css
    assert ".cpt-llm-text" in css
    assert ".cpt-llm-toolbar" in css
    # 状态配色：ok / error / rate_limited 三种状态色都要有对应规则
    for status in ("ok", "error", "interrupted", "rate_limited"):
        assert f'data-status="{status}"' in css, f"缺 {status} 的配色"
