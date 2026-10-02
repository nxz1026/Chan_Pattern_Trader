/*
 * 画布 D —— wbt 报告视图（R16-5）。
 *
 * 与 B/C 的本质差别：**HTML 由服务端生成**。服务端调
 * `wbt.report.HtmlReportBuilder`（`add_header` / `add_metrics` / `add_chart_tab`
 * / `add_table` / `add_footer` / `render`）产出完整文档，CPT 侧丢掉 `<head>`
 * 的 CDN 外链、只取 `<body>` 内容，再由本文件注入 **sandbox iframe**。
 *
 * 为什么是 iframe：wbt 的样式表 + bootstrap 会重排全局（`.container` / `.table`
 * / `.nav-tabs`），直接注入主页面会打乱现有 CPT 看板（R12 刚验过 375px 移动端
 * 触摸目标与水平溢出）。
 *
 * ## R28-11：改用 `srcdoc` + 不透明 origin（审计 M3 的彻底解法）
 *
 * 此前是 `sandbox="allow-same-origin allow-scripts"` + 父页直接操作
 * `contentDocument`。那个组合是**已知可逃逸**的：frame 内的脚本可以
 * `window.frameElement.removeAttribute("sandbox")` 再重载，从而拿到父页面的
 * 同源权限 —— 逃逸原语一直存在，只是当时没有攻击者可控的输入喂给它。
 *
 * 现在 `sandbox="allow-scripts"`（**去掉** `allow-same-origin`），iframe 拿到
 * **不透明 origin**：即使内容里跑进恶意脚本，它也**够不到父页面的 DOM / cookie
 * / localStorage**。DOM 组装从父页搬进字符串侧（`srcdoc` 一次成文），父页不再
 * 触碰 `contentDocument`。
 *
 * 代价评估（2026-10-02 勘察，结论是「几乎免费」）：
 *
 * - **父页读不到 `contentDocument`** —— 但全仓**没有任何代码读它**：
 *   `grep -r contentDocument` 只命中本文件。四画布计数一致性走的是父节点上的
 *   `data-canvas-counts`，数据来自服务端 JSON 的 `counts` 字段，**不经过 iframe
 *   DOM**。所以 R28-5 台账里「去掉 allow-same-origin 会让计数断言全废」那句
 *   当时写错了 —— 那是**假设**审计读了 iframe DOM，实际没有。已更正。
 * - **plotly 仍要跑** —— `allow-scripts` 保留着，且外链 `<script src>` 在
 *   srcdoc 文档里按文档顺序执行：plotly 先加载，再执行片段里的 `newPlot`。
 * - **相对 URL 仍能解析** —— srcdoc 的 base URL 取自父文档，`<link>` /
 *   `<script src>` 用相对路径即可命中本地 vendor。
 *
 * 一处诚实的说明：srcdoc 是**字符串**拼装，而 `body_html` 会被原样嵌进
 * `<body>`。它取自 wbt `render()` 的正文，标签是配平的；但万一上游产出里出现
 * 落单的 `</body>`，HTML 解析器会提前收尾。这不是安全问题（内容仍受 sandbox
 * 约束），但值得知道。
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

  const LOCAL_CSS =
    "html,body{margin:0;padding:0;background:transparent;}" +
    "body{padding:8px;}" +
    ".cpt-d-note{font:13px/1.6 system-ui,sans-serif;color:#4a5568;padding:12px;}";

  /**
   * 拼一整份 iframe 文档。
   *
   * **plotly 必须排在片段内联脚本之前**：外链 `<script src>` 与内联
   * `<script>` 在同一文档里按文档顺序执行，所以把 plotly / bootstrap 放在
   * `<head>`、片段脚本放在 `</body>` 前就满足依赖。
   */
  function shell(base, bodyHtml, css, scripts) {
    const inline = (scripts || [])
      .map((source) => {
        const match = /<script\b[^>]*>([\s\S]*?)<\/script>/i.exec(source);
        return `<script>${match ? match[1] : ""}</script>`;
      })
      .join("");
    return (
      '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width, initial-scale=1">' +
      // bootstrap：wbt 模板用了 .container / .nav-tabs / .table / .bi 图标，
      // 它的 CDN 链接被服务端剥掉了，这里补本地副本（离线可用）。
      `<link rel="stylesheet" href="${base}bootstrap.min.css">` +
      `<link rel="stylesheet" href="${base}bootstrap-icons.css">` +
      `<style>${LOCAL_CSS}</style>` +
      (css ? `<style>${css}</style>` : "") +
      `<script src="${base}plotly-finance.min.js"></script>` +
      `<script src="${base}bootstrap.bundle.min.js"></script>` +
      "</head><body>" +
      (bodyHtml || "") +
      inline +
      "</body></html>"
    );
  }

  function note(text) {
    return `<p class="cpt-d-note">${text}</p>`;
  }

  /**
   * 建 iframe 并塞入文档。
   *
   * `sandbox` 刻意**不含** `allow-same-origin`：frame 因此拿到不透明 origin，
   * 内容里的脚本即使逃逸也够不到父页面。`allow-scripts` 必须留 —— 否则 plotly
   * 跑不起来、画布 D 直接废。
   *
   * 赋 `srcdoc` 会触发一次导航，浏览器自行处理属性转义，调用方不必担心
   * 正文里的引号。
   */
  function buildFrame(node, html) {
    const frame = document.createElement("iframe");
    frame.className = "cpt-canvas-d-frame";
    frame.setAttribute("data-testid", "canvas-d-frame");
    frame.setAttribute("title", "wbt 结构报告");
    frame.setAttribute("sandbox", "allow-scripts");
    node.appendChild(frame);
    frame.srcdoc = html;
    return frame;
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
    const base = vendorBase();
    const token = `${Date.now()}-${Math.random()}`;
    node.dataset.canvasToken = token;
    node.dataset.canvasReady = "false";

    const frame = buildFrame(node, shell(base, note("正在请求服务端 wbt 报告…"), "", []));

    // srcdoc 导航是**异步**的，所以「报告真的画出来了」只能听 load 事件。
    // 直接在 fetch 的 then 里置 true 会撒谎 —— 那时 iframe 里还是空壳。
    let awaitingReport = false;
    frame.addEventListener("load", () => {
      if (node.dataset.canvasToken !== token) return;
      if (awaitingReport) {
        awaitingReport = false;
        node.dataset.canvasReady = "true";
      }
    });

    const url = `${endpoint()}?start_ms=${view.windowStart}&end_ms=${view.windowEnd}${marketQuery()}`;
    window
      .fetch(url)
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
      .then((payload) => {
        if (node.dataset.canvasToken !== token) return; // 已被下一次重绘取代
        if (!payload.available) {
          node.dataset.canvasReady = "unavailable";
          frame.srcdoc = shell(base, note(`画布 D 不可用：${payload.reason || "unknown"}`), "", []);
          return;
        }
        const server = payload.counts || {};
        const mismatch = ["candles", "fractals", "bis", "zhongshus", "trendTypes"].filter(
          (key) => Number(server[key]) !== Number(counts[key]),
        );
        node.dataset.canvasCounts = JSON.stringify(Object.assign({}, counts, server, { canvas: "D" }));
        node.dataset.canvasSource = payload.source || "wbt";
        if (mismatch.length) {
          document.body.dataset.canvasError = `D:count_mismatch:${mismatch.join(",")}`;
        }
        awaitingReport = true;
        frame.srcdoc = shell(base, payload.body_html || "", payload.css || "", payload.scripts || []);
      })
      .catch((error) => {
        if (node.dataset.canvasToken !== token) return;
        node.dataset.canvasReady = "error";
        frame.srcdoc = shell(
          base,
          note(`画布 D 请求失败：${error && error.message ? error.message : error}`),
          "",
          [],
        );
      });

    // 注意：不能 Object.assign(counts, ...) —— 那会把 pending/library 写进 counts，
    // 后面覆盖 data-canvas-counts 时又被带出去（首轮审计里 D 的计数多了两个字段）。
    return Object.assign({}, counts, { pending: true, library: "wbt.report.HtmlReportBuilder" });
  }

  if (window.CPT_CANVASES) {
    window.CPT_CANVASES.register("D", {
      label: "D wbt 报告",
      note: "服务端 wbt HtmlReportBuilder 报告外壳 + plotly K 线（sandbox iframe 隔离样式与 origin）",
      draw: render,
    });
  }
})();
