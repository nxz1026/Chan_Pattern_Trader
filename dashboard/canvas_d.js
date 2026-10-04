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

  // ⚠️ R45：凭据消毒走**全站唯一实现** ``window.CPT_URL.safe``
  // （url_safety.js，必须先于本文件加载）。``window.CPTDashboard`` 是
  // dashboard.js 的入口，而它加载在**最后**（index.html），所以这里不能用它 ——
  // 那正是「重复实现」的路子：主副本改了、副本静默漂移。
  const safeUrl = (target) =>
    (window.CPT_URL && window.CPT_URL.safe ? window.CPT_URL.safe : (t) => t)(target);

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
   * iframe 侧的诊断回传。
   *
   * R28-11 把 iframe 换成不透明 origin 之后，父页**读不到里面**了 ——
   * 于是「图没画出来」这件事在父页上表现为**一片空白、零线索**。
   * 这正是本仓反复吃过的亏：优雅降级会掩盖功能缺失（画布 D 一直「不可用」
   * 两周没人发现、chromium 测试恒 skip）。
   *
   * 修法不是「想办法让父页看进去」（那等于把刚收掉的同源逃逸面又打开），
   * 而是让 iframe **主动报告**：`postMessage` 跨 origin 是允许的。
   *
   * 它同时回答了一个实际故障：2026-10-02 真机上「有数据但没画图」——
   * 表格和指标都出来了（那是静态 HTML），只有 plotly 图缺席。
   * 在能看见里面之前，没人知道是 plotly 没加载、是 newPlot 抛了、
   * 还是被 Basic Auth 挡了。现在这三种会给出三种不同的文案。
   */
  // ⚠️ 这里**不能**再给 window.__cptPhase 赋值 —— 它由 shell() 在本段之前
  // 设成 placeholder / report / unavailable。曾经这里多了一句
  // `window.__cptPhase=null;`（编辑时留下的残渣），把所有消息的 phase 抹成
  // null，父页的 `phase !== "report"` 过滤就把它们**全丢了** —— 表现为
  // 「诊断框一个字都没有」，看起来像 iframe 根本没上报。
  const DIAG_SCRIPT =
    "window.addEventListener('error',function(e){" +
    "parent.postMessage({__cptD:1,kind:'error',phase:window.__cptPhase," +
    "msg:String((e.error&&e.error.message)||e.message||e.type)},'*');});" +
    "window.addEventListener('unhandledrejection',function(e){" +
    "parent.postMessage({__cptD:1,kind:'reject',phase:window.__cptPhase," +
    "msg:String(e.reason&&e.reason.message||e.reason)},'*');});";

  /**
   * vendor `<script>` 的 onload / onerror —— **比 load 事件可靠得多**。
   *
   * ``load`` 要等**全部**子资源（含 1.17MB plotly）都完成才触发，而：
   * - headless 的 ``--virtual-time-budget`` 不会为嵌套 browsing context 的
   *   子资源等那么久，于是 load 永远不触发，诊断框什么都不显示；
   * - 真出问题时（plotly 被 401 挡住），load 同样不触发。
   *
   * 也就是说「等 load」这个设计**恰好在最需要它的时候失效**。改成给每个
   * ``<script>`` 挂 onload/onerror，谁成功、谁失败、HTTP 什么状态，一目了然，
   * 且不依赖任何聚合事件。
   */
  const vendorScript = (url, name) =>
    `<script src="${url}" onload="parent.postMessage({__cptD:1,kind:'vendor-ok',phase:window.__cptPhase,` +
    `name:'${name}'},'*')" onerror="parent.postMessage({__cptD:1,kind:'vendor-fail',phase:window.__cptPhase,` +
    `name:'${name}',msg:'加载失败（401？路径不对？）'},'*')"></script>`;

  /**
   * 拼一整份 iframe 文档。
   *
   * **plotly 必须排在片段内联脚本之前**：外链 `<script src>` 与内联
   * `<script>` 在同一文档里按文档顺序执行，所以把 plotly / bootstrap 放在
   * `<head>`、片段脚本放在 `</body>` 前就满足依赖。
   *
   * ``phase`` 必需：占位文档与正式报告**都会**触发一次 load/错误，不标 phase
   * 父页就分不清收到的是哪一次 —— 第一版就栽在这。
   */
  function shell(base, bodyHtml, css, scripts, phase) {
    const inline = (scripts || [])
      .map((source) => {
        const match = /<script\b[^>]*>([\s\S]*?)<\/script>/i.exec(source);
        return `<script>${match ? match[1] : ""}</script>`;
      })
      .join("");
    return (
      '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width, initial-scale=1">' +
      `<script>window.__cptPhase=${JSON.stringify(phase || "report")};` +
      DIAG_SCRIPT +
      "</script>" +
      // bootstrap：wbt 模板用了 .container / .nav-tabs / .table / .bi 图标，
      // 它的 CDN 链接被服务端剥掉了，这里补本地副本（离线可用）。
      `<link rel="stylesheet" href="${base}bootstrap.min.css" onerror="parent.postMessage({__cptD:1,kind:'vendor-fail',phase:window.__cptPhase,name:'bootstrap.min.css',msg:'CSS 加载失败'},'*')">` +
      `<link rel="stylesheet" href="${base}bootstrap-icons.css" onerror="parent.postMessage({__cptD:1,kind:'vendor-fail',phase:window.__cptPhase,name:'bootstrap-icons.css',msg:'CSS 加载失败'},'*')">` +
      `<style>${LOCAL_CSS}</style>` +
      (css ? `<style>${css}</style>` : "") +
      vendorScript(`${base}plotly-finance.min.js`, "plotly") +
      vendorScript(`${base}bootstrap.bundle.min.js`, "bootstrap.bundle") +
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

  /**
   * 收 iframe 的诊断回传，把失败原因显示在**父页**上。
   *
   * 为什么父页要知道：R28-11 之后父页读不到 iframe 内部（那正是我们要的
   * 安全属性），所以「图没画出来」如果不主动上报，就是一片空白 + 零线索。
   * 这条把它变成「空白 + 一句原因」。
   *
   * 只认带 `__cptD` 标记的消息，且不校验 origin —— 因为 srcdoc + sandbox 的
   * 文档 origin **就是不透明源**（`"null"`），`event.origin` 没有可校验的值。
   * 这里只读、不写，且消息内容只落到一个 data-* 属性上，不构成提权。
   */
  function reportDiag(node, message) {
    if (!message || message.__cptD !== 1) return;
    // 占位文档（"正在请求…"）的诊断没有意义 —— 它本来就不该有 plotly。
    // 只显示**报告阶段**的，否则会拿占位阶段的结论当报告的（第一版栽在这）。
    if (message.phase !== "report") return;
    const mark = message.kind === "vendor-ok" ? "OK"
      : message.kind === "vendor-fail" ? "★失败"
      : message.kind;
    const text = `${mark} ${message.name || ""}${message.msg ? " — " + message.msg : ""}`;
    node.dataset.canvasDiag = (node.dataset.canvasDiag ? node.dataset.canvasDiag + " | " : "") + text;
    let box = q("[data-testid=canvas-d-diag]");
    if (!box) {
      box = document.createElement("pre");
      box.className = "cpt-d-diag";
      box.dataset.testid = "canvas-d-diag";
      node.appendChild(box);
    }
    box.textContent = node.dataset.canvasDiag;
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
    // 重绘计数：用来判断「plotly 还没下载完画布就重绘了」这类时序问题。
    // 每次 draw 都新建 iframe + 重新拉 1.17MB vendor，这个数会涨得很快。
    node.dataset.canvasRenders = String(Number(node.dataset.canvasRenders || 0) + 1);

    const frame = buildFrame(
      node,
      shell(base, note("正在请求服务端 wbt 报告…"), "", [], "placeholder"),
    );

    // iframe 的诊断回传（跨 origin 允许，是不透明 origin 下唯一的可观测通道）
    //
    // ⚠️ **刻意不做 token 守卫**：token 守卫是为了不把「上一次重绘的 load 事件」
    // 当成本次的。但 vendor 脚本的 onload 往往在**下一次重绘之后**才到达（1.17MB
    // plotly 要下载几秒，而画布 30s 轮询 + 任何缩放/重绘都会换 token）——
    // 加了守卫，诊断就永远收不到消息，表现为「诊断框一个字都没有」，
    // 看起来像 iframe 根本没上报。诊断信息晚到一点没关系，宁可旧一点也要有。
    const onDiag = (event) => reportDiag(node, event.data);
    window.addEventListener("message", onDiag);

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
      .fetch(safeUrl(url))
      .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`))))
      .then((payload) => {
        if (node.dataset.canvasToken !== token) return; // 已被下一次重绘取代
        if (!payload.available) {
          node.dataset.canvasReady = "unavailable";
        frame.srcdoc = shell(
          base,
          note(`画布 D 不可用：${payload.reason || "unknown"}`),
          "",
          [],
          "unavailable",
        );
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
        frame.srcdoc = shell(
          base,
          payload.body_html || "",
          payload.css || "",
          payload.scripts || [],
          "report",
        );
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
