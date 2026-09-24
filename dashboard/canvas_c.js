/*
 * 画布 C —— plotly（R16-5）。
 *
 * 依赖：`./vendor/plotly-finance.min.js`（plotly.js 2.35.2 finance 构建，
 * MIT，1.17 MB；含 candlestick / scatter / shapes，比完整包 4.6 MB 小 4 倍）。
 *
 * 与画布 B 的差别不是"换了个库"：plotly 用 **layout.shapes** 画中枢矩形
 * （真矩形，时间×价格两个维度都对），而 LWC 只能画两条虚线边界；分型用
 * 两组 marker trace；笔用一条带 null 断点的 scatter（plotly 的标准分段写法，
 * 比"一笔一条 trace"轻得多）。
 *
 * 返回的 counts 是实际画出来的条数。
 */
(function () {
  "use strict";

  function plotlyApi() {
    return window.Plotly;
  }

  function msToDate(ms) {
    const value = Number(ms);
    return Number.isFinite(value) ? new Date(value) : null;
  }

  function render(view) {
    const api = plotlyApi();
    const node = view.canvas;
    const { clearRegion, appendNote } = window.CPTDashboard;
    clearRegion(node);
    if (view.volumeNode) clearRegion(view.volumeNode);
    if (view.macdNode) clearRegion(view.macdNode);
    if (view.axisNode) clearRegion(view.axisNode);
    if (view.volumeNode) appendNote(view.volumeNode, "成交量：plotly bar（叠加在主图底部）");
    if (view.macdNode) appendNote(view.macdNode, "画布 C 不渲染 MACD 副图（见画布 A）");
    if (view.axisNode) appendNote(view.axisNode, "时间轴由 plotly 自带");

    if (!api || typeof api.newPlot !== "function") {
      appendNote(node, "plotly 未加载：请确认 ./vendor/plotly-finance.min.js 可访问");
      return { canvas: "C", candles: 0, fractals: 0, bis: 0, zhongshus: 0, trendTypes: 0, error: "library_missing" };
    }

    const host = document.createElement("div");
    host.className = "cpt-canvas-host cpt-canvas-plotly";
    host.setAttribute("data-testid", "canvas-c-host");
    host.style.width = "100%";
    host.style.height = "100%";
    node.appendChild(host);
    node.dataset.rendered = "true";

    const traces = [];
    const candles = view.candles;
    traces.push({
      type: "candlestick",
      name: "K 线",
      x: candles.map((bar) => new Date(bar.openTime)),
      open: candles.map((bar) => bar.open),
      high: candles.map((bar) => bar.high),
      low: candles.map((bar) => bar.low),
      close: candles.map((bar) => bar.close),
      increasing: { line: { color: "#c0392b" } },
      decreasing: { line: { color: "#2e8b57" } },
    });

    // 笔：一条 scatter + null 断点（分段）。
    // 价格直接取笔自己的 high/low，**不做 K 线索引查找** —— 与窗口相交的笔可能
    // 起点在窗口之前（窗口内没有对应 K 线），查表会把它整条丢掉（首轮审计实测：
    // C 画 9 笔，A/B/D 画 10 笔）。
    const biX = [];
    const biY = [];
    let biDrawn = 0;
    (view.overlays.bis || []).forEach((bi) => {
      const start = msToDate(bi.start_time);
      const end = msToDate(bi.end_time);
      const high = Number(bi.high);
      const low = Number(bi.low);
      if (!start || !end || !Number.isFinite(high) || !Number.isFinite(low)) return;
      const up = Number(bi.direction) === 1;
      biX.push(start, end, null);
      biY.push(up ? low : high, up ? high : low, null);
      biDrawn += 1;
    });
    if (biX.length) {
      traces.push({
        type: "scatter",
        mode: "lines",
        name: "笔",
        x: biX,
        y: biY,
        line: { color: "#8e44ad", width: 2 },
        connectgaps: false,
        hoverinfo: "skip",
      });
    }

    // 分型：顶/底两组 marker。
    let fxDrawn = 0;
    [
      { kind: "top", symbol: "triangle-down", color: "#c0392b", price: "high" },
      { kind: "bottom", symbol: "triangle-up", color: "#2e8b57", price: "low" },
    ].forEach((style) => {
      const xs = [];
      const ys = [];
      (view.overlays.fractals || []).forEach((fx) => {
        if (String(fx.kind) !== style.kind) return;
        const time = msToDate(fx.start_time);
        const price = Number(fx[style.price]);
        if (!time || !Number.isFinite(price)) return;
        xs.push(time);
        ys.push(price);
        fxDrawn += 1;
      });
      if (!xs.length) return;
      traces.push({
        type: "scatter",
        mode: "markers",
        name: `分型-${style.kind}`,
        x: xs,
        y: ys,
        marker: { symbol: style.symbol, size: 9, color: style.color },
      });
    });

    // 中枢：layout.shapes 矩形（真二维矩形）。
    const shapes = [];
    let zsDrawn = 0;
    (view.overlays.zhongshus || []).forEach((zs) => {
      const start = msToDate(zs.start_time);
      const end = msToDate(zs.end_time);
      const high = Number(zs.high);
      const low = Number(zs.low);
      if (!start || !end || !Number.isFinite(high) || !Number.isFinite(low)) return;
      shapes.push({
        type: "rect",
        xref: "x",
        yref: "y",
        x0: start,
        x1: end,
        y0: low,
        y1: high,
        line: { color: "#4c9be8", width: 1, dash: "dot" },
        fillcolor: "rgba(76, 155, 232, 0.15)",
        layer: "below",
      });
      zsDrawn += 1;
    });

    // 成交量：独立 y 轴压在底部（domain 只占下 22%）。
    if (view.volumeNode) {
      traces.push({
        type: "bar",
        name: "成交量",
        x: candles.map((bar) => new Date(bar.openTime)),
        y: candles.map((bar) => (Number.isFinite(bar.volume) ? bar.volume : null)),
        yaxis: "y2",
        marker: {
          color: candles.map((bar) =>
            bar.direction === 1 ? "rgba(192,57,43,0.45)" : "rgba(46,139,87,0.45)",
          ),
        },
        hoverinfo: "skip",
      });
      appendNote(view.volumeNode, "成交量：plotly bar（叠加在主图底部）");
    }

    const layout = {
      margin: { l: 48, r: 12, t: 12, b: 28 },
      paper_bgcolor: "transparent",
      plot_bgcolor: "transparent",
      showlegend: true,
      legend: { orientation: "h", y: 1.08 },
      xaxis: { rangeslider: { visible: false }, type: "date" },
      yaxis: { domain: [0.24, 1], title: { text: "价格" } },
      yaxis2: { domain: [0, 0.2], anchor: "x", showgrid: false, title: { text: "量" } },
      shapes,
      dragmode: "pan",
    };

    api.newPlot(host, traces, layout, {
      displaylogo: false,
      responsive: true,
      scrollZoom: true,
      modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d"],
    });

    return {
      canvas: "C",
      candles: candles.length,
      fractals: fxDrawn,
      bis: biDrawn,
      zhongshus: zsDrawn,
      trendTypes: (view.overlays.trend_types || []).length,
      library: "plotly-finance",
    };
  }

  if (window.CPT_CANVASES) {
    window.CPT_CANVASES.register("C", {
      label: "C plotly",
      note: "plotly.js 2.35.2 finance 构建（vendor 内联，离线；中枢用真矩形）",
      draw: render,
    });
  }
})();
