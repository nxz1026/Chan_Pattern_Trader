"""画布 D —— wbt 报告视图（R16-5）。

## 定位（与总计划 §6 的一处偏差，依据是实测）

计划把画布 D 定为「wbt report」，来源是
``references/wbt/python/wbt/report/html_builder.py``。实测（2026-09-24）发现两件事：

1. **wbt 报告画不了 K 线**。全仓 grep ``candlestick|ohlc``，``wbt/python/wbt`` 下
   只有 ``mock.py`` 生成 OHLCV **数据** 与「持仓K线数」统计字段，**没有任何**
   ``go.Candlestick`` / ``go.Ohlc`` 绘图代码；它的 5 个 tab 全是净值/回撤/分布/
   绩效表格，零价格图。
2. **wbt 报告的外壳带 6 个 CDN 外链**（``html_builder.py:650-654`` 与 ``:667``：
   Google Fonts ×3、bootstrap CSS ×1、bootstrap-icons CSS ×1、bootstrap JS ×1），
   离线打开会丢样式、tab 失效。

而 R16 验收要求「四个画布同一份快照渲染一致（结构元素数量一致）」+「断网可用」。
⇒ 画布 D 的落地方式是：**复用 wbt 的报告外壳与它的原生表格/指标卡（真实调用
``HtmlReportBuilder``），把 K 线 + 笔/中枢用 plotly 补上**；``render()`` 出来的
完整文档里，``<head>`` 的 CDN 外链被整段丢弃（只取 ``<body>`` 内容），改由
页面把 vendor 目录里的本地 bootstrap/plotly 注入同源 iframe。

## 为什么在 iframe 里渲染

wbt 的样式表会重排全局（``.container`` / ``.table`` / ``.nav-tabs``），
bootstrap 更是如此。直接注入主页面会打乱现有 CPT 看板（R12 刚验过 375px 移动端
触摸目标与水平溢出）。iframe 是**同源**的，父页面能读 ``contentDocument``，
所以「四个画布计数一致」的审计照样能做。

## 与客户端的契约

客户端把**当前可视窗口**（``view.windowStart`` / ``view.windowEnd``）一起传进来，
服务端据此过滤元素 —— 否则画布 A/B/C 只画窗口内的笔，画布 D 画全量，计数必然不等。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "WBT_PINNED_VERSION",
    "WbtUnavailableError",
    "build_canvas_d_payload",
    "extract_body_fragment",
]

#: 锁定的 wbt 版本；与 ``references/wbt`` 副本逐字节一致（实测 ``diff -q``）。
WBT_PINNED_VERSION: Final[str] = "0.9.1"

#: 从 ``render()`` 的完整文档里取 ``<body>`` 内部。
_BODY_RE: Final[re.Pattern[str]] = re.compile(
    r"<body[^>]*>(?P<body>.*)</body>", re.DOTALL | re.IGNORECASE
)
#: ``<script>...</script>``（含外链与内联）——innerHTML 注入不会执行脚本，
#: 必须由父页面取出后重新创建 ``<script>`` 元素。
_SCRIPT_RE: Final[re.Pattern[str]] = re.compile(
    r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE
)
#: 任何 CDN 外链（离线验收要求 0 个）——出现即视为构建缺陷。
_CDN_RE: Final[re.Pattern[str]] = re.compile(r"""(?:src|href)\s*=\s*["']https?://""", re.IGNORECASE)


class WbtUnavailableError(RuntimeError):
    """wbt 未安装（可选依赖 extra ``report``）。"""


def _import_wbt() -> Any:
    """延迟导入 wbt 并校验版本（与 czsc 后端同款稳健模式）。

    版本取自 ``importlib.metadata`` —— **不能**读 ``wbt.__version__``：实测
    wbt 0.9.1 的包命名空间里没有这个属性（``getattr(wbt, "__version__", None)``
    是 ``None``），据此判版本会误报"版本不符"并把画布 D 打成不可用。
    """
    try:
        import wbt.report  # noqa: PLC0415  (延迟导入是设计的一部分)
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于运行环境
        raise WbtUnavailableError(
            '画布 D 需要可选依赖 wbt，请安装：pip install -e ".[report]"'
        ) from exc

    from importlib.metadata import PackageNotFoundError  # noqa: PLC0415
    from importlib.metadata import version as dist_version

    try:
        installed = dist_version("wbt")
    except PackageNotFoundError:  # pragma: no cover - 源码直挂时会走到
        installed = None
    if installed is not None and installed != WBT_PINNED_VERSION:
        raise WbtUnavailableError(
            f"wbt 版本不符：期望 {WBT_PINNED_VERSION}，实际 {installed!r}；"
            f'请执行 pip install "wbt=={WBT_PINNED_VERSION}"'
        )
    return wbt


