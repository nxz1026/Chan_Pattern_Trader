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
