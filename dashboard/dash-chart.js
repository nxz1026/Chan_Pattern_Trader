// dash-chart.js — CPT 看板 · 由 dashboard/dashboard.js 按功能拆分（R45）
//
// 画布 A（手写 SVG）的绘制与交互：MACD 计算、图元绘制、缩放、十字光标。
// 
// 单独成模块是因为它**边界最清楚**（给定 view 画出 SVG）、
// 且体量最大（≈1100 行）—— 混在 core 里时谁改绘制都要翻 5000 行。
//
// ⚠️ **机械搬运**：没有改任何一行逻辑。
//    共享的 IIFE 头在每个模块里各存一份（`dash-core.js` 持有权威副本
//    并挂到 window）—— 跨模块调用因此能跑，代价是暂时有重复。
//    去重是下一步，不在本次范围。

// <<<HEAD
/*
 * CPT Dashboard · 只读看板前端脚本（D3：K 线 / 成交量 / 缠论结构叠加渲染）
 *
 * 边界（docs/dashboard-plan.md）：
 * - 只消费 dashboard.v2 snapshot，不复制 domain 算法，不新增行情 HTTP 逻辑；
 * - 零第三方依赖：手写 SVG，无 CDN、无网络字体、无远程资源；
 * - 只读：不提供任何下单/撤单/仓位入口。
 *
 * 对外契约（D2 起稳定，D3 只做加法）：
 *   window.CPTDashboard.render(snapshot)      → 渲染 snapshot（顶层 8 键）
 *   window.CPTDashboard.loadSnapshot(url)     → fetch 一个 snapshot 并渲染
 *   window.CPTDashboard.demoSnapshot()        → 返回离线 demo snapshot（深拷贝）
 *   window.CPTDashboard.loadDemo()            → 渲染离线 demo snapshot（不发起网络请求）
 *   window.CPTDashboard.clear()               → 回到 empty 状态
 *   window.CPTDashboard.getSnapshot()         → 当前 snapshot
 *   window.CPTDashboard.getSelection()        → 当前选中结构 payload
 *   window.CPTDashboard.schemaVersion         → "dashboard.v2"
 * 事件：cpt:dashboard-ready / cpt:structure-selected
 *
 * 数据流：snapshot → 归一化 → 像素布局（实测容器尺寸）→ SVG 元素；
 * 所有 DOM 文本走 textContent，禁止把 snapshot 内容拼进 HTML 字符串。
 */

