"""R61 前端契约：A 股「市场概览」的面板元素与渲染分支必须同时存在。

这类断言专门防「后端发了字段、前端没接」和「前端读了 testid、HTML 里没有」
这两种**双向**的静默失效 —— 面板上什么都不报错，只是那一行永远是「—」。

``market-last-price-raw`` 就是典型：``dash-chrome.js`` 里
``setText("[data-testid=market-last-price-raw]", ...)`` 在元素缺失时返回
``null`` 且**不创建**节点，于是整段不复权价逻辑全部空转。
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import dashboard_js

ROOT = Path(__file__).parents[1]


def _html() -> str:
    return (ROOT / "dashboard/index.html").read_text(encoding="utf-8")


def test_market_overview_elements_exist_for_every_field_the_js_writes() -> None:
    html = _html()
    for testid in (
        "market-last-price-raw",
        "market-change-amount",
        "market-high-label",
        "market-low-label",
        "market-volume-label",
        "market-data-asof-label",
        "market-data-asof",
        "market-price-basis-label",
        "market-price-basis",
        "market-board",
        "data-quality-closed-label",
        "data-quality-unclosed-label",
        "data-quality-stale-reason-label",
        "data-quality-stale-reason",
        "dual-realtime-label",
    ):
        assert f'data-testid="{testid}"' in html, f"index.html 缺少 {testid}"


def test_js_reads_the_raw_price_and_board_label_fields() -> None:
    js = dashboard_js()
    # 走 market 对象取值（不是 data-field 绑定：那套把缺字段格式化成「—」，
    # 没法区分"没有这个字段"和"字段是空的"，而这里必须能整行隐藏）
    assert "market.last_price_raw" in js
    assert "market.price_ratio" in js
    for needle in (
        "last_price_raw",
        "price_ratio",
        "board_label",
        "last_high_raw",
        "last_low_raw",
        "last_volume_lots",
        "last_amount",
        "last_bar_date",
    ):
        assert needle in js, f"bundle 未引用 {needle}"


def test_realtime_source_label_is_driven_by_upstream_not_hardcoded() -> None:
    """R35 换源成新浪后，面板不能再写死「东财实时价」。"""
    js = dashboard_js()
    assert "SOURCE_LABELS" in js
    assert "sina" in js
    assert "东财实时价" not in js
    assert "东财实时价" not in _html()


def test_volume_unit_and_a_share_relabels_and_stale_tooltip_present() -> None:
    js = dashboard_js()
    assert "万手" in js
    assert "亿元" in js
    assert "当日最高" in js
    assert "窗口交易日" in js
    assert "期望交易日" in js
    assert "个股概览" in js
    # 面板上的英文裸键换中文
    html = _html()
    assert "<span>stale</span>" not in html
    assert "<span>gap</span>" not in html
    assert "数据陈旧" in html
    assert "K 线缺口" in html


def test_change_pct_falls_back_and_never_shows_minus_100_on_open() -> None:
    """涨跌幅回落链（24h → 实时 → 末两根 bar）必须都在，且不除零。"""
    js = dashboard_js()
    assert 'changeSource || "unavailable"' in js
    assert "realtimeIsNewer" in js
    assert 'changeSource === "bars"' in js
    assert "prevClose > 0" in js


def test_change_amount_converts_hfq_prev_close_before_subtracting() -> None:
    """``dual_compare.realtime.prev_close`` 是**后复权域**，只有 raw_price 是不复权。

    实测 600519 / 2026-10-14：``raw_price=1271.77``、``prev_close=8151.84``、
    ``hfq_factor=6.39415165``。直接相减 = ``−6880.07``（"跌了 6880 元"的茅台）；
    正确 = ``1271.77 − 8151.84 / 6.39415165 = −3.12`` 元。
    """
    js = dashboard_js()
    assert "realtime.hfq_factor" in js, "涨跌额必须用上游随报价给的因子"
    assert "realtimeRawPrev / divisor" in js, "昨收必须先除回不复权域"
    # 带分号才是"直接相减后收工"的那个坏写法（正确写法后面还跟着 `/ divisor`）
    assert "realtimeRawPrice - realtimeRawPrev;" not in js, "禁止直接相减（量纲不同）"


def test_raw_fallback_basis_changes_the_price_basis_wording() -> None:
    """R64：``market.price_basis === "raw"`` 时口径行必须改口，且不许并排同样的数。

    2026-10-15 的真实形态：因子只到 10-13，K 线只认带因子的 bar ⇒ 旧行为下快照停在
    10-13。兜底（整段不复权）之后面板上的价**就是**不复权价，若照旧显示
    「后复权（因子未知）」，看板会把不复权价说成后复权价 —— 比不显示更坏。
    """
    js = dashboard_js()
    assert "market.price_basis" in js
    assert '"raw"' in js
    assert "不复权（当日缺复权因子，整段未复权）" in js
    assert "整段不复权：面板上的价就是不复权价" in js
