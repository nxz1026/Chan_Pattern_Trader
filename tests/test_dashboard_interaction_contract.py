from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_dashboard_controls_have_visible_feedback_and_snapshot_reload() -> None:
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    html = (ROOT / "dashboard/index.html").read_text(encoding="utf-8")
    css = (ROOT / "dashboard/dashboard.css").read_text(encoding="utf-8")
    assert "function refreshSelectedSnapshot" in javascript
    assert "refreshSelectedSnapshot();" in javascript
    assert 'data-runtime-mode="offline"' in html
    assert 'data-view-mode="research"' in html
    assert "root.dataset.runtimeMode" in javascript
    assert "root.dataset.viewMode" in javascript
    assert 'body[data-runtime-mode="offline"]' in css
    assert "function noteKey" in javascript


def test_chart_svg_is_pinned_to_its_region_and_placeholder_texture_is_gone() -> None:
    """回归（2026-09-23）：图表 SVG 必须被钉在各自区域内。

    实测缺陷：`.cpt-chart-svg` 无任何 CSS 尺寸约束时，MACD 的 SVG 被拉伸成
    768×1649px 并压在 K 线图上（其容器只有 96px 高），叠加层占位假纹理
    （.chart-canvas::before）也会盖住真实图表。
    """
    css = (ROOT / "dashboard/dashboard.css").read_text(encoding="utf-8")
    assert ".cpt-chart-svg {" in css
    # SVG 钉死在容器内
    assert "position: absolute;" in css
    assert "inset: 0;" in css
    # 四个图表区都必须是裁剪容器
    for region in (".chart-canvas {", ".chart-volume {", ".chart-macd {", ".chart-time-axis {"):
        assert region in css, f"缺少区域样式: {region}"
    assert css.count("overflow: hidden;") >= 4
    # 占位假纹理不得回归
    assert ".chart-canvas::before" not in css
    assert "repeating-linear-gradient(\n      165deg" not in css


def test_crosshair_tooltip_host_is_outside_cleared_canvas() -> None:
    """回归（2026-09-23）：tooltip 不得挂在会被 clearRegion 清空的 canvas 内部。

    实测缺陷：`canvas.appendChild(tooltip)` + 每次重绘 `clearRegion(canvas)`
    把 tooltip 删掉，鼠标悬浮永远没有 OHLCV 详情。
    """
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    assert "host.appendChild(tooltip)" in javascript
    assert "canvas.appendChild(tooltip)" not in javascript
    assert 'q("[data-testid=chart-shell]")' in javascript
    assert "shellRect" in javascript


def test_default_zoom_window_keeps_candles_readable() -> None:
    """回归（2026-09-23）：600 根平铺会把蜡烛压成 1px 发丝线。

    默认窗口必须限制根数，且十字光标索引必须与渲染窗口一致。
    """
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    assert "DEFAULT_VISIBLE_BARS" in javascript
    assert "Math.max(1.5, Math.min(view.geom.slot * 0.62, 16))" in javascript
    # tooltip 索引基于 applyZoomWindow 之后的窗口
    assert "const shown = applyZoomWindow(all);" in javascript


def test_interval_tiers_and_default_hour() -> None:
    """回归（2026-09-23）：周期档位扩到 1m~1w，默认 1h。"""
    html = (ROOT / "dashboard/index.html").read_text(encoding="utf-8")
    expected = [
        ("60000", "1m"),
        ("180000", "3m"),
        ("300000", "5m"),
        ("900000", "15m"),
        ("1800000", "30m"),
        ("3600000", "1h"),
        ("7200000", "2h"),
        ("14400000", "4h"),
        ("21600000", "6h"),
        ("28800000", "8h"),
        ("43200000", "12h"),
        ("86400000", "1d"),
        ("259200000", "3d"),
        ("604800000", "1w"),
    ]
    for value, label in expected:
        assert f'<option value="{value}"' in html, f"缺少周期档位 {label}"
    assert '<option value="3600000" selected>1h</option>' in html, "默认周期必须是 1h"


def test_time_range_controls_exist() -> None:
    """回归（2026-09-23）：时间范围选择控件（起止时间 + 应用 + 回到实时 + 状态）。"""
    html = (ROOT / "dashboard/index.html").read_text(encoding="utf-8")
    for testid in ("range-start", "range-end", "range-apply", "range-live", "range-status"):
        assert f'data-testid="{testid}"' in html, f"缺少时间范围控件 {testid}"
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    # 请求参数名是冻结契约（后端 app.py 同名解析）
    assert 'url.searchParams.set("start_ms"' in javascript
    assert 'url.searchParams.set("end_ms"' in javascript
    # 固定区间后必须跳过轮询，且整段渲染（否则所选区间前半段被默认窗口截掉）
    assert "if (state.pinnedRange) return;" in javascript
    assert "if (state.pinnedRange) return candles;" in javascript
    assert "state.pinnedRange = { startMs, endMs };" in javascript
    assert "node.dataset.state = value;" in javascript


def test_interval_labels_cover_all_tiers() -> None:
    """回归（2026-09-23）：快照里的 interval_ms 必须显示成档位名，不是 "60m"/"1440m"。"""
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    assert "INTERVAL_LABELS" in javascript
    for label in ('60000: "1m"', '3600000: "1h"', '86400000: "1d"', '604800000: "1w"'):
        assert label in javascript, f"周期标签表缺少 {label}"


def test_quick_range_buttons_exist_with_frozen_dom_contract() -> None:
    """回归（2026-09-23）：最近 1/7/30 天快选按钮（DOM 契约与 JS 绑定一致）。"""
    html = (ROOT / "dashboard/index.html").read_text(encoding="utf-8")
    assert 'data-testid="range-quick"' in html
    for days in ("1d", "7d", "30d"):
        assert f'data-range-quick="{days}"' in html, f"缺少快选按钮 {days}"
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    assert "data-range-quick" in javascript


def test_out_of_window_overlays_are_skipped_not_collapsed() -> None:
    """回归（2026-09-23）：视窗外的区间型结构必须跳过，不能被压成通高竖条。

    `timeIndexOf` 会把窗口外时间吸附到 0/末尾，走势类型/中枢因此被画成
    宽约 slot(3.7px)、高约视窗高(1355px) 的橙色矩形（用户看到的"贯穿全高竖线"）。
    """
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    assert "const overlapsWindow = (view, startMs, endMs) =>" in javascript
    assert "windowStart: candles[0].openTime" in javascript
    assert "windowEnd: candles[candles.length - 1].openTime" in javascript
    # 两处区间型结构都必须先过守卫
    assert javascript.count("!overlapsWindow(view,") >= 2


def test_trend_type_renders_as_top_strip_not_full_height() -> None:
    """回归（2026-09-23）：走势类型画在顶部窄条带，不再铺满价格区。

    铺底时窄的走势类型会变成"贯穿全高的色条"、宽的变成大块暗色背景
    （视觉复审据此把可读性判为 3/5）；改为 10px 条带后复审给 5/5。
    """
    javascript = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
    assert "TREND_STRIP_HEIGHT" in javascript
    assert "height: TREND_STRIP_HEIGHT," in javascript
    # 不得再出现铺满绘图区高度的走势类型矩形
    assert "height: view.geom.plotHeight," not in javascript