(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const SCHEMA_VERSION = "dashboard.v2";
  const MS_PER_MINUTE = 60000;
  const RUNTIME_STYLE_ID = "cpt-dashboard-runtime-style";
  /* 默认只渲染最后 180 根：600 根平铺进约 670px 会把蜡烛压成 1px 发丝线，无法判读。 */
  const DEFAULT_VISIBLE_BARS = 180;

  /* ---- Phase N0：信号「说人话」—— 状态中文映射 ---- */
  const STATUS_LABELS = {
    structure_ready: "结构已就绪（尚未触发）",
    alert: "⚠️ 预警：反向K线盘中出现，随时可能消失",
    candidate: "候选：反向K线已收盘，仍成立",
    confirmed: "已确认：后续反向新笔成立",
    invalidated: "已失效：结构被破坏（终态）",
    none: "暂无信号",
  };

  const STATUS_EDUCATION = {
    structure_ready: "结构条件已满足，等待反向K线触发预警。",
    alert: "盘中出现反向K线，但尚未收盘确认——信号可能随时消失，不得当作已确认。",
    candidate: "反向K线已收盘且结构仍成立，等待后续反向新笔确认。",
    confirmed: "后续反向新笔已成立，信号由「候选」升级为「已确认」。",
    invalidated: "后续结构被结构破坏，信号已失效（终态），不再跟踪。",
    none: "当前无信号。",
  };

  const DIVERGENCE_LABELS = {
    not_checked: "未检查",
    not_detected: "未检测到背驰",
    detected: "检测到背驰",
  };

  const SIGNAL_TYPE_LABELS = {
    first_buy: "一买信号",
    first_sell: "一卖信号",
  };

  /* ---- Phase A1：range.reason 中文映射（历史区间查询失败原因） ---- */
  const RANGE_REASON_LABELS = {
    range_too_short: "区间过短（不足 3 根 K 线）",
    upstream_range_unavailable: "上游区间数据不可用",
  };

  /* ---- Phase B1/B2：数据源探活面板文案 ---- */
  // probe.status 中文映射（数据源探活）
  const PROBE_STATUS_LABELS = {
    ok: "可用",
    degraded: "降级",
    unavailable: "不可用",
    skipped: "已跳过",
  };
  // source.role 中文映射
  const SOURCE_ROLE_LABELS = { primary: "主通道", fallback: "兜底", local: "本地库" };
// >>>HEAD
// <<<FUNCS
  function priceDomain(candles, overlays) {
    let min = Infinity;
    let max = -Infinity;
    const push = (value) => {
      if (value === null) return;
      if (value < min) min = value;
      if (value > max) max = value;
    };
    candles.forEach((bar) => {
      push(bar.low);
      push(bar.high);
    });
    ["fractals", "bis", "zhongshus", "trend_types"].forEach((key) => {
      overlays[key].forEach((item) => {
        push(num(item.low));
        push(num(item.high));
      });
    });
    if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
    if (max - min < 1e-9) {
      max += 1;
      min -= 1;
    }
    const margin = (max - min) * 0.08;
    return { min: min - margin, max: max + margin };
  }

  const layoutPlot = (width, height, count) => {
    const left = PAD.left;
    const top = PAD.top;
    const plotWidth = Math.max(10, width - PAD.left - PAD.right);
    const plotHeight = Math.max(10, height - PAD.top - PAD.bottom);
    return { width, height, left, top, plotWidth, plotHeight, slot: plotWidth / count };
  };

  /**
   * 渲染窗口：state.visibleWindow 显式指定时按其切片；未指定时是默认视图——
   * 只取最后 DEFAULT_VISIBLE_BARS 根，长度不足则全部。
   */

  function applyZoomWindow(candles) {
    if (!candles.length) return candles;
    if (state.visibleWindow) {
      const [start, end] = state.visibleWindow;
      const clampedStart = Math.max(0, Math.min(start, candles.length - 1));
      const clampedEnd = Math.max(clampedStart + 1, Math.min(end, candles.length));
      return candles.slice(clampedStart, clampedEnd);
    }
    // 用户主动选定的历史区间：整段展示，不再套"最近 N 根"默认窗口，
    // 否则所选区间的前半段会被静默截掉（2026-09-23 端到端实测）。
    if (state.pinnedRange) return candles;
    if (candles.length > DEFAULT_VISIBLE_BARS) return candles.slice(candles.length - DEFAULT_VISIBLE_BARS);
    return candles;
  }


  function payloadFor(kind, item, view, stateValue) {
    const raw = item || {};
    // 中枢契约没有 source_ids，用 bi_ids 追溯成分笔（与图形 data-source-ids 一致）。
    const rawIds = kind === "zhongshu" ? raw.bi_ids : raw.source_ids;
    const sourceIds = asArray(rawIds).map(String);
    const barIndex = num(raw.bar_index);
    const close = num(raw.close);
    const payload = {
      kind,
      id: sourceIds.length ? sourceIds[0] : null,
      level: num(raw.level),
      direction: num(raw.direction),
      startTime: num(raw.start_time),
      endTime: num(raw.end_time),
      high: num(raw.high),
      low: num(raw.low),
      price: close === null ? num(raw.price) : close,
      close,
      barIndex,
      openTime: num(raw.open_time),
      isClosed: typeof raw.is_closed === "boolean" ? raw.is_closed : false,
      status: typeof raw.status === "string" ? raw.status : null,
      trendKind: typeof raw.kind === "string" ? raw.kind : null,
      fractalKind: kind === "fractal" && (raw.kind === "top" || raw.kind === "bottom") ? raw.kind : null,
      sourceIds,
      state: stateValue,
      raw,
    };
    if (kind === "candle") {
      payload.startTime = payload.openTime;
      payload.endTime = payload.openTime;
      payload.high = num(raw.high);
      payload.low = num(raw.low);
      payload.open = num(raw.open);
      payload.isClosed = raw.is_closed !== false;
      payload.state = payload.isClosed ? "confirmed" : "alert";
    }
    if (kind === "signal") {
      payload.state = typeof raw.status === "string" ? raw.status : "none";
      payload.startTime = num(raw.confirmed_time) || num(raw.candidate_time) || num(raw.alert_time);
      payload.endTime = payload.startTime;
    }
    payload.label = selectionText(payload);
    return payload;
  }

  /* -------------------------------------------------------------- 图形绘制 */

  const clearRegion = (node) => {
    while (node.firstChild) node.removeChild(node.firstChild);
    node.dataset.rendered = "false";
  };

  const appendNote = (node, text) => {
    node.appendChild(createHtml("p", { class: "placeholder-note" }, text));
    return node;
  };


  function drawPriceGrid(group, view) {
    for (let step = 0; step <= GRID_LEVELS; step += 1) {
      const price = view.domain.min + ((view.domain.max - view.domain.min) * step) / GRID_LEVELS;
      const y = view.yForPrice(price);
      group.appendChild(
        paint(
          createSvg("line", {
            x1: view.geom.left,
            x2: view.geom.left + view.geom.plotWidth,
            y1: y,
            y2: y,
          }),
          { stroke: "var(--color-grid)", "stroke-width": "1" },
        ),
      );
      group.appendChild(
        createSvg("text", { x: 6, y: y + 3 }, formatPrice(price)),
      );
    }
  }

  /* 顶部走势类型条带用的填充：条带只有 10px 高，需要有足够对比度才看得见，
     但因为不再铺满价格区，0.35 也不会造成视觉噪音。 */
  const trendFill = (direction) =>
    direction === 1
      ? "rgba(22, 199, 132, 0.35)"
      : direction === -1
        ? "rgba(234, 57, 67, 0.35)"
        : "rgba(240, 185, 11, 0.35)";

  // `timeIndexOf` 会把窗口外时间吸附到 0/末尾；区间型结构若两端都落在窗口外，
  // 会被压成 slot 宽（约 3.7px）、通高的竖条（用户看到的"贯穿全高橙黄竖线"）。
  // 画之前先按真实时间判断是否与视窗相交，完全在外的直接跳过。
  const overlapsWindow = (view, startMs, endMs) =>
    startMs !== null && endMs !== null && endMs >= view.windowStart && startMs <= view.windowEnd;

  /* 走势类型：绘制在绘图区**顶部的一条窄带**，而不是铺满整个价格区。
     铺底会把窄的走势类型变成"贯穿全高的色条"、宽的变成大块暗色背景，
     视觉复审（2026-09-23）判定这是主要噪音来源。 */
  const TREND_STRIP_HEIGHT = 10;


  function drawTrendBackgrounds(group, view) {
    view.overlays.trend_types.forEach((item) => {
      const startTime = num(item.start_time);
      const endTime = num(item.end_time);
      if (!overlapsWindow(view, startTime, endTime)) return;
      const startIndex = view.indexForTime(startTime);
      const endIndex = view.indexForTime(endTime);
      if (startIndex === null || endIndex === null) return;
      const left = view.xForIndex(Math.min(startIndex, endIndex)) - view.geom.slot / 2;
      const width = Math.max(2, Math.abs(endIndex - startIndex) * view.geom.slot + view.geom.slot);
      const element = createSvg("rect", {
        x: left,
        // 放在绘图区上方的内边距里，完全不压价格区
        y: Math.max(0, view.geom.top - TREND_STRIP_HEIGHT),
        width,
        height: TREND_STRIP_HEIGHT,
        rx: 2,
        "data-structure-kind": "trend_type",
        "data-trend-kind": item.kind,
        "data-level": num(item.level),
        "data-state": structureStateOf(view, num(item.end_time)),
        "data-source-ids": asArray(item.source_ids).join(" "),
      });
      paint(element, {
        fill: trendFill(num(item.direction)),
        stroke: "var(--color-accent)",
        "stroke-opacity": "0.35",
        "stroke-width": "1",
      });
      group.appendChild(attachHit(element, payloadFor("trend_type", item, view, structureStateOf(view, num(item.end_time)))));
    });
  }


  function drawZhongshus(group, view) {
    view.overlays.zhongshus.forEach((item) => {
      const startTime = num(item.start_time);
      const endTime = num(item.end_time);
      if (!overlapsWindow(view, startTime, endTime)) return;
      const startIndex = view.indexForTime(startTime);
      const endIndex = view.indexForTime(endTime);
      const high = num(item.high);
      const low = num(item.low);
      if (startIndex === null || endIndex === null || high === null || low === null) return;
      const left = view.xForIndex(Math.min(startIndex, endIndex)) - view.geom.slot / 2;
      const width = Math.max(2, Math.abs(endIndex - startIndex) * view.geom.slot + view.geom.slot);
      const top = view.yForPrice(high);
      const height = Math.max(2, view.yForPrice(low) - top);
      const element = createSvg("rect", {
        x: left,
        y: top,
        width,
        height,
        rx: 2,
        "data-structure-kind": "zhongshu",
        "data-level": num(item.level),
        "data-state": structureStateOf(view, endTime),
        "data-source-ids": asArray(item.bi_ids).join(" "),
      });
      paint(element, {
        fill: "rgba(76, 155, 232, 0.15)",
        stroke: "var(--color-loading)",
        "stroke-width": "1",
        "stroke-dasharray": "4 3",
      });
      group.appendChild(attachHit(element, payloadFor("zhongshu", item, view, structureStateOf(view, endTime))));
      if (state.selection && state.selection.kind === "zhongshu" && startTime === state.selection.startTime) {
        group.appendChild(
          paint(
            createSvg("text", { x: left + 4, y: top - 4 }, `中枢 L${num(item.level)} ${formatPrice(high)}—${formatPrice(low)}`),
            { fill: "var(--color-loading)" },
          ),
        );
      }
    });
  }


  function drawCandles(group, view) {
    const bodyWidth = Math.max(1.5, Math.min(view.geom.slot * 0.62, 16));
    view.candles.forEach((bar, index) => {
      const x = view.xForIndex(index);
      const alert = !bar.isClosed;
      const color = alert ? "var(--color-alert)" : bar.direction === 1
        ? "var(--color-up)"
        : bar.direction === -1
          ? "var(--color-down)"
          : "var(--color-flat)";
      const wick = createSvg("line", {
        x1: x,
        x2: x,
        y1: view.yForPrice(bar.high),
        y2: view.yForPrice(bar.low),
        "data-structure-kind": "candle",
        "data-bar-index": index,
        "data-open-time": bar.openTime,
        "data-direction": bar.direction,
        "data-state": alert ? "alert" : "confirmed",
      });
      paint(wick, { stroke: color, "stroke-width": "1", "stroke-opacity": "0.9" });
      const top = view.yForPrice(Math.max(bar.open, bar.close));
      const height = Math.max(1, view.yForPrice(Math.min(bar.open, bar.close)) - top);
      const body = createSvg("rect", {
        x: x - bodyWidth / 2,
        y: top,
        width: bodyWidth,
        height,
        "data-structure-kind": "candle",
        "data-bar-index": index,
        "data-open-time": bar.openTime,
        "data-direction": bar.direction,
        "data-state": alert ? "alert" : "confirmed",
        "data-is-closed": bar.isClosed ? "true" : "false",
      });
      paint(body, { fill: color, "fill-opacity": bodyWidth > 2 ? "0.85" : "1" });
      const payload = payloadFor("candle", {
        open_time: bar.openTime,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
        is_closed: bar.isClosed,
        source_ids: [],
      }, view, alert ? "alert" : "confirmed");
      const title = createSvg("title", {});
      title.textContent =
        `${formatAxisTime(bar.openTime)} O ${formatPrice(bar.open)} H ${formatPrice(bar.high)} ` +
        `L ${formatPrice(bar.low)} C ${formatPrice(bar.close)}${alert ? "（未收盘）" : ""}`;
      body.appendChild(title);
      [wick, body].forEach((node) => {
        node.classList.add("cpt-chart-hit");
        node.addEventListener("click", () => selectStructure(body, payload));
      });
      group.appendChild(wick);
      group.appendChild(body);
    });
  }


  function fractalPrice(fractal, fallback) {
    if (!isObject(fractal)) return fallback;
    const high = num(fractal.high);
    const low = num(fractal.low);
    if (fractal.kind === "top") return high === null ? fallback : high;
    if (fractal.kind === "bottom") return low === null ? fallback : low;
    return fallback;
  }


  function drawBis(group, view) {
    const byStart = new Map();
    const byEnd = new Map();
    view.overlays.fractals.forEach((item) => {
      const start = num(item.start_time);
      const end = num(item.end_time);
      if (start !== null && !byStart.has(start)) byStart.set(start, item);
      if (end !== null && !byEnd.has(end)) byEnd.set(end, item);
    });
    view.overlays.bis.forEach((item) => {
      const direction = num(item.direction);
      const startTime = num(item.start_time);
      const endTime = num(item.end_time);
      const startIndex = view.indexForTime(startTime);
      const endIndex = view.indexForTime(endTime);
      if (startIndex === null || endIndex === null) return;
      const low = num(item.low);
      const high = num(item.high);
      if (low === null || high === null) return;
      const up = direction === 1;
      const startPrice = fractalPrice(byStart.get(startTime), up ? low : high);
      const endPrice = fractalPrice(byEnd.get(endTime), up ? high : low);
      const stateValue = structureStateOf(view, endTime);
      // ashare: 标签（涨跌停/炸板/一字板）→ 端点可信度低，画虚线 + 降透明
      const hasAshareTag = asArray(item.source_ids).some((id) => String(id).startsWith("ashare:"));
      const element = createSvg("path", {
        d: `M ${view.xForIndex(startIndex)} ${view.yForPrice(startPrice)} L ${view.xForIndex(endIndex)} ${view.yForPrice(endPrice)}`,
        fill: "none",
        "data-structure-kind": "bi",
        "data-direction": direction,
        "data-level": num(item.level),
        "data-state": stateValue,
        "data-source-ids": asArray(item.source_ids).join(" "),
        "data-ashare-tag": hasAshareTag ? "true" : "false",
      });
      paint(element, {
        stroke: up ? "var(--color-up)" : "var(--color-down)",
        "stroke-width": "2",
        "stroke-linecap": "round",
        "stroke-dasharray": stateValue === "alert" || hasAshareTag ? "5 3" : "none",
        opacity: hasAshareTag ? "0.5" : "1",
      });
      group.appendChild(attachHit(element, payloadFor("bi", item, view, stateValue)));
    });
  }


  function drawFractals(group, view) {
    view.overlays.fractals.forEach((item) => {
      const kind = item.kind === "bottom" ? "bottom" : item.kind === "top" ? "top" : null;
      if (!kind) return;
      const price = fractalPrice(item, null);
      const index = view.indexForTime(num(item.start_time));
      if (price === null || index === null) return;
      const x = view.xForIndex(index);
      const y = view.yForPrice(price);
      const d = kind === "top"
        ? `M ${x - 4} ${y - 11} L ${x + 4} ${y - 11} L ${x} ${y - 3} Z`
        : `M ${x - 4} ${y + 11} L ${x + 4} ${y + 11} L ${x} ${y + 3} Z`;
      const stateValue = structureStateOf(view, num(item.end_time));
      const element = createSvg("path", {
        d,
        "data-structure-kind": "fractal",
        "data-fractal-kind": kind,
        "data-bar-index": num(item.bar_index),
        "data-level": num(item.level),
        "data-state": stateValue,
        "data-source-ids": asArray(item.source_ids).join(" "),
      });
      paint(element, {
        fill: kind === "top" ? "var(--color-down)" : "var(--color-up)",
        stroke: "var(--color-bg)",
        "stroke-width": "1",
      });
      group.appendChild(attachHit(element, payloadFor("fractal", item, view, stateValue)));
    });
  }


  function drawSignal(group, view) {
    const signal = isObject(state.snapshot) && isObject(state.snapshot.signal) ? state.snapshot.signal : null;
    if (!signal) return;
    const price = num(signal.price);
    const time = num(signal.confirmed_time) || num(signal.candidate_time) || num(signal.alert_time);
    const index = view.indexForTime(time);
    if (price === null || index === null) return;
    const y = view.yForPrice(price);
    const stateValue = typeof signal.status === "string" ? signal.status : "none";
    const element = createSvg("line", {
      x1: view.geom.left,
      x2: view.geom.left + view.geom.plotWidth,
      y1: y,
      y2: y,
      "data-structure-kind": "signal",
      "data-signal-status": stateValue,
      "data-state": stateValue,
      "data-source-ids": asArray(signal.source_ids).join(" "),
    });
    paint(element, {
      stroke: stateValue === "confirmed" ? "var(--color-confirmed)" : "var(--color-alert)",
      "stroke-width": "1",
      "stroke-dasharray": "6 4",
    });
    group.appendChild(attachHit(element, payloadFor("signal", signal, view, stateValue)));
  }


  function drawLastPrice(group, view) {
    const last = view.candles[view.candles.length - 1];
    const y = view.yForPrice(last.close);
    const element = createSvg("line", {
      x1: view.geom.left,
      x2: view.geom.left + view.geom.plotWidth,
      y1: y,
      y2: y,
      "data-structure-kind": "last_price",
      "data-state": last.isClosed ? "confirmed" : "alert",
    });
    paint(element, {
      stroke: last.isClosed ? "var(--color-text-faint)" : "var(--color-alert)",
      "stroke-width": "1",
      "stroke-dasharray": "2 3",
    });
    group.appendChild(element);
    group.appendChild(
      paint(
        createSvg("text", {
          x: view.geom.left + view.geom.plotWidth - 4,
          y: y - 4,
          "text-anchor": "end",
        }, `${formatPrice(last.close)}${last.isClosed ? "" : "（未收盘 alert）"}`),
        { fill: last.isClosed ? "var(--color-text-dim)" : "var(--color-alert)" },
      ),
    );
  }


  function drawVolume(volumeNode, view, volumeHeight) {
    const maxVolume = view.candles.reduce((max, bar) => Math.max(max, bar.volume === null ? 0 : bar.volume), 0);
    if (maxVolume <= 0) {
      appendNote(volumeNode, "暂无成交量字段（volume 为空或全 0）");
      volumeNode.dataset.rendered = "false";
      return;
    }
    const svg = createSvg("svg", {
      class: "cpt-chart-svg",
      viewBox: `0 0 ${view.geom.width} ${volumeHeight}`,
      preserveAspectRatio: "none",
      role: "presentation",
    });
    const group = createSvg("g", {});
    svg.appendChild(group);
    const bodyWidth = Math.max(1, Math.min(view.geom.slot * 0.62, 16));
    view.candles.forEach((bar, index) => {
      const volume = bar.volume === null ? 0 : bar.volume;
      const height = Math.max(1, (volume / maxVolume) * (volumeHeight - 14));
      const alert = !bar.isClosed;
      const color = alert ? "var(--color-alert)" : bar.direction === 1 ? "var(--color-up)" : "var(--color-down)";
      const element = createSvg("rect", {
        x: view.xForIndex(index) - bodyWidth / 2,
        y: volumeHeight - 12 - height,
        width: bodyWidth,
        height,
        "data-structure-kind": "volume",
        "data-bar-index": index,
        "data-open-time": bar.openTime,
        "data-direction": bar.direction,
        "data-state": alert ? "alert" : "confirmed",
      });
      paint(element, { fill: color, "fill-opacity": "0.55" });
      const payload = payloadFor("candle", {
        open_time: bar.openTime,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
        is_closed: bar.isClosed,
        source_ids: [],
      }, view, alert ? "alert" : "confirmed");
      element.classList.add("cpt-chart-hit");
      element.addEventListener("click", () => selectStructure(element, payload));
      group.appendChild(element);
    });
    group.appendChild(
      createSvg("text", { x: 6, y: volumeHeight - 4 }, `成交量 ${formatVolume(maxVolume)} 峰值`),
    );
    volumeNode.dataset.rendered = "true";
    volumeNode.appendChild(svg);
  }

  // 后端预计算 MACD（snapshot.indicators.macd）优先。与后端 macd_series 同源，
  // 避免前端重算的信号线 EMA 种子与后端口径不一致（面板标着 12/26/9 却和后端对不上）。
  // 按 open_time 建索引：view.candles 可能是缩放窗口（甚至回放前缀），
  // 而 indicators.macd 始终是全量序列，用下标对齐会错位。

  function backendMacdSeries(view) {
    const snapshot = view && view.snapshot;
    const indicators = isObject(snapshot) && isObject(snapshot.indicators) ? snapshot.indicators : null;
    const entries = indicators ? asArray(indicators.macd) : [];
    if (!entries.length) return null;
    const byOpenTime = new Map();
    entries.forEach((entry) => {
      if (!isObject(entry)) return;
      const openTime = num(entry.open_time);
      if (openTime !== null) byOpenTime.set(openTime, entry);
    });
    if (!byOpenTime.size) return null;
    const dif = [];
    const dea = [];
    const histogram = [];
    let matched = 0;
    view.candles.forEach((bar) => {
      const entry = byOpenTime.get(bar.openTime);
      if (!entry) {
        dif.push(null);
        dea.push(null);
        histogram.push(null);
        return;
      }
      matched += 1;
      dif.push(num(entry.macd));
      dea.push(num(entry.signal));
      histogram.push(num(entry.histogram));
    });
    // 一根都对不上说明这份 indicators 与当前 K 线不同源，宁可回退重算也不要画错。
    if (!matched) return null;
    return { dif, dea, histogram };
  }


  function drawMacd(macdNode, view, macdHeight) {
    const closes = view.candles.map((bar) => bar.close);
    // 后端预计算优先；缺失（老快照 / demo fixture）时回退到前端重算，保证不空图。
    const series = backendMacdSeries(view) || computeMacd(closes);
    if (!series) {
      appendNote(macdNode, "K 线不足或无 close 字段，跳过 MACD");
      macdNode.dataset.rendered = "false";
      return;
    }
    const { dif, dea, histogram } = series;
    const flatHist = histogram.map((value) => (value === null ? 0 : value));
    const flatDif = dif.map((value) => (value === null ? 0 : value));
    const flatDea = dea.map((value) => (value === null ? 0 : value));
    const maxAbs = Math.max(
      1e-9,
      ...flatHist.map((value) => Math.abs(value)),
      ...flatDif.map((value) => Math.abs(value)),
      ...flatDea.map((value) => Math.abs(value)),
    );
    const width = view.geom.width;
    const svg = createSvg("svg", {
      class: "cpt-chart-svg",
      viewBox: `0 0 ${width} ${macdHeight}`,
      preserveAspectRatio: "none",
      role: "presentation",
    });
    const group = createSvg("g", {});
    svg.appendChild(group);
    const zero = macdHeight / 2;
    const bodyWidth = Math.max(1, Math.min(view.geom.slot * 0.62, 16));
    histogram.forEach((value, index) => {
      if (value === null) return;
      const barHeight = (value / maxAbs) * (macdHeight * 0.4);
      const y = value >= 0 ? zero - barHeight : zero;
      const element = createSvg("rect", {
        x: view.xForIndex(index) - bodyWidth / 2,
        y,
        width: bodyWidth,
        height: Math.max(1, Math.abs(barHeight)),
        "data-structure-kind": "macd",
        "data-bar-index": index,
        "data-open-time": view.candles[index].openTime,
      });
      paint(element, {
        fill: value >= 0 ? "var(--color-up)" : "var(--color-down)",
        "fill-opacity": "0.5",
      });
      group.appendChild(element);
    });
    const lineFor = (values, color, dasharray) => {
      let previous = null;
      values.forEach((value, index) => {
        if (value === null) return;
        const x = view.xForIndex(index);
        const y = zero - (value / maxAbs) * (macdHeight * 0.4);
        if (previous) {
          const element = createSvg("line", {
            x1: previous.x,
            y1: previous.y,
            x2: x,
            y2: y,
            "data-structure-kind": "macd",
            "data-bar-index": index,
          });
          paint(element, { stroke: color, "stroke-width": "1.5", "stroke-dasharray": dasharray || "" });
          group.appendChild(element);
        }
        previous = { x, y };
      });
    };
    lineFor(dif, "var(--color-accent)", "");
    lineFor(dea, "var(--color-confirmed)", "3 2");
    group.appendChild(createSvg("line", {
      x1: view.geom.left,
      x2: view.geom.left + view.geom.plotWidth,
      y1: zero,
      y2: zero,
    }));
    group.appendChild(
      paint(createSvg("text", { x: 6, y: 12 }, "MACD 12/26/9"), { fill: "var(--color-text-dim)" }),
    );
    macdNode.dataset.rendered = "true";
    macdNode.appendChild(svg);
  }


  function computeMacd(closes) {
    if (!Array.isArray(closes) || closes.length < 26) return null;
    const ema = (period) => {
      const k = 2 / (period + 1);
      const series = [];
      closes.forEach((close, index) => {
        if (close === null || close === undefined) {
          series.push(null);
          return;
        }
        if (index === 0) {
          series.push(close);
        } else {
          const previous = series[index - 1];
          series.push(previous === null ? close : close * k + previous * (1 - k));
        }
      });
      return series;
    };
    const fast = ema(12);
    const slow = ema(26);
    const dif = closes.map((_, index) => {
      if (fast[index] === null || slow[index] === null) return null;
      return fast[index] - slow[index];
    });
    const k9 = 2 / (9 + 1);
    const dea = [];
    dif.forEach((value, index) => {
      if (value === null) {
        dea.push(null);
        return;
      }
      if (index === 0) {
        dea.push(value);
      } else {
        const previous = dea[index - 1];
        dea.push(previous === null ? value : value * k9 + previous * (1 - k9));
      }
    });
    const histogram = dif.map((value, index) => {
      if (value === null || dea[index] === null) return null;
      return (value - dea[index]) * 2;
    });
    return { dif, dea, histogram };
  }


  function installZoomControls() {
    const buttons = root.querySelectorAll("[data-zoom-action]");
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const action = button.dataset.zoomAction;
        if (action === "in") zoomBy(1);
        else if (action === "out") zoomBy(-1);
        else if (action === "reset") resetZoom();
        drawChart();
      });
    });
    const canvas = q("[data-testid=chart-canvas-region]");
    if (canvas) {
      canvas.addEventListener("wheel", (event) => {
        if (!event.ctrlKey && !event.metaKey) return;
        event.preventDefault();
        zoomBy(event.deltaY < 0 ? 1 : -1);
        drawChart();
      }, { passive: false });
      canvas.addEventListener("dblclick", () => {
        zoomBy(1);
        drawChart();
      });
    }
  }

  /**
   * 缩放按窗口长度操作，右边缘锚定最新一根：
   * 放大 → 长度减半（下限 30）；缩小 → 长度翻倍（上限 = 总根数）。
   * 缩到全量时 visibleWindow 记为 [0, 总数]（覆盖全部）而非 null，
   * 这样 600 根数据缩到底也不会退回 1px 发丝线的默认视图。
   * state.zoomLevel 只是派生值，供展示使用。
   */

  function zoomBy(direction) {
    const candles = state.snapshot ? normalizeCandles(state.snapshot.candles) : [];
    const total = candles.length;
    if (!total) return;
    const current = applyZoomWindow(candles).length;
    const next = direction > 0
      ? Math.max(30, Math.round(current / 2))
      : Math.min(total, Math.round(current * 2));
    const end = total;
    state.visibleWindow = [Math.max(0, end - next), end];
    state.zoomLevel = Math.max(0, Math.round(total / next) - 1);
  }


  function resetZoom() {
    state.zoomLevel = 0;
    state.visibleWindow = null;
  }


  function drawAxis(axisNode, view, axisHeight) {
    const svg = createSvg("svg", {
      class: "cpt-chart-svg",
      viewBox: `0 0 ${view.geom.width} ${axisHeight}`,
      preserveAspectRatio: "none",
      role: "presentation",
    });
    const group = createSvg("g", { class: "cpt-chart-axis" });
    svg.appendChild(group);
    const candles = view.candles;
    const step = Math.max(1, Math.ceil(candles.length / 6));

    // 级别：优先用相邻 bar 间隔推断（不依赖 runtime.interval_ms 是否存在）
    const intervalMs =
      candles.length > 1 ? Math.abs(candles[1].openTime - candles[0].openTime) : 0;
    const daily = intervalMs >= DAY_MS;
    // 窗口跨年时必须带年份，否则 12-31 与次年 01-01 无法区分
    const firstDate = candles.length ? new Date(candles[0].openTime) : null;
    const lastDate = candles.length
      ? new Date(candles[candles.length - 1].openTime)
      : null;
    const spansYear = Boolean(
      firstDate && lastDate && firstDate.getUTCFullYear() !== lastDate.getUTCFullYear(),
    );

    let previousDayKey = null;
    let labelIndex = 0;
    for (let index = step - 1; index < candles.length; index += step) {
      const openTime = candles[index].openTime;
      const date = new Date(openTime);
      const dayKey = `${date.getUTCFullYear()}-${date.getUTCMonth()}-${date.getUTCDate()}`;
      const dayChanged = dayKey !== previousDayKey;
      previousDayKey = dayKey;
      // 日线以上只给日期；盘中在跨日的那根给日期，其余只给时间（金融图惯例）
      const showDate = daily || dayChanged || labelIndex === 0;
      const label = formatAxisTime(openTime, {
        showDate,
        showTime: !daily,
        showYear: spansYear,
      });
      group.appendChild(
        createSvg("text", {
          x: view.xForIndex(index),
          y: axisHeight - 9,
          "text-anchor": "middle",
          "data-bar-index": index,
          "data-open-time": openTime,
        }, label),
      );
      labelIndex += 1;
    }
    // 全站时间为 UTC；在轴上标出来，避免被读成本地时间
    axisNode.dataset.timezone = "UTC";
    axisNode.dataset.rendered = "true";
    axisNode.appendChild(svg);
  }


  function scheduleDraw() {
    if (state.drawPending) return;
    state.drawPending = true;
    const run = () => {
      state.drawPending = false;
      drawChart();
    };
    if (typeof window.requestAnimationFrame === "function") window.requestAnimationFrame(run);
    else window.setTimeout(run, 16);
  }

  /* ------------------------------------------------------------ 生命周期 */


  function installCrosshair() {
    const canvas = q("[data-testid=chart-canvas-region]");
    if (!canvas) return;
    /* 宿主必须是 canvas 之外的稳定容器：drawChart() 会 clearRegion(canvas) 清空 canvas 子节点。 */
    const host = q("[data-testid=chart-shell]") || canvas.parentElement;
    if (!host || host === canvas) return;
    const tooltip = document.createElement("div");
    tooltip.className = "cpt-crosshair-tooltip";
    tooltip.hidden = true;
    host.appendChild(tooltip);
    canvas.addEventListener("mousemove", (event) => {
      /* 索引换算必须与 drawChart() 渲染的窗口一致：画的是 applyZoomWindow() 之后的那一段。 */
      const all = normalizeCandles(state.snapshot && state.snapshot.candles);
      const shown = applyZoomWindow(all);
      if (!shown.length) return;
      const offset = all.indexOf(shown[0]);
      if (offset < 0) return;
      const rect = canvas.getBoundingClientRect();
      const shellRect = host.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
      const candle = all[offset + Math.min(shown.length - 1, Math.floor(ratio * shown.length))];
      tooltip.textContent = `${formatDateTime(candle.openTime)} UTC  O ${formatPrice(candle.open)} H ${formatPrice(candle.high)} L ${formatPrice(candle.low)} C ${formatPrice(candle.close)} V ${formatVolume(candle.volume || 0)}`;
      tooltip.style.left = `${Math.max(4, event.clientX - shellRect.left + 8)}px`;
      tooltip.style.top = `${Math.max(4, event.clientY - shellRect.top + 8)}px`;
      tooltip.hidden = false;
    });
    canvas.addEventListener("mouseleave", () => { tooltip.hidden = true; });
  }


  function buildCanvasView() {
    const canvas = q("[data-testid=chart-canvas-region]");
    const volumeNode = q("[data-testid=chart-volume-region]");
    const macdNode = q("[data-testid=chart-macd-region]");
    const axisNode = q("[data-testid=chart-time-axis]");
    if (!canvas || !volumeNode || !axisNode) return null;

    const snapshot = state.snapshot;
    const rawCandles = normalizeCandles(snapshot && snapshot.candles);
    const candles = applyZoomWindow(rawCandles);
    const rawOverlays = normalizeOverlays(snapshot && snapshot.overlays);
    // 可视窗口过滤（R16-5 新增）：只保留与窗口相交的结构。
    // 改这个是因为旧行为把窗口外的笔/分型**钳到图边缘**（timeIndexOf 会把任意
    // 时间戳夹到 0 或 length-1），既画出假线段，又让"四个画布画同一批元素"
    // 无从定义。中枢本来就有这个过滤（overlapsWindow），现在笔/分型/走势类型
    // 与它统一口径，四个画布拿到的就是同一份窗口内结构。
    const windowStart = candles.length ? candles[0].openTime : 0;
    const windowEnd = candles.length ? candles[candles.length - 1].openTime : 0;
    const inWindow = (item) => {
      const start = num(item.start_time);
      const end = num(item.end_time);
      return start !== null && end !== null && end >= windowStart && start <= windowEnd;
    };
    const overlays = {
      fractals: rawOverlays.fractals.filter(inWindow),
      bis: rawOverlays.bis.filter(inWindow),
      zhongshus: rawOverlays.zhongshus.filter(inWindow),
      trend_types: rawOverlays.trend_types.filter(inWindow),
    };
    const domain = priceDomain(candles, overlays);
    const rect = canvas.getBoundingClientRect();
    const width = Math.round(rect.width);
    const height = Math.round(rect.height);
    const base = {
      canvas,
      volumeNode,
      macdNode,
      axisNode,
      snapshot,
      candles,
      overlays,
      domain,
      width,
      height,
      ready: Boolean(candles.length && domain && width >= 40 && height >= 40),
    };
    if (!base.ready) return base;

    const geom = layoutPlot(width, height, candles.length);
    return Object.assign(base, {
      geom,
      lastOpenTime: candles[candles.length - 1].openTime,
      windowStart: candles[0].openTime,
      windowEnd: candles[candles.length - 1].openTime,
      indexForTime: timeIndexOf(candles),
      xForIndex: (index) => geom.left + (index + 0.5) * geom.slot,
      yForPrice: (price) =>
        geom.top + ((domain.max - price) / (domain.max - domain.min)) * geom.plotHeight,
    });
  }

  // 空数据/未布局时的统一占位（四个画布共用，保证降级行为一致）。

  function drawChartA(view) {
    if (!view) return null;
    const { canvas, volumeNode, macdNode, axisNode } = view;
    if (!view.ready) {
      renderCanvasPlaceholder(view);
      return { canvas: "A", candles: 0, fractals: 0, bis: 0, zhongshus: 0, trendTypes: 0 };
    }

    clearRegion(canvas);
    clearRegion(volumeNode);
    if (macdNode) clearRegion(macdNode);
    clearRegion(axisNode);
    canvas.dataset.rendered = "true";
    canvas.setAttribute("role", "group");
    canvas.setAttribute("aria-label", "K 线与缠论结构叠加图：分型、笔、中枢、走势类型可点击查看结构详情");

    const svg = createSvg("svg", {
      class: "cpt-chart-svg",
      viewBox: `0 0 ${view.width} ${view.height}`,
      preserveAspectRatio: "none",
      role: "presentation",
    });
    canvas.appendChild(svg);

    const gridGroup = createSvg("g", { class: "cpt-chart-grid" });
    const trendGroup = createSvg("g", { class: "cpt-chart-trends" });
    const zhongshuGroup = createSvg("g", { class: "cpt-chart-zhongshus" });
    const candleGroup = createSvg("g", { class: "cpt-chart-candles" });
    const biGroup = createSvg("g", { class: "cpt-chart-bis" });
    const fractalGroup = createSvg("g", { class: "cpt-chart-fractals" });
    const signalGroup = createSvg("g", { class: "cpt-chart-signals" });
    const overlayGroup = createSvg("g", { class: "cpt-chart-overlays" });
    [
      gridGroup,
      trendGroup,
      zhongshuGroup,
      candleGroup,
      biGroup,
      fractalGroup,
      signalGroup,
      overlayGroup,
    ].forEach((group) => svg.appendChild(group));

    drawPriceGrid(gridGroup, view);
    drawTrendBackgrounds(trendGroup, view);
    drawZhongshus(zhongshuGroup, view);
    drawCandles(candleGroup, view);
    drawBis(biGroup, view);
    drawFractals(fractalGroup, view);
    drawSignal(signalGroup, view);
    drawLastPrice(overlayGroup, view);

    const volumeRect = volumeNode.getBoundingClientRect();
    const axisRect = axisNode.getBoundingClientRect();
    drawVolume(volumeNode, view, Math.max(48, Math.round(volumeRect.height) || 72));
    if (macdNode) drawMacd(macdNode, view, Math.max(60, Math.round(macdNode.getBoundingClientRect().height) || 96));
    drawAxis(axisNode, view, Math.max(20, Math.round(axisRect.height) || 28));
    updateZoomLabel();

    // 重绘会替换全部节点，按 kind + source_ids 找回当前选中元素并重新标记。
    state.selectedNode = null;
    if (state.selection) {
      const selection = state.selection;
      const selector = selection.kind === "candle"
        ? `[data-structure-kind=candle][data-bar-index="${selection.barIndex}"]`
        : `[data-structure-kind=${selection.kind}][data-source-ids="${selection.sourceIds.join(" ")}"]`;
      const node = canvas.querySelector(selector);
      if (node) {
        node.setAttribute("data-selected", "true");
        state.selectedNode = node;
      }
    }
    return canvasACounts(view);
  }


  function drawChart() {
    const registry = canvasRegistry();
    const view = buildCanvasView();
    if (!view) return null;
    // 未就绪（无 K 线 / 容器还没布局）时**不交给画布模块**：此时 view 里没有
    // geom / windowStart / xForIndex，画布 B/C/D 拿去会算出 undefined 的请求参数
    // （首轮审计实测：画布 D 对 /api/canvas/wbt 发了 start_ms=undefined 的 400 请求）。
    if (!view.ready) {
      renderCanvasPlaceholder(view);
      const empty = {
        canvas: state.canvas,
        candles: 0,
        fractals: 0,
        bis: 0,
        zhongshus: 0,
        trendTypes: 0,
      };
      state.canvasStats = empty;
      view.canvas.dataset.canvas = state.canvas;
      view.canvas.dataset.canvasCounts = JSON.stringify(empty);
      root.dataset.canvas = state.canvas;
      return empty;
    }
    const module = (registry && registry.get(state.canvas)) || (registry && registry.get(CANVAS_FALLBACK));
    let stats = null;
    if (module) {
      try {
        stats = module.draw(view);
      } catch (error) {
        // 单个画布炸掉不能拖垮整页：记一条 warning，回落 A 画布。
        state.canvasError = `${state.canvas}: ${error && error.message ? error.message : error}`;
        stats = registry.get(CANVAS_FALLBACK) ? registry.get(CANVAS_FALLBACK).draw(view) : null;
      }
    }
    state.canvasStats = stats;
    const node = view.canvas;
    if (node) {
      node.dataset.canvas = state.canvas;
      if (stats) node.dataset.canvasCounts = JSON.stringify(stats);
      else delete node.dataset.canvasCounts;
    }
    root.dataset.canvas = state.canvas;
    if (state.canvasError) root.dataset.canvasError = state.canvasError;
    else delete root.dataset.canvasError;
    return stats;
  }


  function setCanvas(id) {
    const registry = canvasRegistry();
    const wanted = String(id || "").toUpperCase();
    if (!registry || !registry.has(wanted)) return false;
    if (wanted === state.canvas) return true;
    state.canvas = wanted;
    state.canvasError = null;
    // URL 写回（照抄模式切换的 history.replaceState 范式）
    try {
      const url = resolveUrl(window.location.href);
      url.searchParams.set("canvas", wanted);
      window.history.replaceState(null, "", url.toString());
    } catch (error) {
      void error;
    }
    renderCanvasSwitch();
    drawChart();
    root.dispatchEvent(new CustomEvent("cpt:canvas-changed", { detail: { canvas: wanted } }));
    return true;
  }


  function renderCanvasSwitch() {
    const container = q("[data-testid=canvas-switch]");
    if (!container) return;
    const registry = canvasRegistry();
    const options = registry ? registry.ids() : ["A"];
    if (container.dataset.builtFor !== options.join(",")) {
      container.replaceChildren();
      options.forEach((id) => {
        const module = registry.get(id);
        const button = createHtml("button", {
          type: "button",
          "data-canvas-option": id,
          "data-testid": `canvas-option-${id.toLowerCase()}`,
        });
        button.textContent = `${id} · ${(module && module.label) || id}`;
        button.addEventListener("click", () => setCanvas(id));
        container.appendChild(button);
      });
      container.dataset.builtFor = options.join(",");
    }
    Array.from(container.querySelectorAll("[data-canvas-option]")).forEach((button) => {
      const active = button.dataset.canvasOption === state.canvas;
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.dataset.state = active ? "active" : "idle";
    });
  }


  function installCanvasSwitch() {
    const params = new URLSearchParams(window.location.search);
    const requested = (params.get("canvas") || CANVAS_FALLBACK).toUpperCase();
    const registry = canvasRegistry();
    state.canvas = registry && registry.has(requested) ? requested : CANVAS_FALLBACK;
    state.canvasError = null;
    renderCanvasSwitch();
    root.dataset.canvas = state.canvas;
  }

  /* ---- Phase B1/B2：数据源与运行状态面板 ---- */

  /**
   * 从 snapshot 地址推出看板 API 基址（与 ``market_a_share.js`` 的 ``aShareBase`` 同源推导）。
   *
   * **不能写死 ``/cpt/api/...``**：看板可挂在任意 nginx 前缀下（本地 ``/cpt/``、独立
   * 服务在根路径），写死会在换前缀时静默 404。
   */
// >>>FUNCS
})();
