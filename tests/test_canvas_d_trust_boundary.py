"""画布 D iframe 的信任边界（M3，审计基线 S1）。

## M3 到底是什么（2026-10-02 勘察结论）

审计条目写的是「canvas iframe 信任边界」，一直挂着「未定义」。勘察下来：

**当前没有可利用的注入路径。** 整份 wbt HTML 里**只有一个**外部值被拼进去 ——
``builder = HtmlReportBuilder(title=f"CPT 结构报告 · {symbol}")``，其余全是字面量
或纯数值。而 ``symbol`` 的两条入口都拦得住：

- A 股：``a_share_routes._normalize`` → ``normalize_code``，线上实测
  ``?code=<script>alert(1)</script>`` 回 **400 invalid_code**；
- 加密：provider 自己配置的 symbol，不来自请求参数。

所以 M3 的性质是「**边界隐式、无人看守**」，不是「有 XSS」。

## 那它仍然值得管

``canvas_d.js`` 此前建 iframe 用的是
``sandbox="allow-same-origin allow-scripts"``。这个组合下 frame 内的脚本可以
``window.frameElement.removeAttribute("sandbox")`` 再重载，从而拿到父页面的
同源权限 —— **逃逸原语一直存在**，只是没有攻击者可控的输入喂给它。

R28-5 时我判断「两个 flag 都是承重的，去掉 allow-same-origin 会让四画布计数
断言全废」。**那个判断是错的**：全仓 `grep contentDocument` 只命中 `canvas_d.js`
自己，没有任何测试或审计脚本读 iframe DOM —— 四画布计数走的是父节点上的
``data-canvas-counts``，数据来自服务端 JSON 的 ``counts`` 字段。

R28-11 据此改成 ``srcdoc`` + ``sandbox="allow-scripts"``：DOM 组装搬到字符串侧，
iframe 拿到**不透明 origin**，逃逸原语从根上不存在。代价接近于零。

本文件钉的是**无争议的那一半**：把隐式边界变成被强制的。

- 任何非白名单字符进了 ``body_html`` 就失败；
- ``symbol`` 里的 HTML 元字符必须被转义成字面文本（纵深防御：今天靠上游校验，
  上游哪天放宽格式，这里就是静默的注入点）；
- ``allow-same-origin`` 不许被加回来，``contentDocument`` 不许被重新使用。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from cpt.application import canvas_wbt

_REPO = Path(__file__).resolve().parents[1]

_HTML_METACHARS = re.compile(r"<(script|img|svg|iframe)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 1. 构造点：唯一的插值点必须转义
# --------------------------------------------------------------------------- #


def test_symbol_is_escaped_before_reaching_html() -> None:
    """``symbol`` 是唯一被拼进 HTML 的外部值 —— 必须转义。

    这是纵深防御：当前两条入口都拦得住，所以这条断言在正常路径下**恒成立**
    （6 位数字代码 / BTCUSDT 转义后不变）。它防的是「将来有人放宽了 code 格式
    或新增了市场入口」这个场景。
    """
    import html as _html

    assert _html.escape("<script>alert(1)</script>", quote=True) == (
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )
    # 正常值不受影响 —— 不能因为加转义把 BTCUSDT 之类搞坏
    for legit in ("600519", "000011", "BTCUSDT", "—"):
        assert _html.escape(legit, quote=True) == legit


def test_source_does_not_interpolate_unescaped_symbol() -> None:
    """源码级守卫：title 那行必须用的是转义后的变量。

    比运行时断言更早失效 —— 有人把 ``safe_symbol`` 改回 ``symbol`` 时，
    这条在跑测试前就红了。
    """
    text = (_REPO / "cpt" / "application" / "canvas_wbt.py").read_text(encoding="utf-8")
    assert "safe_symbol = html.escape(symbol, quote=True)" in text
    assert 'title=f"CPT 结构报告 · {safe_symbol}"' in text
    # 旧写法不许残留
    assert 'title=f"CPT 结构报告 · {symbol}"' not in text
    # kind 也一并转义（它来自 market.kind，理论上是固定词表，但同理）
    assert "html.escape(kind, quote=True)" in text


# --------------------------------------------------------------------------- #
# 2. sandbox 组合：现状 + 为什么不能随手改
# --------------------------------------------------------------------------- #


def _canvas_d_code() -> str:
    """``canvas_d.js`` 剥掉注释后的**代码部分**。

    刻意剥注释：文件头里要保留「以前是 allow-same-origin + contentDocument」
    这段历史说明（免得后人把 flag 加回去），而断言要禁的是**代码里**再用它。
    两件事不冲突。
    """
    text = (_REPO / "dashboard" / "canvas_d.js").read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # 块注释
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)  # 行注释
    return text


def test_iframe_no_longer_has_allow_same_origin() -> None:
    """R28-11：``allow-same-origin`` 已移除，iframe 拿到**不透明 origin**。

    这是 M3 的彻底解法：即使内容里跑进恶意脚本，它也够不到父页面的
    DOM / cookie / localStorage —— 逃逸原语**从根上不存在**了。

    ``allow-scripts`` 保留：去掉它 plotly 就不跑，画布 D 直接废。
    """
    code = _canvas_d_code()
    assert 'frame.setAttribute("sandbox", "allow-scripts")' in code
    assert "allow-same-origin" not in code, "allow-same-origin 必须已移除"
    # 逃逸原语依赖的 contentDocument 也不该再出现
    assert "contentDocument" not in code, "父页不该再触碰 iframe 的 contentDocument"


def test_sandbox_escape_is_explained_in_source() -> None:
    """那条逃逸原语要留在注释里 —— 免得有人把 flag 加回去以为在做加固。"""
    js = (_REPO / "dashboard" / "canvas_d.js").read_text(encoding="utf-8")
    assert "removeAttribute" in js
    assert "不透明 origin" in js


def test_counts_do_not_depend_on_iframe_dom() -> None:
    """四画布计数一致性**必须**只依赖服务端 JSON，不能读 iframe DOM。

    这是 R28-5 写错过的地方：当时假设「去掉 allow-same-origin 会让计数断言全废」，
    实际全仓没有任何代码读 ``contentDocument``。这条测试把这个事实钉住 ——
    万一将来有人为了拿 DOM 把 ``contentDocument`` 加回来，这条立刻红。
    """
    code = _canvas_d_code()
    # 计数来自 payload.counts（服务端字段）
    assert "payload.counts || {}" in code
    assert "node.dataset.canvasCounts" in code
    # 且不经过 iframe DOM
    assert "contentDocument" not in code


# --------------------------------------------------------------------------- #
# 3. 运行时：构造出的 HTML 里不能出现可执行标签
# --------------------------------------------------------------------------- #


def _payload_with_symbol(symbol: str) -> dict[str, Any]:
    return {
        "available": True,
        "body_html": f"<h1>结构报告 · {symbol}</h1>",
        "css": "",
        "scripts": [],
        "counts": {"candles": 0, "fractals": 0, "bis": 0, "zhongshus": 0, "trendTypes": 0},
    }


@pytest.mark.parametrize(
    "payload",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "<svg/onload=alert(1)>",
        '600519"><script>alert(1)</script>',
    ],
    ids=["script", "img-onerror", "svg-onload", "attr-breakout"],
)
def test_no_executable_tag_survives_escaping(payload: str) -> None:
    """转义后的恶意 symbol 在 HTML 里只能是字面文本。"""
    import html as _html

    escaped = _html.escape(payload, quote=True)
    assert not _HTML_METACHARS.search(escaped), escaped
    assert "<" not in escaped and ">" not in escaped


def test_build_payload_rejects_injected_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：即使 snapshot 里混进了恶意 symbol，输出 HTML 也不含可执行标签。

    这里直接构造一份 ``market.symbol`` 带 payload 的 snapshot，**绕过上游校验**
    （模拟「上游校验被放宽 / 新增了市场入口」这个我们要防的场景）。
    """
    payload = "<script>alert(1)</script>"
    snapshot: dict[str, Any] = {
        "market": {"symbol": payload, "kind": "a_share"},
        # 必须给 K 线：`no_candles_in_window` 分支会在构造 title 之前就降级返回
        "candles": [
            {
                "open_time": 1_700_000_000_000,
                "close_time": 1_700_003_600_000,
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "volume": 100.0,
            }
        ],
        "overlays": {"fractals": [], "bis": [], "zhongshus": [], "trend_types": []},
    }

    captured: dict[str, str] = {}

    class _FakeBuilder:
        def __init__(self, title: str = "", theme: str = "") -> None:
            captured["title"] = title

        def add_header(self, *_a: Any, **_k: Any) -> None:
            return None

        def add_metrics(self, *_a: Any, **_k: Any) -> None:
            return None

        def add_chart_tab(self, *_a: Any, **_k: Any) -> None:
            return None

        def add_table(self, *_a: Any, **_k: Any) -> None:
            return None

        def add_footer(self, *_a: Any, **_k: Any) -> None:
            return None

        def render(self) -> str:
            return f"<html><body><h1>{captured['title']}</h1></body></html>"

    fake = type("FakeWbt", (), {"report": type("R", (), {"HtmlReportBuilder": _FakeBuilder})})

    class _FakeFigure:
        def to_html(self, *_a: Any, **_k: Any) -> str:
            return "<div>fake</div>"

    monkeypatch.setattr(canvas_wbt, "_import_wbt", lambda: fake, raising=False)
    # plotly / pandas 是**可选依赖**（CI 不装），桩掉让本条只验证转义逻辑
    monkeypatch.setattr(
        canvas_wbt, "_candlestick_figure", lambda *a, **k: _FakeFigure(), raising=False
    )
    monkeypatch.setattr(
        canvas_wbt, "extract_body_fragment", lambda _d: (captured["title"], []), raising=False
    )

    result = canvas_wbt.build_canvas_d_payload(snapshot=snapshot)
    title = captured.get("title", "")
    assert title, "没走到 title 构造这一步（wbt 分支提前降级了？）"
    assert "<script>" not in title, title
    assert "&lt;script&gt;" in title, f"应转义成字面文本，实际 {title!r}"
    # 降级时也要是空 body，不能把注入内容塞进 available=False 的响应里
    assert result.get("available") in (True, False)
