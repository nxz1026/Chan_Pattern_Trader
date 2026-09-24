/*
 * 画布 D —— wbt 报告视图（R16-5）。
 *
 * 与 B/C 的本质差别：**HTML 由服务端生成**。服务端调
 * `wbt.report.HtmlReportBuilder`（`add_header` / `add_metrics` / `add_chart_tab`
 * / `add_table` / `add_footer` / `render`）产出完整文档，CPT 侧丢掉 `<head>`
 * 的 CDN 外链、只取 `<body>` 内容，再由本文件注入**同源 iframe**。
 *
 * 为什么是 iframe：wbt 的样式表 + bootstrap 会重排全局（`.container` / `.table`
 * / `.nav-tabs`），直接注入主页面会打乱现有 CPT 看板（R12 刚验过 375px 移动端
 * 触摸目标与水平溢出）。iframe 同源 ⇒ 父页面能读 contentDocument，审计照做。
 *
 * 计数来源：服务端按**同一可视窗口**过滤后返回 `counts`，客户端拿它覆盖
 * `data-canvas-counts`；客户端先给出的本地计数若与服务端不一致，会记到
 * `data-canvas-error` 上 —— 这正是"四画布一致"断言的护栏。
 */
(function () {
  "use strict";

  // 服务端片段地址由 `data-snapshot-url` 推出，保证 file:// 与 /cpt/ 两种部署都对。
  function endpoint() {
    const raw = (document.body && document.body.dataset.snapshotUrl) || "/cpt/api/dashboard/snapshot";
    return String(raw).replace(/\/dashboard\/snapshot.*$/, "/canvas/wbt");
  }

  // A 股模式必须把 code 透给服务端：本路由是服务端取数的，不带 code 就会拿
  // 加密快照 —— 结果是 A/B/C 画 A 股、D 画 BTCUSDT（R17-3 审计实测 579 vs 123）。
  function marketQuery() {
    const data = (document.body && document.body.dataset) || {};
    if (data.market !== "a_share") return "";
    const code = data.aShareCode || "";
    return code ? `&code=${encodeURIComponent(code)}` : "";
  }

  function vendorBase() {
    const link = document.querySelector('link[rel="stylesheet"][href*="dashboard.css"]');
    const href = link ? link.getAttribute("href") : "./dashboard.css";
    return String(href).replace(/dashboard\.css.*$/, "vendor/");
  }

  function buildFrame(node) {
    const frame = document.createElement("iframe");
    frame.className = "cpt-canvas-d-frame";
    frame.setAttribute("data-testid", "canvas-d-frame");
    frame.setAttribute("title", "wbt 结构报告");
    frame.setAttribute("sandbox", "allow-same-origin allow-scripts");
    node.appendChild(frame);
    const doc = frame.contentDocument;
    doc.open();
    doc.write(
      '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
        '<meta name="viewport" content="width=device-width, initial-scale=1">' +
        "</head><body></body></html>",
    );
    doc.close();
    return frame;
  }

  function injectAssets(doc, base) {
    // bootstrap：wbt 模板用了 .container / .nav-tabs / .table / .bi 图标，
    // 它的 CDN 链接被服务端剥掉了，这里补本地副本（离线可用）。
    const bootstrapCss = doc.createElement("link");
    bootstrapCss.rel = "stylesheet";
    bootstrapCss.href = `${base}bootstrap.min.css`;
    doc.head.appendChild(bootstrapCss);

    const iconsCss = doc.createElement("link");
    iconsCss.rel = "stylesheet";
    iconsCss.href = `${base}bootstrap-icons.css`;
    doc.head.appendChild(iconsCss);

    const localCss = doc.createElement("style");
    localCss.textContent =
      "html,body{margin:0;padding:0;background:transparent;}" +
      "body{padding:8px;}" +
      ".cpt-d-note{font:13px/1.6 system-ui,sans-serif;color:#4a5568;padding:12px;}";
    doc.head.appendChild(localCss);

    // plotly 必须**先于**片段里的 Plotly.newPlot 内联脚本执行。
    const plotly = doc.createElement("script");
    plotly.src = `${base}plotly-finance.min.js`;
    doc.head.appendChild(plotly);

    const bootstrapJs = doc.createElement("script");
    bootstrapJs.src = `${base}bootstrap.bundle.min.js`;
    doc.head.appendChild(bootstrapJs);
  }

  function runScripts(doc, scripts) {
    // innerHTML 注入不会执行 <script>，必须重建元素（含 plotly 的 newPlot 调用）。
    scripts.forEach((source) => {
      const script = doc.createElement("script");
      const match = /<script\b[^>]*>([\s\S]*?)<\/script>/i.exec(source);
      script.textContent = match ? match[1] : "";
      doc.body.appendChild(script);
    });
  }

  function localCounts(view) {
    return {
      canvas: "D",
      candles: view.candles.length,
      fractals: (view.overlays.fractals || []).length,
      bis: (view.overlays.bis || []).length,
      zhongshus: (view.overlays.zhongshus || []).length,
      trendTypes: (view.overlays.trend_types || []).length,
    };
  }

  function render(view) {
    const { clearRegion, appendNote } = window.CPTDashboard;
    const node = view.canvas;
    clearRegion(node);
    if (view.volumeNode) clearRegion(view.volumeNode);
    if (view.macdNode) clearRegion(view.macdNode);
    if (view.axisNode) clearRegion(view.axisNode);
    if (view.volumeNode) appendNote(view.volumeNode, "画布 D 的成交量不在 wbt 报告里（见画布 A）");
    if (view.macdNode) appendNote(view.macdNode, "画布 D 不渲染 MACD 副图（见画布 A）");
    if (view.axisNode) appendNote(view.axisNode, "时间轴由报告内 plotly 自带");

    const counts = localCounts(view);
    const frame = buildFrame(node);
    injectAssets(frame.contentDocument, vendorBase());
    const token = `${Date.now()}-${Math.random()}`;
    node.dataset.canvasToken = token;
    node.dataset.canvasReady = "false";

    const note = frame.contentDocument.createElement("p");
    note.className = "cpt-d-note";
    note.textContent = "正在请求服务端 wbt 报告…";
    frame.contentDocument.body.appendChild(note);

    const url = `${endpoint()}?start_ms=${view.windowStart}&end_ms=${view.windowEnd}${marketQuery()}`;
    window
      .fetch(url)
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
      .then((payload) => {
        if (node.dataset.canvasToken !== token) return; // 已被下一次重绘取代
        const doc = frame.contentDocument;
        if (!payload.available) {
          node.dataset.canvasReady = "unavailable";
          doc.body.replaceChildren();
          const reason = doc.createElement("p");
          reason.className = "cpt-d-note";
          reason.textContent = `画布 D 不可用：${payload.reason || "unknown"}`;
          doc.body.appendChild(reason);
          return;
        }
        const style = doc.createElement("style");
        style.textContent = payload.css || "";
        doc.head.appendChild(style);
        doc.body.innerHTML = payload.body_html || "";
        runScripts(doc, payload.scripts || []);
        const server = payload.counts || {};
        const mismatch = ["candles", "fractals", "bis", "zhongshus", "trendTypes"].filter(
          (key) => Number(server[key]) !== Number(counts[key]),
        );
        node.dataset.canvasCounts = JSON.stringify(Object.assign({}, counts, server, { canvas: "D" }));
        node.dataset.canvasSource = payload.source || "wbt";
        node.dataset.canvasReady = "true";
        if (mismatch.length) {
          document.body.dataset.canvasError = `D:count_mismatch:${mismatch.join(",")}`;
        }
      })
      .catch((error) => {
        if (node.dataset.canvasToken !== token) return;
        node.dataset.canvasReady = "error";
        const doc = frame.contentDocument;
        doc.body.replaceChildren();
        const reason = doc.createElement("p");
        reason.className = "cpt-d-note";
        reason.textContent = `画布 D 请求失败：${error && error.message ? error.message : error}`;
        doc.body.appendChild(reason);
      });

    // 注意：不能 Object.assign(counts, ...) —— 那会把 pending/library 写进 counts，
    // 后面覆盖 data-canvas-counts 时又被带出去（首轮审计里 D 的计数多了两个字段）。
    return Object.assign({}, counts, { pending: true, library: "wbt.report.HtmlReportBuilder" });
  }

  if (window.CPT_CANVASES) {
    window.CPT_CANVASES.register("D", {
      label: "D wbt 报告",
      note: "服务端 wbt HtmlReportBuilder 报告外壳 + plotly K 线（同源 iframe 隔离样式）",
      draw: render,
    });
  }
})();
