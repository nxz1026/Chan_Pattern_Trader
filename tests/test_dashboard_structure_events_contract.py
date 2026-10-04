"""结构事件 UI 接线的**契约**测试（R27-3）。

## 为什么不直接上浏览器

`test_dashboard_chromium_smoke.py` 用 ``--dump-dom`` 跑 ``file://``，够不到
``fetch`` —— 新面板是**拉到数据才渲染**的，静态 dump 看不见。所以这里用源码契约
钉住「接线对不对」，渲染效果另在真服务器上用浏览器验（见台账）。

## 钉住的核心性质：两个事件来源不许混为一谈

R26 实测确认 ``snapshot.events`` 在稳态下**恒为空**（每轮 diff，绝大多数轮次
结构没变）。这意味着只渲染它的时间线面板在绝大多数时候是死的。因此 R27 加了
``cpt_structure_event`` 的累计事件流。

这两者极易被后人「顺手统一」掉 —— 名字都叫事件，接的是同一个面板。所以显式断言：

- 累计流走**新路由**（``/structure-events``），与 snapshot 无关；
- 原有 ``event-timeline`` 仍渲染 ``snapshot.events``，因为它**接了 replay 的
  时间轴过滤**（``replayPrefix``），换掉会破坏回放功能。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests.conftest import dashboard_js

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS_TEXT = dashboard_js()   # R45 拆分：读全部模块
INDEX_HTML = ROOT / "dashboard" / "index.html"
CSS = ROOT / "dashboard" / "dashboard.css"
APP_PY = ROOT / "cpt" / "web" / "app.py"


@pytest.fixture(scope="module")
def js() -> str:
    return DASHBOARD_JS_TEXT


def test_cumulative_stream_calls_new_route(js: str) -> None:
    assert "/structure-events" in js
    assert "loadStructureEvents" in js
    assert "loadStructureTimeline" in js


def test_cumulative_stream_is_wired_into_boot(js: str) -> None:
    """不接进 boot 的 loader 永远不会被调用 —— 面板会一直是隐藏的。"""
    assert "loadStructureEvents();" in js


def test_existing_snapshot_timeline_keeps_its_source(js: str) -> None:
    """原有时间线**必须**继续渲染 snapshot.events。

    ``replayPrefix`` 会按 K 线时间过滤 ``next.events``，所以它不是「多余的旧实现」，
    而是回放功能的一部分。统一到累计流会直接破坏回放。
    """
    assert "const events = snapshot ? asArray(snapshot.events) : [];" in js
    assert "next.events = asArray(snapshot.events)" in js


def test_cumulative_stream_does_not_read_snapshot(js: str) -> None:
    """累计流不能从 snapshot 取数 —— 那正是它在稳态下为空的原因。"""
    assert "asArray(state.snapshot.events)" not in js
    assert "snapshot.events" not in js.split("function renderStructureEvents()", 1)[1][:4000]


def test_degradation_is_surfaced_not_swallowed(js: str) -> None:
    """``available=false`` 必须渲染成可见文案，不能静默成「暂无事件」。

    后端已刻意改成「读失败就抛」，就是为了让接口能如实说不可用；前端若把两者
    渲染成同一句话，这次改动就白做了。
    """
    assert "structure_event_stream_unavailable" in js
    assert "structure-events-unavailable" in js
    assert "structure-events-empty" in js


def test_detail_is_labelled_as_cumulative(js: str) -> None:
    """标题与说明必须写清这是「累计」事件流。"""
    assert "结构事件流（累计）" in js
    assert "跨重启累积的事件流" in js


def test_panel_and_styles_exist() -> None:
    css = CSS.read_text(encoding="utf-8")
    assert ".cpt-structure-event-id" in css
    assert ".cpt-structure-timeline" in css
    assert ".cpt-structure-events-note" in css


def test_backed_by_real_routes() -> None:
    """前端接的 URL 必须在后端真的存在，否则面板永远降级。"""
    app = APP_PY.read_text(encoding="utf-8")
    assert '"/api/dashboard/structure-events"' in app
    assert '"/api/dashboard/structure-events/timeline"' in app


def test_existing_event_panel_anchor_still_present() -> None:
    """原有面板的锚点不能被这次改动挤掉。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'data-testid="event-timeline"' in html
    assert 'data-testid="event-panel"' in html


def test_empty_state_is_redrawn_not_a_static_placeholder(js: str) -> None:
    """空态必须由 JS 画出，不能靠 index.html 里的静态占位。

    **这是一个真 bug 的钉子**：原实现先 ``removeChild`` 清空整个 ``<ol>``
    （把静态占位 ``<li>`` 一起删掉），再对那个**已脱离文档**的占位调
    ``setHidden`` —— 于是第一次空渲染之后占位就永久消失，面板变成一个没有任何
    解释的空白框。

    R26 实测 ``snapshot.events`` 稳态恒为空，所以这不是边角情况而是**常态**，
    那个空白框是每次打开看板都会看到的默认画面。
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    # 静态占位必须已经不存在（否则又会回到「两处真相」）
    assert 'data-testid="event-timeline-empty"' not in html
    # 空的 <ol>：里面不许有任何真实 <li>，空态由 renderEvents 运行时插入。
    # 先剥掉 HTML 注释 —— 注释里为了说明旧 bug 提到了 "<li>" 字样，
    # 不剥掉的话这段说明会把自己判失败。
    ol = re.search(r'<ol[^>]*data-testid="event-timeline"[^>]*>(.*?)</ol>', html, re.S)
    assert ol is not None, "event-timeline 的 <ol> 不见了"
    inner = re.sub(r"<!--.*?-->", "", ol.group(1), flags=re.S)
    assert "<li" not in inner, f"<ol> 里仍留着静态 <li> 占位: {inner[:120]}"
    # renderEvents 自己画空态
    assert "本轮无结构变化" in js
    # 旧的错误路径不能再出现
    assert "event-timeline-empty" not in js


def test_two_event_panels_are_labelled_differently(js: str) -> None:
    """两个来源的标题必须能让人一眼分清，否则用户不知道该看哪个。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "结构事件（本轮变化）" in html
    assert "结构事件流（累计）" in js
    # 本轮时间线加一句指向累计流
    assert "event-timeline-hint" in html