def extract_body_fragment(document: str) -> tuple[str, list[str]]:
    """从完整 HTML 文档里取出 ``<body>`` 内部，并把 ``<script>`` 抽出来。

    Returns:
        ``(body_html_without_scripts, scripts)``。脚本单独返回是因为
        ``innerHTML`` 注入**不会执行** ``<script>``，父页面必须重新创建元素。
    """
    match = _BODY_RE.search(document)
    if match is None:  # pragma: no cover - wbt 模板固定有 body
        raise ValueError("wbt 文档缺少 <body>")
    body = match.group("body")
    scripts = _SCRIPT_RE.findall(body)
    body = _SCRIPT_RE.sub("", body)
    return body, scripts


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _in_window(start_ms: Any, end_ms: Any, window: tuple[int, int] | None) -> bool:
    """元素时间区间是否与可视窗口相交（与 dashboard.js 的 ``overlapsWindow`` 同口径）。"""
    if window is None:
        return True
    start = _num(start_ms)
    end = _num(end_ms)
    if start is None:
        start = end
    if end is None:
        end = start
    if start is None or end is None:
        return False
    return not (end < window[0] or start > window[1])


def _filter(items: Sequence[Any], window: tuple[int, int] | None) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if _in_window(item.get("start_time"), item.get("end_time"), window):
            out.append(item)
    return out


def _candlestick_figure(
    candles: Sequence[Mapping[str, Any]],
    bis: Sequence[Mapping[str, Any]],
    zhongshus: Sequence[Mapping[str, Any]],
    fractals: Sequence[Mapping[str, Any]],
) -> Any:
    """plotly K 线 + 笔/中枢/分型叠加（``include_plotlyjs=False``，页面已 vendor）。"""
    from datetime import UTC, datetime

    import plotly.graph_objects as go  # noqa: PLC0415

    def stamp(ms: Any) -> Any:
        value = _num(ms)
        if value is None:
            return None
        return datetime.fromtimestamp(value / 1000, tz=UTC)

    times = [stamp(bar.get("open_time")) for bar in candles]
    figure = go.Figure()
    figure.add_trace(
        go.Candlestick(
            x=times,
            open=[_num(bar.get("open")) for bar in candles],
            high=[_num(bar.get("high")) for bar in candles],
            low=[_num(bar.get("low")) for bar in candles],
            close=[_num(bar.get("close")) for bar in candles],
            name="K 线",
            increasing_line_color="#c0392b",
            decreasing_line_color="#2e8b57",
        )
    )

    # 笔：所有笔塞进一条 scatter，用 None 断开分段（比"一笔一条 trace"轻得多）。
    bi_x: list[Any] = []
    bi_y: list[Any] = []
    price_by_time = {stamp(bar.get("open_time")): bar for bar in candles}
    for bi in bis:
        start = stamp(bi.get("start_time"))
        end = stamp(bi.get("end_time"))
        if start is None or end is None:
            continue
        direction = _num(bi.get("direction"))
        start_bar = price_by_time.get(start)
        end_bar = price_by_time.get(end)
        if start_bar is None or end_bar is None:
            continue
        # 向上的笔：起点取 low、终点取 high；向下反之（与 dashboard.js 同口径）。
        up = direction == 1
        bi_x.extend([start, end, None])
        bi_y.extend(
            [
                _num(start_bar.get("low")) if up else _num(start_bar.get("high")),
                _num(end_bar.get("high")) if up else _num(end_bar.get("low")),
                None,
            ]
        )
    if bi_x:
        figure.add_trace(
            go.Scatter(
                x=bi_x, y=bi_y, mode="lines", name="笔", line={"color": "#8e44ad", "width": 2}
            )
        )

    # 中枢：矩形（时间 × 价格）。
    for index, zs in enumerate(zhongshus):
        start = stamp(zs.get("start_time"))
        end = stamp(zs.get("end_time"))
        high = _num(zs.get("high"))
        low = _num(zs.get("low"))
        if start is None or end is None or high is None or low is None:
            continue
        figure.add_shape(
            type="rect",
            x0=start,
            x1=end,
            y0=low,
            y1=high,
            line={"color": "#4c9be8", "width": 1, "dash": "dot"},
            fillcolor="rgba(76, 155, 232, 0.15)",
            name=f"中枢{index}",
        )

    # 分型：顶/底两组标记。
    for kind, symbol, color in (
        ("top", "triangle-down", "#c0392b"),
        ("bottom", "triangle-up", "#2e8b57"),
    ):
        xs = [stamp(fx.get("start_time")) for fx in fractals if str(fx.get("kind")) == kind]
        ys = [
            _num(fx.get("high")) if kind == "top" else _num(fx.get("low"))
            for fx in fractals
            if str(fx.get("kind")) == kind
        ]
        pairs = [(x, y) for x, y in zip(xs, ys, strict=True) if x is not None and y is not None]
        if not pairs:
            continue
        figure.add_trace(
            go.Scatter(
                x=[p[0] for p in pairs],
                y=[p[1] for p in pairs],
                mode="markers",
                name=f"分型-{kind}",
                marker={"symbol": symbol, "size": 8, "color": color},
            )
        )

    figure.update_layout(
        height=520,
        margin={"l": 40, "r": 20, "t": 30, "b": 30},
        xaxis={"rangeslider": {"visible": False}, "title": "时间（UTC）"},
        yaxis={"title": "价格（后复权）"},
        legend={"orientation": "h", "y": 1.02},
        template="plotly_white",
    )
    return figure


