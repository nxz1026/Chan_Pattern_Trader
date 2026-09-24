"""A 股主看板前端契约测试（R17-3）。

钉住的是**接入方式**而不是渲染细节：A 股之所以能零改动复用四个画布，是因为后端
快照与加密侧同构、前端只换 snapshot URL。任何"顺手在画布里加 A 股分支"的改动都会
破坏这个性质，所以这里显式断言画布模块里**没有** A 股逻辑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"

CANVAS_MODULES = ("canvas_b.js", "canvas_c.js", "canvas_d.js")
A_SHARE_JS = DASHBOARD / "market_a_share.js"
DASHBOARD_JS = DASHBOARD / "dashboard.js"
INDEX_HTML = DASHBOARD / "index.html"
CSS = DASHBOARD / "dashboard.css"


@pytest.fixture(scope="module")
def index_source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def a_share_source() -> str:
    return A_SHARE_JS.read_text(encoding="utf-8")


def test_market_switch_exists_in_topbar(index_source: str) -> None:
    assert 'data-testid="market-switch"' in index_source
    assert 'data-market-action="crypto"' in index_source
    assert 'data-market-action="a_share"' in index_source
    assert 'data-testid="a-share-picker"' in index_source
    # 默认必须是加密：A 股入口是附加能力，不能改默认行为
    assert index_source.index('data-market-action="crypto"') < index_source.index(
        'data-market-action="a_share"'
    )


def test_a_share_picker_starts_hidden(index_source: str) -> None:
    """选择器默认隐藏：否则加密模式下顶栏会多出一排无意义的 A 股控件。"""
    picker = index_source.index('data-testid="a-share-picker"')
    assert "hidden" in index_source[picker : picker + 80]


def test_market_module_loads_before_dashboard(index_source: str) -> None:
    """必须用**脚本标签**定位：正文注释里也有 ``./dashboard.js`` 字样（第 131/250/384 行），
    用裸 index() 比顺序会拿到注释的位置，测试自己就错了。"""
    tags = [line for line in index_source.splitlines() if "<script" in line and "src=" in line]
    names = [line.split('src="', 1)[1].split('"', 1)[0] for line in tags]
    assert "./market_a_share.js" in names
    assert names.index("./market_a_share.js") < names.index("./dashboard.js")


def test_no_absolute_api_path_in_market_module(a_share_source: str) -> None:
    """A 股路由必须从 ``data-snapshot-url`` 推导，不能写死 ``/cpt/api/...``。

    看板可以挂在任意 nginx 前缀下（本地 ``/cpt/``、独立 A 股服务是根路径），
    写死会在换前缀时静默 404。
    """
    assert '"/cpt/' not in a_share_source
    assert "aShareBase" in a_share_source
    assert "indexOf(marker)" in a_share_source


def test_market_module_uses_shared_snapshot_loader(a_share_source: str) -> None:
    """必须复用 ``loadSnapshot``，而不是自己 fetch + 自己渲染。

    自己渲染就会绕过 buildCanvasView() 的窗口过滤，四个画布立刻不一致。
    """
    assert "dashboard.loadSnapshot" in a_share_source
    assert "buildCanvasView" not in a_share_source
    assert "drawChart" not in a_share_source


def test_render_canvas_modules_are_market_agnostic() -> None:
    """B / C 必须完全市场无关 —— 这是"零改动复用"的守卫。

    它们只画客户端快照，多一个市场分支就意味着接入第二个市场要改渲染代码。
    """
    for name in ("canvas_b.js", "canvas_c.js"):
        source = (DASHBOARD / name).read_text(encoding="utf-8")
        assert "a_share" not in source.lower(), f"{name} 里出现了 A 股特化逻辑"
        assert "market" not in source.lower().replace("marketdata", ""), f"{name} 里出现了市场分支"


def test_canvas_d_only_forwards_market_context() -> None:
    """D 是**服务端**取数的，必须透传 code —— 但仅此而已。

    实测过的坑：不透传 code 时服务端拿的是 provider 的加密快照，于是 A/B/C 画
    123 根 A 股 K 线、D 画 579 根 BTCUSDT K 线，一屏两个市场。
    允许的"市场相关"只有这一处透传；一旦 D 开始按市场分支渲染，就该重新设计。
    """
    source = (DASHBOARD / "canvas_d.js").read_text(encoding="utf-8")
    lowered = source.lower()
    assert "a_share" in lowered, "canvas_d.js 丢了市场透传，会画成加密"
    # 只允许在 marketQuery() 这一处出现市场判断
    assert lowered.count("a_share") == 1, "canvas_d.js 里出现了多余的市场分支"
    assert "function marketQuery()" in source
    assert "&code=" in source
    assert 'data.market !== "a_share"' in source


def test_a_share_mode_disables_polling() -> None:
    source = DASHBOARD_JS.read_text(encoding="utf-8")
    assert 'root.dataset.market === "a_share"' in source
    start = source.index("function startPolling(")
    body = source[start : start + 400]
    assert 'root.dataset.market === "a_share"' in body
    assert "return null" in body


def test_a_share_mode_locks_interval_to_daily(a_share_source: str) -> None:
    # option 的 value 是**毫秒**：写 "1d" 匹配不到任何 option，select 会显示成空白
    # （R17-3 审计实测 interval.value === ""）。86400000 才是 1d。
    assert 'interval.value = "86400000"' in a_share_source
    # 只在 A 股模式下禁用（加密模式必须仍然可切周期）
    assert "interval.disabled = state.market === MARKET_A_SHARE" in a_share_source


def test_degraded_reasons_cover_ashare_failures() -> None:
    """``no_factor`` / ``no_data`` 必须有自己的中文说明。

    否则会被显示成"无法连接行情上游（Binance）"—— 把排查方向带偏（实测踩到）。
    """
    source = DASHBOARD_JS.read_text(encoding="utf-8")
    assert "no_factor:" in source
    assert "no_data:" in source
    assert "invalid_code:" in source
    assert "缺复权因子" in source
    # 按需补因子的三种结果也必须各有文案：用户要能据此判断该不该重试
    assert "no_factor_unsupported:" in source
    assert "no_factor_cooldown:" in source
    assert "no_factor_fetch_failed:" in source


def test_market_url_params_are_linkable(a_share_source: str) -> None:
    """``?market=a_share&code=xxx`` 必须可分享、可审计（与 ``?canvas=`` 同套路）。"""
    assert 'params.get("market")' in a_share_source
    assert 'params.get("code")' in a_share_source
    assert 'url.searchParams.set("market"' in a_share_source
    assert 'url.searchParams.set("code"' in a_share_source


def test_picker_lists_undrawable_codes_without_disabling_them(a_share_source: str) -> None:
    """热门池里本地无因子的票：**列出、标注，但不禁用**。

    不能直接过滤（会让人以为池子少了票），也**不能禁用** —— R17-3 起点击会触发
    按需拉取，腾讯有该标的后复权就能救回来；禁用等于把这条路堵死。
    """
    assert "本地无因子" in a_share_source
    assert "option.disabled" not in a_share_source


def test_css_defines_market_widgets() -> None:
    source = CSS.read_text(encoding="utf-8")
    for selector in (
        ".market-switch",
        '.market-switch button[aria-pressed="true"]',
        ".a-share-picker",
    ):
        assert selector in source, f"dashboard.css 缺少 {selector}"
