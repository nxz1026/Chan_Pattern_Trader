"""R85（B）：第一屏「今天该不该看盘」横幅的接线与口径。

**这个横幅是用户打开页面第一眼看到的**，所以它撒谎的代价最高：
- 没有数据必须**隐藏**，不能显示「0 只站在位置上」（会被读成「今天没机会」）
- 有票必须给出**能去做决定**的数字，而不是一句「结构就绪」
- 永远不许出现买卖指令（R76 红线）
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HTML = REPO / "dashboard" / "index.html"
DASH = REPO / "dashboard"


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def _js(name: str) -> str:
    return (DASH / name).read_text(encoding="utf-8")


# ───────────────────────── 存在性 ─────────────────────────


def test_banner_exists_before_the_dashboard_body() -> None:
    """必须在 ``dashboard-body`` **之前** —— 第一屏的意思就是「先于一切」。"""
    html = _html()
    banner_at = html.find('data-testid="market-step-banner"')
    body_at = html.find('data-testid="dashboard-body"')
    assert banner_at > 0, "横幅不存在"
    assert body_at > 0
    assert banner_at < body_at, "横幅必须在 dashboard-body 之前，否则它不在第一屏"


def test_banner_has_a_title_users_understand() -> None:
    """标题不许是「信号全景」「结构就绪」这类词 —— 那是板块名，不是问题。"""
    html = _html()
    m = re.search(r'id="market-step-title"[^>]*>([^<]+)<', html)
    assert m, "找不到横幅标题"
    title = m.group(1)
    assert "该不该" in title or "要不要" in title, f"标题读起来不像一个问题：{title!r}"
    for jargon in ("结构", "信号", "缠论", "中枢", "背驰"):
        assert jargon not in title, f"标题里不该出现术语「{jargon}」：{title!r}"


def test_banner_starts_hidden() -> None:
    """默认 hidden：没拉到数据前不许显示任何数字。"""
    html = _html()
    m = re.search(r'<section\s+(?=[^>]*data-testid="market-step-banner")[^>]*>', html)
    assert m, "找不到横幅 section 标签"
    assert re.search(r"\bhidden\b", m.group(0)), "横幅默认必须是 hidden"


def test_banner_has_the_three_testids_the_renderer_writes() -> None:
    html = _html()
    for testid in ("market-step-headline", "market-step-numbers", "market-step-action"):
        assert f'data-testid="{testid}"' in html, f"缺少 {testid}"


# ───────────────────────── 渲染逻辑 ─────────────────────────


def test_renderer_hides_on_missing_distribution() -> None:
    """没有 ``step_distribution`` ⇒ 隐藏。**不许显示 0**。

    「0 只站在位置上」与「数据没拿到」是两件事，混在一起会让人以为今天没机会。
    """
    js = _js("dash-signal.js")
    start = js.find("function buildMarketStepBanner")
    assert start > 0, "找不到 buildMarketStepBanner"
    body = js[start : js.find("\n  }\n", start) + 4]
    assert "wrap.hidden = true" in body
    assert "return" in body


def test_renderer_never_says_buy_or_sell() -> None:
    """R76 红线：横幅只能说位置，不能说动作。"""
    js = _js("dash-signal.js")
    start = js.find("function buildMarketStepBanner")
    body = js[start : js.find("\n  }\n", start) + 4]
    for word in ("买入", "卖出", "加仓", "抄底", "建仓", "清仓"):
        assert word not in body, f"横幅里出现祈使动作「{word}」"


def test_renderer_carries_the_no_alpha_evidence() -> None:
    """有票站在位置上时，必须**同一句话里**给 R70 的实测数字。

    不给数字的「位置提示」等于换个说法说「可能有机会」。
    """
    js = _js("dash-signal.js")
    start = js.find("function buildMarketStepBanner")
    body = js[start : js.find("\n  }\n", start) + 4]
    assert "+0.02%" in body and "46%" in body, "必须有 R70 的 20 日收益与胜率"
    assert "不是买点" in body


def test_renderer_tells_the_user_what_to_do_when_nothing_is_standing() -> None:
    """没有票时也要给一句「现在不用做任何事」—— 空着会让人不知道下一步。"""
    js = _js("dash-signal.js")
    start = js.find("function buildMarketStepBanner")
    body = js[start : js.find("\n  }\n", start) + 4]
    assert "什么都不用做" in body


def test_banner_reads_the_same_payload_as_the_overview_panel() -> None:
    """**不许为第一屏再开一个接口** —— 两份数据迟早不一致。

    两个渲染点都从 ``remoteOps.signalOverview`` 取数。
    """
    ops = _js("dash-ops.js")
    assert "buildMarketStepBanner" in ops, "横幅没被调用"
    start = ops.find("buildMarketStepBanner(")
    call = ops[start : ops.find(");", start) + 2]
    assert "remoteOps.signalOverview" in call, "横幅必须复用 signalOverview 的数据"
    # 全仓源文件里只许有一处 overview 请求（bundle 是产物，不查）
    for path in DASH.glob("dash-*.js"):
        text = path.read_text(encoding="utf-8")
        hits = re.findall(r"/signal-overview", text)
        assert len(hits) <= 1, f"{path.name} 里有 {len(hits)} 处 signal-overview 请求"


def test_banner_is_wired_after_the_overview_is_loaded() -> None:
    """横幅与全景面板在同一次拉取后渲染，顺序不倒。"""
    ops = _js("dash-ops.js")
    assign = ops.find("remoteOps.signalOverview = ")
    overview = ops.find("renderSignalOverview(state.snapshot);")
    banner = ops.find("buildMarketStepBanner(")
    assert 0 < assign < overview < banner