def _metrics(counts: Mapping[str, Any], window_label: str) -> list[dict[str, Any]]:
    """结构统计卡片（走 wbt ``add_metrics``，``neutral=True`` 表示无涨跌语义）。"""
    return [
        {"label": "K 线", "value": str(counts["candles"]), "neutral": True},
        {"label": "分型", "value": str(counts["fractals"]), "neutral": True},
        {"label": "笔", "value": str(counts["bis"]), "neutral": True},
        {"label": "笔中枢", "value": str(counts["zhongshus"]), "neutral": True},
        {"label": "走势类型", "value": str(counts["trendTypes"]), "neutral": True},
        {"label": "可视窗口", "value": window_label, "neutral": True},
    ]


def build_canvas_d_payload(
    snapshot: Mapping[str, Any],
    *,
    window: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """把 dashboard snapshot 渲染成画布 D 的报告片段。

    Args:
        snapshot: ``build_dashboard_snapshot_v2`` 的产物（或 ``_empty_snapshot``）。
        window: ``(start_ms, end_ms)`` 可视窗口；``None`` 表示不过滤。客户端
            必须传它，否则画布 D 的计数与只画窗口的 A/B/C 不相等。

    Returns:
        ``{"available": bool, "source": str, "css": str, "body_html": str,
        "scripts": [...], "counts": {...}, "reason": str | None}``。

        ``available=False`` 时 ``reason`` 说明原因（wbt 未安装 / 无 K 线），
        前端画布 D 显示提示而不崩。
    """
    candles = [bar for bar in (snapshot.get("candles") or []) if isinstance(bar, Mapping)]
    overlays = snapshot.get("overlays") or {}
    if not isinstance(overlays, Mapping):
        overlays = {}
    fractals = _filter(overlays.get("fractals") or [], window)
    bis = _filter(overlays.get("bis") or [], window)
    zhongshus = _filter(overlays.get("zhongshus") or [], window)
    trend_types = _filter(overlays.get("trend_types") or [], window)
    if window is not None:
        candles = [
            bar for bar in candles if _in_window(bar.get("open_time"), bar.get("open_time"), window)
        ]

    counts: dict[str, Any] = {
        "canvas": "D",
        "candles": len(candles),
        "fractals": len(fractals),
        "bis": len(bis),
        "zhongshus": len(zhongshus),
        "trendTypes": len(trend_types),
    }

    if not candles:
        return {
            "available": False,
            "source": "wbt.report.HtmlReportBuilder",
            "reason": "no_candles_in_window",
            "css": "",
            "body_html": "",
            "scripts": [],
            "counts": counts,
        }

    try:
        wbt = _import_wbt()
    except WbtUnavailableError as exc:
        return {
            "available": False,
            "source": "wbt.report.HtmlReportBuilder",
            "reason": f"wbt_unavailable:{exc}",
            "css": "",
            "body_html": "",
            "scripts": [],
            "counts": counts,
        }

    from datetime import UTC, datetime  # noqa: PLC0415

    import pandas as pd  # noqa: PLC0415

    market = snapshot.get("market") or {}
    symbol = str(market.get("symbol") or "—")
    kind = str(market.get("kind") or "—")
    window_label = (
        "全部"
        if window is None
        else f"{datetime.fromtimestamp(window[0] / 1000, tz=UTC):%Y-%m-%d} → "
        f"{datetime.fromtimestamp(window[1] / 1000, tz=UTC):%Y-%m-%d}"
    )

    builder = wbt.report.HtmlReportBuilder(title=f"CPT 结构报告 · {symbol}", theme="light")
    builder.add_header(
        {"市场": kind, "代码": symbol, "可视窗口": window_label},
        subtitle=(
            "由 wbt HtmlReportBuilder 渲染的报告外壳"
            "（CDN 外链已在 CPT 侧剥离，改注入本地 vendor 资产）"
        ),
    )
    builder.add_metrics(_metrics(counts, window_label))

    figure = _candlestick_figure(candles, bis, zhongshus, fractals)
    builder.add_chart_tab(
        "K 线 + 缠论结构",
        figure.to_html(full_html=False, include_plotlyjs=False, div_id="cpt-canvas-d-chart"),
        active=True,
    )

    def _stamp(ms: Any) -> str:
        value = _num(ms)
        if value is None:
            return "—"
        return f"{datetime.fromtimestamp(value / 1000, tz=UTC):%Y-%m-%d}"

    if bis:
        builder.add_table(
            pd.DataFrame(
                [
                    {
                        "序号": index,
                        "方向": "向上" if _num(bi.get("direction")) == 1 else "向下",
                        "起点": _stamp(bi.get("start_time")),
                        "终点": _stamp(bi.get("end_time")),
                        "高": _num(bi.get("high")),
                        "低": _num(bi.get("low")),
                        "level": bi.get("level"),
                    }
                    for index, bi in enumerate(bis)
                ]
            ),
            title=f"笔明细（{len(bis)} 条，最多显示 30 行）",
            max_rows=30,
        )
    if zhongshus:
        builder.add_table(
            pd.DataFrame(
                [
                    {
                        "序号": index,
                        "起点": _stamp(zs.get("start_time")),
                        "终点": _stamp(zs.get("end_time")),
                        "zg": _num(zs.get("high")),
                        "zd": _num(zs.get("low")),
                        "纳入笔数": len(zs.get("bi_ids") or ()),
                    }
                    for index, zs in enumerate(zhongshus)
                ]
            ),
            title=f"笔中枢明细（{len(zhongshus)} 个）",
            max_rows=30,
        )
    builder.add_footer(f"CPT 只读看板 · 画布 D · wbt {WBT_PINNED_VERSION}")

    document = builder.render()
    body_html, raw_scripts = extract_body_fragment(document)
    # wbt 模板把 bootstrap 的 CDN ``<script src>`` 放在 **body 里**（``:667``），
    # 所以它不会出现在 ``body_html`` 上、只会出现在 scripts 里。离线验收要求
    # 0 个 CDN 引用 ⇒ 这里只保留**内联**脚本（无 ``src``），外链一律丢弃；
    # 页面自己会注入 vendor 目录里的本地 bootstrap。
    scripts = [s for s in raw_scripts if "src=" not in s.split(">", 1)[0].lower()]
    css = str(getattr(builder, "base_css", "") or "")
    leaked = (
        _CDN_RE.search(body_html) or _CDN_RE.search(css) or any(_CDN_RE.search(s) for s in scripts)
    )
    if leaked:  # pragma: no cover - 防御性：模板升级引入外链时响亮失败
        return {
            "available": False,
            "source": "wbt.report.HtmlReportBuilder",
            "reason": "cdn_reference_leaked",
            "css": "",
            "body_html": "",
            "scripts": [],
            "counts": counts,
        }
    return {
        "available": True,
        "source": f"wbt.report.HtmlReportBuilder@{WBT_PINNED_VERSION}",
        "reason": None,
        "css": css,
        "body_html": body_html,
        "scripts": scripts,
        "counts": counts,
    }
