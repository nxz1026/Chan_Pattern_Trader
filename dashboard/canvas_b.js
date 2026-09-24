/*
 * 画布 B —— lightweight-charts（R16-5）。
 *
 * 依赖：`./vendor/lightweight-charts.standalone.production.js`（v4.2.0，Apache-2.0，
 * 内联进仓库，见 `vendor/README.md`；总计划决策 E2 明确不走 CDN）。
 *
 * 映射关系（四个画布必须画同一批元素）：
 *   K 线   → candlestick series
 *   笔     → **一笔一条 line series**（起点/终点两个点）。不合并成一条折线：
 *            笔之间可能有缺口，合并会把不连续的笔连成假线段。
 *   笔中枢 → 每个中枢两条虚线 line series（zg 上沿 / zd 下沿，横跨中枢时间区间）。
 *            LWC v4 没有矩形图元，价格线（createPriceLine）会横贯整幅图、
 *            表达不出中枢的时间范围，所以用两条线而不是价格线。
 *   分型   → 一个 markers 数组（顶=向下三角，底=向上三角）。
 *   成交量 → histogram series（独立价格刻度，压在图底部）。
 *
 * 返回的 counts 是**实际画出来的条数**，供审计脚本断言四画布一致。
 */
(function () {
  "use strict";

  const CHART_API = () => window.LightweightCharts;

  function toTime(ms) {
    // LWC 用秒级 UTCTimestamp。
    return Math.floor(Number(ms) / 1000);
  }

  function overlayPrice(overlays, timeMs, want) {
    // 分型价格：顶取 high、底取 low；与 dashboard.js 的 fractalPrice 同口径。
    const items = (overlays && overlays.fractals) || [];
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      if (Number(item.start_time) === Number(timeMs) || Number(item.end_time) === Number(timeMs)) {
        const high = Number(item.high);
        const low = Number(item.low);
        if (want === "high") return Number.isFinite(high) ? high : low;
        return Number.isFinite(low) ? low : high;
      }
    }
    return null;
  }

  function render(view) {
    const api = CHART_API();
    const node = view.canvas;
    const { clearRegion, appendNote } = window.CPTDashboard;
    clearRegion(node);
    if (view.volumeNode) clearRegion(view.volumeNode);
    if (view.macdNode) clearRegion(view.macdNode);
    if (view.axisNode) clearRegion(view.axisNode);
    if (view.macdNode) appendNote(view.macdNode, "画布 B 不渲染 MACD 副图（见画布 A）");
    if (view.axisNode) appendNote(view.axisNode, "时间轴由 lightweight-charts 自带");

    if (!api || typeof api.createChart !== "function") {
      appendNote(node, "lightweight-charts 未加载：请确认 ./vendor/lightweight-charts.standalone.production.js 可访问");
      return { canvas: "B", candles: 0, fractals: 0, bis: 0, zhongshus: 0, trendTypes: 0, error: "library_missing" };
    }

    const host = document.createElement("div");
    host.className = "cpt-canvas-host cpt-canvas-lwc";
    host.setAttribute("data-testid", "canvas-b-host");
    node.appendChild(host);
    node.dataset.rendered = "true";

    const chart = api.createChart(host, {
      width: Math.max(40, view.width),
      height: Math.max(40, view.height),
      layout: { background: { color: "transparent" }, textColor: "#4a5568" },
      grid: { vertLines: { color: "rgba(0,0,0,0.06)" }, horzLines: { color: "rgba(0,0,0,0.06)" } },
      rightPriceScale: { borderColor: "rgba(0,0,0,0.15)" },
      timeScale: { borderColor: "rgba(0,0,0,0.15)", timeVisible: true },
      crosshair: { mode: 0 },
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: "#c0392b",
      downColor: "#2e8b57",
      borderUpColor: "#c0392b",
      borderDownColor: "#2e8b57",
      wickUpColor: "#c0392b",
      wickDownColor: "#2e8b57",
    });
    candleSeries.setData(
      view.candles.map((bar) => ({
        time: toTime(bar.openTime),
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })),
    );

    // 成交量：独立刻度压在图底部（scaleMargins 让 K 线只占上 75%）。
    if (view.volumeNode) {
      const volumeSeries = chart.addHistogramSeries({
        priceFormat: { type: "volume" },
        priceScaleId: "volume",
        color: "rgba(76, 155, 232, 0.45)",
      });
      volumeSeries.setData(
        view.candles
          .filter((bar) => Number.isFinite(bar.volume))
          .map((bar) => ({
            time: toTime(bar.openTime),
            value: bar.volume,
            color: bar.direction === 1 ? "rgba(192,57,43,0.45)" : "rgba(46,139,87,0.45)",
          })),
      );
      chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
      appendNote(view.volumeNode, "成交量：lightweight-charts histogram（叠加在主图底部）");
    }

    // 笔：一笔一条 line series。
    let biDrawn = 0;
    (view.overlays.bis || []).forEach((bi) => {
      const start = Number(bi.start_time);
      const end = Number(bi.end_time);
      if (!Number.isFinite(start) || !Number.isFinite(end)) return;
      const up = Number(bi.direction) === 1;
      const startPrice = up ? Number(bi.low) : Number(bi.high);
      const endPrice = up ? Number(bi.high) : Number(bi.low);
      const startFallback = overlayPrice(view.overlays, start, up ? "low" : "high");
      const endFallback = overlayPrice(view.overlays, end, up ? "high" : "low");
      const y0 = Number.isFinite(startPrice) ? startPrice : startFallback;
      const y1 = Number.isFinite(endPrice) ? endPrice : endFallback;
      if (!Number.isFinite(y0) || !Number.isFinite(y1)) return;
      const series = chart.addLineSeries({
        color: up ? "#c0392b" : "#2e8b57",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      series.setData([
        { time: toTime(start), value: y0 },
        { time: toTime(end), value: y1 },
      ]);
      biDrawn += 1;
    });

    // 中枢：每个两条虚线（zg / zd）。
    let zsDrawn = 0;
    (view.overlays.zhongshus || []).forEach((zs) => {
      const start = Number(zs.start_time);
      const end = Number(zs.end_time);
      const high = Number(zs.high);
      const low = Number(zs.low);
      if (![start, end, high, low].every(Number.isFinite)) return;
      ["#4c9be8", "#4c9be8"].forEach((color, index) => {
        const series = chart.addLineSeries({
          color,
          lineWidth: 1,
          lineStyle: 2,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        const value = index === 0 ? high : low;
        series.setData([
          { time: toTime(start), value },
          { time: toTime(end), value },
        ]);
      });
      zsDrawn += 1;
    });

    // 分型：markers。
    let fxDrawn = 0;
    const markers = [];
    (view.overlays.fractals || []).forEach((fx) => {
      const time = Number(fx.start_time);
      if (!Number.isFinite(time)) return;
      const kind = String(fx.kind);
      if (kind !== "top" && kind !== "bottom") return;
      const price = kind === "top" ? Number(fx.high) : Number(fx.low);
      if (!Number.isFinite(price)) return;
      markers.push({
        time: toTime(time),
        position: kind === "top" ? "aboveBar" : "belowBar",
        color: kind === "top" ? "#c0392b" : "#2e8b57",
        shape: kind === "top" ? "arrowDown" : "arrowUp",
        text: kind === "top" ? "顶" : "底",
      });
      fxDrawn += 1;
    });
    markers.sort((left, right) => left.time - right.time);
    if (markers.length && typeof candleSeries.setMarkers === "function") {
      candleSeries.setMarkers(markers);
    }
    chart.timeScale().fitContent();

    return {
      canvas: "B",
      candles: view.candles.length,
      fractals: fxDrawn,
      bis: biDrawn,
      zhongshus: zsDrawn,
      trendTypes: (view.overlays.trend_types || []).length,
      library: "lightweight-charts",
    };
  }

  if (window.CPT_CANVASES) {
    window.CPT_CANVASES.register("B", {
      label: "B lightweight-charts",
      note: "TradingView lightweight-charts v4.2.0（vendor 内联，离线）",
      draw: render,
    });
  }
})();
