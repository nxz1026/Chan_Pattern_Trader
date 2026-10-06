// dash-core.js — CPT 看板 · 核心（由 dashboard/dashboard.js 按功能拆分，R45）
//
// 核心：共享工具 / state / 快照加载 / **导出 window.CPTDashboard** / boot。
// 
// ⚠️ **只有本文件有 boot() 与 window.CPTDashboard** —— 拆到别处会重复执行。
// **热路径**：每次 30s 轮询都走这里。
//
// ⚠️ 机械搬运，没有改任何一行逻辑。

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
  function statusLabel(status) {
    return STATUS_LABELS[status] || STATUS_LABELS.none;
  }


  function statusEducation(status) {
    return STATUS_EDUCATION[status] || STATUS_EDUCATION.none;
  }


  function divergenceLabel(divergence) {
    return DIVERGENCE_LABELS[divergence] || "—";
  }


  function signalTypeLabel(signalType) {
    return SIGNAL_TYPE_LABELS[signalType] || "信号";
  }

  /*
   * 本阶段不修改 dashboard.css，因此把三条最小运行时样式随脚本注入：
   * 占位纹理让位给真实图形，图形层绝对定位铺满绘制区。
   */
  const RUNTIME_CSS = [
    '.chart-canvas[data-rendered="true"]::before { display: none; }',
    '.chart-volume[data-rendered="true"] { background-image: none; }',
    // K 线区已是定位容器；成交量/时间轴区需补 position: relative，
    // 否则绝对定位的 <svg> 会跑到最近的定位祖先上，跨区域错位。
    ".chart-volume, .chart-time-axis { position: relative; overflow: hidden; }",
    ".cpt-chart-svg { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }",
    ".cpt-chart-svg text { font-family: var(--font-mono); font-size: 10px; fill: var(--color-text-faint); }",
    ".cpt-chart-hit { cursor: pointer; }",
    ".cpt-chart-hit:hover { filter: brightness(1.3); }",
    '.cpt-chart-hit[data-selected="true"] { filter: brightness(1.5); }',
    ".cpt-chart-hit:focus-visible { outline: 1px solid var(--color-accent); outline-offset: 1px; }",
    ".cpt-selection { margin-top: var(--space-3); padding-top: var(--space-3);",
    "  border-top: var(--border-width) solid var(--color-border); }",
    ".cpt-selection-status { color: var(--color-text-dim); font-size: var(--font-size-xs); }",
    '.cpt-selection-status[data-state="alert"] { color: var(--color-alert); }',
    '.cpt-selection-status[data-state="confirmed"] { color: var(--color-confirmed); }',
  ].join("\n");

  /*
   * 离线 demo fixture：由本仓库 D1 snapshot service（build_dashboard_snapshot）
   * 用 domain 管线在固定输入上离线生成的结果，逐字节固定，前端只读不改写。
   * 末根 K 线 is_closed=false，用于演示未收盘 alert 路径。
   */
  const DEMO_FIXTURE_JSON = `
{
  "schema_version": "dashboard.v2",
  "market": {"symbol": "DEMOUSDT", "interval_ms": 300000, "last_price": 60345.0, "first_open_time": 1756000000000, "last_open_time": 1756008700000, "bar_count": 30},
  "candles": [
    {"open_time": 1756000000000, "open": 60000.0, "high": 60118.0, "low": 59992.0, "close": 60110.0, "volume": 67.5, "close_time": 1756000299999, "quote_volume": 2400000.0, "trade_count": 120, "taker_buy_base_volume": 18.0, "taker_buy_quote_volume": 1100000.0, "is_closed": true, "direction": 1},
    {"open_time": 1756000300000, "open": 60110.0, "high": 60232.0, "low": 60098.0, "close": 60220.0, "volume": 80.5, "close_time": 1756000599999, "quote_volume": 2401375.0, "trade_count": 127, "taker_buy_base_volume": 23.0, "taker_buy_quote_volume": 1100640.0, "is_closed": true, "direction": 1},
    {"open_time": 1756000600000, "open": 60220.0, "high": 60346.0, "low": 60204.0, "close": 60330.0, "volume": 70.5, "close_time": 1756000899999, "quote_volume": 2402750.0, "trade_count": 134, "taker_buy_base_volume": 28.0, "taker_buy_quote_volume": 1101280.0, "is_closed": true, "direction": 1},
    {"open_time": 1756000900000, "open": 60330.0, "high": 60448.0, "low": 60322.0, "close": 60440.0, "volume": 83.5, "close_time": 1756001199999, "quote_volume": 2404125.0, "trade_count": 141, "taker_buy_base_volume": 22.0, "taker_buy_quote_volume": 1101920.0, "is_closed": true, "direction": 1},
    {"open_time": 1756001200000, "open": 60440.0, "high": 60562.0, "low": 60428.0, "close": 60550.0, "volume": 73.5, "close_time": 1756001499999, "quote_volume": 2405500.0, "trade_count": 148, "taker_buy_base_volume": 27.0, "taker_buy_quote_volume": 1102560.0, "is_closed": true, "direction": 1},
    {"open_time": 1756001500000, "open": 60550.0, "high": 60566.0, "low": 60444.0, "close": 60460.0, "volume": 81.5, "close_time": 1756001799999, "quote_volume": 2406875.0, "trade_count": 155, "taker_buy_base_volume": 21.0, "taker_buy_quote_volume": 1103200.0, "is_closed": true, "direction": -1},
    {"open_time": 1756001800000, "open": 60460.0, "high": 60468.0, "low": 60362.0, "close": 60370.0, "volume": 71.5, "close_time": 1756002099999, "quote_volume": 2408250.0, "trade_count": 122, "taker_buy_base_volume": 26.0, "taker_buy_quote_volume": 1103840.0, "is_closed": true, "direction": -1},
    {"open_time": 1756002100000, "open": 60370.0, "high": 60382.0, "low": 60268.0, "close": 60280.0, "volume": 84.5, "close_time": 1756002399999, "quote_volume": 2409625.0, "trade_count": 129, "taker_buy_base_volume": 20.0, "taker_buy_quote_volume": 1104480.0, "is_closed": true, "direction": -1},
    {"open_time": 1756002400000, "open": 60280.0, "high": 60296.0, "low": 60174.0, "close": 60190.0, "volume": 74.5, "close_time": 1756002699999, "quote_volume": 2411000.0, "trade_count": 136, "taker_buy_base_volume": 25.0, "taker_buy_quote_volume": 1105120.0, "is_closed": true, "direction": -1},
    {"open_time": 1756002700000, "open": 60190.0, "high": 60328.0, "low": 60182.0, "close": 60320.0, "volume": 74.5, "close_time": 1756002999999, "quote_volume": 2412375.0, "trade_count": 143, "taker_buy_base_volume": 19.0, "taker_buy_quote_volume": 1105760.0, "is_closed": true, "direction": 1},
    {"open_time": 1756003000000, "open": 60320.0, "high": 60462.0, "low": 60308.0, "close": 60450.0, "volume": 87.5, "close_time": 1756003299999, "quote_volume": 2413750.0, "trade_count": 150, "taker_buy_base_volume": 24.0, "taker_buy_quote_volume": 1106400.0, "is_closed": true, "direction": 1},
    {"open_time": 1756003300000, "open": 60450.0, "high": 60596.0, "low": 60434.0, "close": 60580.0, "volume": 77.5, "close_time": 1756003599999, "quote_volume": 2415125.0, "trade_count": 157, "taker_buy_base_volume": 18.0, "taker_buy_quote_volume": 1107040.0, "is_closed": true, "direction": 1},
    {"open_time": 1756003600000, "open": 60580.0, "high": 60718.0, "low": 60572.0, "close": 60710.0, "volume": 90.5, "close_time": 1756003899999, "quote_volume": 2416500.0, "trade_count": 124, "taker_buy_base_volume": 23.0, "taker_buy_quote_volume": 1107680.0, "is_closed": true, "direction": 1},
    {"open_time": 1756003900000, "open": 60710.0, "high": 60852.0, "low": 60698.0, "close": 60840.0, "volume": 80.5, "close_time": 1756004199999, "quote_volume": 2417875.0, "trade_count": 131, "taker_buy_base_volume": 28.0, "taker_buy_quote_volume": 1108320.0, "is_closed": true, "direction": 1},
    {"open_time": 1756004200000, "open": 60840.0, "high": 60986.0, "low": 60824.0, "close": 60970.0, "volume": 93.5, "close_time": 1756004499999, "quote_volume": 2419250.0, "trade_count": 138, "taker_buy_base_volume": 22.0, "taker_buy_quote_volume": 1108960.0, "is_closed": true, "direction": 1},
    {"open_time": 1756004500000, "open": 60970.0, "high": 60978.0, "low": 60862.0, "close": 60870.0, "volume": 76.0, "close_time": 1756004799999, "quote_volume": 2420625.0, "trade_count": 145, "taker_buy_base_volume": 27.0, "taker_buy_quote_volume": 1109600.0, "is_closed": true, "direction": -1},
    {"open_time": 1756004800000, "open": 60870.0, "high": 60882.0, "low": 60758.0, "close": 60770.0, "volume": 66.0, "close_time": 1756005099999, "quote_volume": 2422000.0, "trade_count": 152, "taker_buy_base_volume": 21.0, "taker_buy_quote_volume": 1110240.0, "is_closed": true, "direction": -1},
    {"open_time": 1756005100000, "open": 60770.0, "high": 60786.0, "low": 60654.0, "close": 60670.0, "volume": 79.0, "close_time": 1756005399999, "quote_volume": 2423375.0, "trade_count": 159, "taker_buy_base_volume": 26.0, "taker_buy_quote_volume": 1110880.0, "is_closed": true, "direction": -1},
    {"open_time": 1756005400000, "open": 60670.0, "high": 60678.0, "low": 60562.0, "close": 60570.0, "volume": 69.0, "close_time": 1756005699999, "quote_volume": 2424750.0, "trade_count": 126, "taker_buy_base_volume": 20.0, "taker_buy_quote_volume": 1111520.0, "is_closed": true, "direction": -1},
    {"open_time": 1756005700000, "open": 60570.0, "high": 60582.0, "low": 60458.0, "close": 60470.0, "volume": 82.0, "close_time": 1756005999999, "quote_volume": 2426125.0, "trade_count": 133, "taker_buy_base_volume": 25.0, "taker_buy_quote_volume": 1112160.0, "is_closed": true, "direction": -1},
    {"open_time": 1756006000000, "open": 60470.0, "high": 60576.0, "low": 60454.0, "close": 60560.0, "volume": 69.5, "close_time": 1756006299999, "quote_volume": 2427500.0, "trade_count": 140, "taker_buy_base_volume": 19.0, "taker_buy_quote_volume": 1112800.0, "is_closed": true, "direction": 1},
    {"open_time": 1756006300000, "open": 60560.0, "high": 60658.0, "low": 60552.0, "close": 60650.0, "volume": 82.5, "close_time": 1756006599999, "quote_volume": 2428875.0, "trade_count": 147, "taker_buy_base_volume": 24.0, "taker_buy_quote_volume": 1113440.0, "is_closed": true, "direction": 1},
    {"open_time": 1756006600000, "open": 60650.0, "high": 60752.0, "low": 60638.0, "close": 60740.0, "volume": 72.5, "close_time": 1756006899999, "quote_volume": 2430250.0, "trade_count": 154, "taker_buy_base_volume": 18.0, "taker_buy_quote_volume": 1114080.0, "is_closed": true, "direction": 1},
    {"open_time": 1756006900000, "open": 60740.0, "high": 60846.0, "low": 60724.0, "close": 60830.0, "volume": 62.5, "close_time": 1756007199999, "quote_volume": 2431625.0, "trade_count": 121, "taker_buy_base_volume": 23.0, "taker_buy_quote_volume": 1114720.0, "is_closed": true, "direction": 1},
    {"open_time": 1756007200000, "open": 60830.0, "high": 60928.0, "low": 60822.0, "close": 60920.0, "volume": 75.5, "close_time": 1756007499999, "quote_volume": 2433000.0, "trade_count": 128, "taker_buy_base_volume": 28.0, "taker_buy_quote_volume": 1115360.0, "is_closed": true, "direction": 1},
    {"open_time": 1756007500000, "open": 60920.0, "high": 60932.0, "low": 60793.0, "close": 60805.0, "volume": 71.75, "close_time": 1756007799999, "quote_volume": 2434375.0, "trade_count": 135, "taker_buy_base_volume": 22.0, "taker_buy_quote_volume": 1116000.0, "is_closed": true, "direction": -1},
    {"open_time": 1756007800000, "open": 60805.0, "high": 60821.0, "low": 60674.0, "close": 60690.0, "volume": 84.75, "close_time": 1756008099999, "quote_volume": 2435750.0, "trade_count": 142, "taker_buy_base_volume": 27.0, "taker_buy_quote_volume": 1116640.0, "is_closed": true, "direction": -1},
    {"open_time": 1756008100000, "open": 60690.0, "high": 60698.0, "low": 60567.0, "close": 60575.0, "volume": 74.75, "close_time": 1756008399999, "quote_volume": 2437125.0, "trade_count": 149, "taker_buy_base_volume": 21.0, "taker_buy_quote_volume": 1117280.0, "is_closed": true, "direction": -1},
    {"open_time": 1756008400000, "open": 60575.0, "high": 60587.0, "low": 60448.0, "close": 60460.0, "volume": 87.75, "close_time": 1756008699999, "quote_volume": 2438500.0, "trade_count": 156, "taker_buy_base_volume": 26.0, "taker_buy_quote_volume": 1117920.0, "is_closed": true, "direction": -1},
    {"open_time": 1756008700000, "open": 60460.0, "high": 60476.0, "low": 60329.0, "close": 60345.0, "volume": 77.75, "close_time": 1756008999999, "quote_volume": 2439875.0, "trade_count": 123, "taker_buy_base_volume": 20.0, "taker_buy_quote_volume": 1118560.0, "is_closed": false, "direction": -1}
  ],
  "overlays": {
    "fractals": [
      {"kind": "top", "level": 5, "bar_index": 5, "start_time": 1756001500000, "end_time": 1756001799999, "high": 60566.0, "low": 60444.0, "source_ids": ["merged:5", "bar:5"]},
      {"kind": "bottom", "level": 5, "bar_index": 8, "start_time": 1756002400000, "end_time": 1756002699999, "high": 60296.0, "low": 60174.0, "source_ids": ["merged:8", "bar:8"]},
      {"kind": "top", "level": 5, "bar_index": 14, "start_time": 1756004200000, "end_time": 1756004799999, "high": 60986.0, "low": 60862.0, "source_ids": ["merged:14", "bar:14", "bar:15"]},
      {"kind": "bottom", "level": 5, "bar_index": 20, "start_time": 1756006000000, "end_time": 1756006299999, "high": 60576.0, "low": 60454.0, "source_ids": ["merged:19", "bar:20"]},
      {"kind": "top", "level": 5, "bar_index": 24, "start_time": 1756007200000, "end_time": 1756007799999, "high": 60932.0, "low": 60822.0, "source_ids": ["merged:23", "bar:24", "bar:25"]}
    ],
    "bis": [
      {"level": 5, "direction": -1, "start_time": 1756001500000, "end_time": 1756002699999, "high": 60566.0, "low": 60174.0, "source_ids": ["merged:5", "bar:5", "merged:8", "bar:8"]},
      {"level": 5, "direction": 1, "start_time": 1756002400000, "end_time": 1756004799999, "high": 60986.0, "low": 60174.0, "source_ids": ["merged:8", "bar:8", "merged:14", "bar:14", "bar:15"]},
      {"level": 5, "direction": -1, "start_time": 1756004200000, "end_time": 1756006299999, "high": 60986.0, "low": 60454.0, "source_ids": ["merged:14", "bar:14", "bar:15", "merged:19", "bar:20"]},
      {"level": 5, "direction": 1, "start_time": 1756006000000, "end_time": 1756007799999, "high": 60932.0, "low": 60454.0, "source_ids": ["merged:19", "bar:20", "merged:23", "bar:24", "bar:25"]}
    ],
    "zhongshus": [
      {"level": 5, "start_time": 1756001500000, "end_time": 1756007799999, "high": 60566.0, "low": 60454.0, "bi_ids": ["merged:5", "merged:8", "merged:14", "merged:19"]}
    ],
    "trend_types": [
      {"level": 5, "kind": "open_end", "direction": 0, "start_time": 1756001500000, "end_time": 1756007799999, "high": 60986.0, "low": 60174.0, "source_ids": ["merged:5", "merged:8", "merged:14", "merged:19", "bar:5", "bar:8", "bar:14", "bar:15", "bar:20", "merged:23", "bar:24", "bar:25"]}
    ]
  },
  "signal": null,
  "events": [],
  "data_quality": {"stale": false, "gap": false, "closed_bar_count": 29, "unclosed_bar_count": 1},
  "runtime": {"mode": "offline", "status": "alert", "data_source": "demo-fixture"}
}
`;

  const root = document.querySelector("[data-testid=dashboard-root]");
  if (!root) return;

  const PAD = { top: 12, right: 12, bottom: 18, left: 64 };
  const GRID_LEVELS = 4;

  // 页面自己的刷新节奏（与服务端 --poll-seconds 无关：服务端轮询只更新它自己
  // 的快照，不会推给页面，页面必须自己去取）。
  const POLL_INTERVAL_MS = 5000;
  // 陈旧阈值 = 容忍 3 次轮询落空。**不要写死**：写死会在轮询间隔被调大时
  // 立刻变成周期性假警报（2026-10-06 审计实测：15s 阈值碰上从不启动的轮询，
  // 页面每次加载后必然自己变成「数据陈旧」，而 health.ok 一直是 true）。
  const STALE_AFTER_MS = POLL_INTERVAL_MS * 3;

  const state = {
    snapshot: null,
    fullSnapshot: null,
    selection: null,
    selectedNode: null,
    drawPending: false,
    replayIndex: null,
    replayTimer: null,
    pollTimer: null,
    staleTimer: null,
    snapshotUrl: null,
    notificationPermission: "default",
    lastAlertSignature: "",
    lastAlertDetail: null,
    mode: new URLSearchParams(window.location.search).get("mode") || "research",
    level: null,
    crosshair: null,
    zoomLevel: 0,
    visibleWindow: null,
    pinnedRange: null,
    // R16-5 画布分发：当前画布 id、最近一次绘制的元素计数、绘制异常（回落 A 时记录）
    canvas: "A",
    canvasStats: null,
    canvasError: null,
  };

  /* ------------------------------------------------------------ DOM 基础 */

  const q = (selector) => root.querySelector(selector);

  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const asArray = (value) => (Array.isArray(value) ? value : []);
  const num = (value) =>
    typeof value === "number" && Number.isFinite(value)
      ? value
      : typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))
        ? Number(value)
        : null;

  const setAttrs = (node, attrs) => {
    Object.keys(attrs || {}).forEach((key) => {
      const value = attrs[key];
      if (value === null || value === undefined) return;
      node.setAttribute(key, String(value));
    });
    return node;
  };

  const createSvg = (tag, attrs, text) => {
    const node = setAttrs(document.createElementNS(SVG_NS, tag), attrs);
    if (text !== undefined) node.textContent = String(text);
    return node;
  };

  const createHtml = (tag, attrs, text) => {
    const node = setAttrs(document.createElement(tag), attrs);
    if (text !== undefined) node.textContent = String(text);
    return node;
  };

  /** 颜色等样式统一走内联 style，让 CSS 变量在每个 SVG 元素上解析。 */
  const paint = (node, styles) => {
    Object.keys(styles).forEach((key) => node.style.setProperty(key, styles[key]));
    return node;
  };

  const setText = (selector, value) => {
    const node = q(selector);
    if (!node) return null;
    node.textContent = value === null || value === undefined ? "—" : String(value);
    return node;
  };

  const setState = (node, value) => {
    if (node) node.setAttribute("data-state", String(value));
    return node;
  };

  const setHidden = (selector, hidden) => {
    const node = q(selector);
    if (node) node.hidden = Boolean(hidden);
    return node;
  };

  /* -------------------------------------------------------------- 格式化 */

  const pad2 = (value) => (value < 10 ? `0${value}` : String(value));

  /** 全站统一 UTC 展示（snapshot 时间为 Unix 毫秒）。 */
  const formatDateTime = (ms) => {
    if (ms === null) return "—";
    const date = new Date(ms);
    return (
      `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())} ` +
      `${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}:${pad2(date.getUTCSeconds())}`
    );
  };

  const DAY_MS = 86400000;

  /**
   * x 轴刻度文案（自适应）。
   *
   * 原实现固定输出 ``MM-DD HH:mm``，在两类场景下会误读：
   * - 窗口跨年时没有年份，``12-31`` 与次年 ``01-01`` 看起来像同一年；
   * - 日线及以上级别仍输出 ``:00`` 分钟，噪音大。
   *
   * @param {number|null} ms Unix 毫秒
   * @param {{showDate?: boolean, showTime?: boolean, showYear?: boolean}} [options]
   */
  const formatAxisTime = (ms, options = {}) => {
    if (ms === null) return "—";
    const date = new Date(ms);
    const showDate = options.showDate !== false;
    const showTime = options.showTime !== false;
    const showYear = options.showYear === true;
    const month = pad2(date.getUTCMonth() + 1);
    const day = pad2(date.getUTCDate());
    const clock = `${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}`;
    const datePart = showYear
      ? `${date.getUTCFullYear()}-${month}-${day}`
      : `${month}-${day}`;
    if (showDate && showTime) return `${datePart} ${clock}`;
    if (showDate) return datePart;
    return clock;
  };

  const formatNumber = (value, digits) => {
    if (value === null) return "—";
    const fixed = Math.abs(value).toFixed(digits);
    const parts = fixed.split(".");
    const int = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return parts[1] ? `${int}.${parts[1]}` : int;
  };

  const formatPrice = (value) => formatNumber(value, 2);

  const formatRange = (low, high) =>
    low === null || high === null ? "—" : `${formatPrice(low)} – ${formatPrice(high)}`;

  const formatVolume = (value) => {
    if (value === null) return "—";
    if (Math.abs(value) >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
    if (Math.abs(value) >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
    if (Math.abs(value) >= 1e3) return `${(value / 1e3).toFixed(2)}K`;
    return formatNumber(value, 2);
  };

  /* 周期档位名：与 index.html 的 interval-select 选项一一对应，
     避免 3600000 被显示成 "60m"、86400000 被显示成 "1440m"。 */
  const INTERVAL_LABELS = {
    60000: "1m", 180000: "3m", 300000: "5m", 900000: "15m", 1800000: "30m",
    3600000: "1h", 7200000: "2h", 14400000: "4h", 21600000: "6h",
    28800000: "8h", 43200000: "12h", 86400000: "1d", 259200000: "3d", 604800000: "1w",
  };

  const formatInterval = (ms) => {
    if (ms === null) return "—";
    return INTERVAL_LABELS[ms] || `${formatNumber(ms / MS_PER_MINUTE, 0)}m`;
  };

  const directionLabel = (direction) => {
    if (direction === 1) return "向上（+1）";
    if (direction === -1) return "向下（-1）";
    if (direction === 0) return "未定（0）";
    return "—";
  };

  const directionState = (direction) => (direction === 1 ? "up" : direction === -1 ? "down" : "flat");

  /* ---------------------------------------------------------- snapshot 读取 */

  /** 解析 `a.b[-1].c` 形式的数据路径（支持负下标），缺失返回 undefined。 */
  const resolvePath = (object, path) => {
    let value = object;
    const segments = String(path).split(".");
    for (let i = 0; i < segments.length; i += 1) {
      const match = /^([^[\]]+)(?:\[(-?\d+)\])?$/.exec(segments[i]);
      if (!match || value === null || value === undefined) return undefined;
      value = value[match[1]];
      if (match[2] !== undefined) {
        if (!Array.isArray(value)) return undefined;
        let index = Number(match[2]);
        if (index < 0) index += value.length;
        value = value[index];
      }
    }
    return value;
  };

  const formatFieldValue = (field, value) => {
    if (value === undefined || value === null) return "—";
    if (field === "market.interval_ms") return formatInterval(num(value));
    if (field === "market.last_price") return formatPrice(num(value));
    if (typeof value === "object") return "—";
    return String(value);
  };


  function normalizeCandles(raw) {
    const candles = [];
    asArray(raw).forEach((item) => {
      if (!isObject(item)) return;
      const openTime = num(item.open_time);
      const open = num(item.open);
      const high = num(item.high);
      const low = num(item.low);
      const close = num(item.close);
      if (openTime === null || open === null || high === null || low === null || close === null) return;
      candles.push({
        openTime,
        open,
        high,
        low,
        close,
        volume: num(item.volume),
        direction: close > open ? 1 : close < open ? -1 : 0,
        isClosed: item.is_closed !== false,
      });
    });
    candles.sort((left, right) => left.openTime - right.openTime);
    return candles;
  }

  const structures = (raw) => asArray(raw).filter(isObject);


  function normalizeOverlays(raw) {
    const source = isObject(raw) ? raw : {};
    const selectedLevel = state.level;
    const filter = (items) => selectedLevel === null || selectedLevel === undefined
      ? structures(items)
      : structures(items).filter((item) => Number(item.level) === selectedLevel);
    return {
      fractals: filter(source.fractals),
      bis: filter(source.bis),
      zhongshus: filter(source.zhongshus),
      trend_types: filter(source.trend_types),
    };
  }

  /* -------------------------------------------------------- 顶栏 / 状态区 */


  function setConnection(value, text) {
    root.dataset.connection = value;
    const node = setText("[data-testid=topbar-connection]", text);
    if (node) node.setAttribute("data-connection", value);
  }


  function emptyMessage(snapshot) {
    if (!snapshot) return "未提供 snapshot：页面保持空占位（离线 demo 可由 CPTDashboard.loadDemo() 加载）";
    return "snapshot 未包含 K 线：图表与结构面板保持空占位。";
  }

  // 降级原因（后端 runtime.degraded_reason）→ 面向用户的中文说明。
  const DEGRADED_REASONS = {
    upstream_fetch_failed: "无法连接行情上游（Binance），K 线与结构均不可用",
    data_guard_rejected: "上游返回的 K 线未通过数据守卫（去重/递增/缺口/OHLC 校验）",
    poll_failed: "实时轮询线程异常退出本轮采集",
    no_poll_yet: "尚未完成首次行情采集",
    // A 股（R17-3）：这两条是最常见的失败，必须与加密侧的措辞分开 ——
    // 否则"缺复权因子"会被显示成"无法连接行情上游（Binance）"，把排查方向带偏。
    no_factor: "该代码缺复权因子，画不出后复权序列（本地因子表未覆盖该标的，且按需拉取没成功）",
    no_data: "本地库 public.daily_bar 里没有该代码的行情",
    // 「按需补因子」的三种结果（见 market_a_share.js）：必须分开，否则用户不知道
    // 该不该重试 —— unsupported 重试无意义，fetch_failed 有意义。
    no_factor_unsupported: "腾讯不提供该标的的后复权数据，补不了因子（逐标的属性，无法用板块预测）",
    no_factor_cooldown: "刚为该代码拉取过因子，请稍候再试（冷却中）",
    no_factor_fetch_failed: "拉取因子失败（网络或落库），可以再试一次",
    invalid_code: "A 股代码格式不正确（期望 600519 / 600519.SH / sh600519）",
  };


  function degradedMessage(reason) {
    const key = String(reason || "").split(":")[0];
    const base = DEGRADED_REASONS[key] || "上游数据不可用";
    const detail = reason && String(reason).includes(":") ? `（${reason}）` : "";
    return `${base}${detail} · 页面保持空占位，恢复后会自动刷新。`;
  }


  function showDegraded(reason) {
    setText("[data-testid=state-degraded-message]", degradedMessage(reason));
    setHidden("[data-testid=state-degraded]", false);
    // 降级也是错误态：顶栏与连接文案同步，避免"看起来正常但没数据"
    setState(setText("[data-testid=topbar-status]", "degraded"), "degraded");
    root.dataset.status = "degraded";
    setConnection("error", degradedMessage(reason));
  }


  async function requestJson(url) {
    try {
      const response = await fetch(safeFetchUrl(url), { headers: { Accept: "application/json" } });
      let body = null;
      try {
        body = await response.json();
      } catch (error) {
        body = null;
      }
      return { ok: response.ok, status: response.status, body };
    } catch (error) {
      return { ok: false, status: 0, body: null };
    }
  }

  /** 后端 400 的 `{error:{code,message}}` → 中文提示；否则 null。 */

  function errorMessage(body) {
    if (isObject(body) && isObject(body.error) && typeof body.error.message === "string") {
      return body.error.message;
    }
    if (isObject(body) && isObject(body.error) && typeof body.error.code === "string") {
      return body.error.code;
    }
    return null;
  }

  const DASHBOARD_BASE = () => dashboardApiBase(snapshotEndpoint());

  /* ---------------- C1 范围导出 ---------------- */


  function snapshotEndpoint() {
    return state.snapshotUrl || root.dataset.snapshotUrl || new URLSearchParams(window.location.search).get("snapshot");
  }

  /**
   * 相对 ``window.location.href`` 解析出一个**可 fetch** 的绝对地址。
   *
   * ⚠️ R44 修：``new URL(endpoint, window.location.href)`` 会把 base URL 里的
   * ``user:password@`` **继承**到结果里（URL 规范：相对解析保留 base 的凭据）。
   * 而 ``fetch``/``Request`` 按规范**拒绝**带凭据的 URL，于是：
   *
   *     Failed to execute 'fetch' on 'Window': Request cannot be constructed
   *     from a URL that includes credentials: /cpt/api/dashboard/snapshot
   *
   * 触发条件：页面 URL 里带凭据。看板挂在 nginx ``auth_basic`` 后面时，
   * 用户若用 ``https://user:pwd@host/cpt/`` 这种形式打开（或任何中间层做
   * 过一次带凭据的重定向/跳转），**整个看板静默退化成离线 demo**：
   * K 线空白、盘口全空、结构详情全 `—`，只剩一条红色报错。
   *
   * 实测（2026-10-03 真机无头 Chrome，NDORACLE）：后端 ``/cpt/api/dashboard/snapshot``
   * 明明返回 200 + 真实 BTCUSDT K 线，前端却显示「暂无数据」。
   *
   * 修法：解析后显式清空 ``username``/``password``。**不能**改用
   * ``document.baseURI`` 之类的替代 base —— 凭据照样在。也不能靠 nginx 侧
   * 改 rewrite 绕开：这是**客户端**的既成事实，任何来源的凭据都要防。
   *
   * 顺带：``replaceState`` 那两处（模式切换 / 画布切换）也用同一个 base，
   * 会把凭据写回地址栏 —— 一并走这里，行为统一。
   */

  function resolveUrl(endpoint) {
    // 同源时保留会话 cookie 即可，URL 里带凭据既无必要也不安全。
    // 消毒委托给 url_safety.js（唯一实现）；它返回 null 时保持旧行为：抛出去，
    // 由调用方在 try/catch 里降级。
    const shared = window.CPT_URL;
    if (shared && typeof shared.urlObject === "function") {
      const url = shared.urlObject(endpoint);
      if (url) return url;
    }
    const url = new URL(endpoint, window.location.href);
    if (url.username || url.password) {
      url.username = "";
      url.password = "";
    }
    return url;
  }

  /**
   * **所有** ``fetch`` 出口的统一入口：把可能是相对路径、也可能带凭据的
   * ``endpoint`` 解析成一个「可 fetch」的安全地址。
   *
   * ⚠️ 为什么必须是「统一出口」而不是逐个调用点修：
   * 首屏加载走的是 ``loadSnapshot(root.dataset.snapshotUrl)`` —— 传的是
   * ``index.html`` 里那个**裸相对路径** ``/cpt/api/dashboard/snapshot``，
   * 它不经过 :func:`resolveUrl`，而是直接进 ``_fetchSnapshot`` 的 ``fetch``。
   * 只修 :func:`refreshSelectedSnapshot` 的话，**首屏照样报错**（R44 实测：
   * 改完重截一张图，报错一字未变 —— 因为压根不是那条路径）。
   */

  function safeFetchUrl(endpoint) {
    // ⚠️ R45：凭据消毒的**全站唯一实现**在 url_safety.js。这里只做委托 ——
    // 本仓今天反复吃「多份实现漂移」的亏（R45 首次修复就漏了 4 个出口），
    // 所以不再在 dashboard.js 里自己算一遍。
    const shared = window.CPT_URL;
    if (shared && typeof shared.safe === "function") return shared.safe(endpoint);
    const url = resolveUrl(endpoint);
    return url ? url.toString() : endpoint;
  }


  function inspectEndpoint(barIndex) {
    const base = snapshotEndpoint();
    if (!base) return null;
    const trimmed = base.replace(/\/?snapshot(\?.*)?$/, "");
    const separator = trimmed.endsWith("/") ? "" : "/";
    return `${trimmed}${separator}inspect?bar_index=${encodeURIComponent(String(barIndex))}`;
  }

  /**
   * 解析 inspect 端点。**同样要剥掉凭据**（见 :func:`resolveUrl`）——
   * 它虽然是用字符串拼的，但 ``snapshotEndpoint()`` 本身可能就是绝对地址，
   * 而相对拼接的 base 若来自 ``location.href`` 就会把 ``user:pwd@`` 带进来。
   */

  function resolveInspectUrl(barIndex) {
    const endpoint = inspectEndpoint(barIndex);
    if (!endpoint) return null;
    return resolveUrl(endpoint);
  }


  async function fetchInspect(barIndex) {
    const url = resolveInspectUrl(barIndex);
    if (!url) {
      return { available: false, reason: "inspect_unavailable_no_endpoint" };
    }
    try {
      const response = await fetch(safeFetchUrl(url), { headers: { Accept: "application/json" } });
      if (!response.ok) {
        return { available: false, reason: `http_${response.status}` };
      }
      return await response.json();
    } catch (error) {
      return { available: false, reason: `fetch_failed:${error && error.message ? error.message : String(error)}` };
    }
  }


  function refreshSelectedSnapshot({ level, startMs, endMs } = {}) {
    const endpoint = snapshotEndpoint();
    if (!endpoint) {
      setConnection("offline", "当前为离线 demo；切换仅更新本地选择状态");
      return Promise.resolve(null);
    }
    const url = resolveUrl(endpoint);
    const symbol = q("[data-testid=symbol-select]")?.value;
    const interval = q("[data-testid=interval-select]")?.value;
    if (symbol) url.searchParams.set("symbol", symbol);
    if (interval) url.searchParams.set("interval_ms", interval);
    if (Number.isInteger(level)) url.searchParams.set("level", String(level));
    if (Number.isInteger(startMs)) url.searchParams.set("start_ms", String(startMs));
    if (Number.isInteger(endMs)) url.searchParams.set("end_ms", String(endMs));
    return loadSnapshot(url.toString());
  }


  function installRealtimeRefresh() {
    const button = q("[data-testid=realtime-refresh]");
    if (!button) return;
    button.addEventListener("click", () => {
      root.dispatchEvent(new CustomEvent("cpt:realtime-refresh", { detail: { symbol: q("[data-testid=symbol-select]")?.value || "BTCUSDT" } }));
      refreshSelectedSnapshot();
      button.textContent = "已请求刷新";
      window.setTimeout(() => { button.textContent = "刷新实时快照"; }, 1200);
    });
  }


  function installModeSwitch() {
    root.dataset.viewMode = state.mode;
    root.querySelectorAll("[data-mode-action]").forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.modeAction === state.mode ? "true" : "false");
      button.addEventListener("click", () => {
        state.mode = button.dataset.modeAction === "watch" ? "watch" : "research";
        root.dataset.viewMode = state.mode;
        root.querySelectorAll("[data-mode-action]").forEach((item) => item.setAttribute("aria-pressed", item.dataset.modeAction === state.mode ? "true" : "false"));
        const url = resolveUrl(window.location.href);
        url.searchParams.set("mode", state.mode);
        window.history.replaceState({}, "", url);
        root.dispatchEvent(new CustomEvent("cpt:mode-changed", { detail: { mode: state.mode } }));
        setConnection("live", `视图模式：${state.mode === "watch" ? "盯盘" : "研究"}`);
      });
    });
  }


  function installRuntimeStyle() {
    if (document.getElementById(RUNTIME_STYLE_ID)) return;
    const style = createHtml("style", { id: RUNTIME_STYLE_ID, "data-owner": "dashboard.js" });
    style.textContent = RUNTIME_CSS;
    document.head.appendChild(style);
  }


  function render(snapshot, options = {}) {
    // 回放渲染（options.replay=true）只替换"当前展示"的 state.snapshot，
    // 不能覆盖权威全量快照、也不能重置回放指针——否则回放会自我吞噬。
    const isReplay = options && options.replay === true;
    state.snapshot = isObject(snapshot) ? snapshot : null;
    if (!isReplay) {
      state.fullSnapshot = state.snapshot;
      state.replayIndex = state.snapshot ? asArray(state.snapshot.candles).length : null;
    }
    renderReplayControls();
    state.selection = null;
    state.selectedNode = null;
    renderChrome(state.snapshot);
    renderWatchMetrics(state.snapshot);
    renderRunOptions(state.snapshot);
    // 回放渲染不重放提醒（前缀快照沿用同一 alerts，重复触发无意义）
    if (!isReplay) syncAlerts(state.snapshot);
    refreshLevelSelect(state.snapshot);
    renderEvents(state.snapshot);
    renderParity(state.snapshot);
    renderParityCharts(state.snapshot);
    renderSignalHistory(state.snapshot);
    renderEventAudit(state.snapshot);
    renderSignalStats(state.snapshot);
    renderSignalRadar(state.snapshot);
    renderReproducibility(state.snapshot);
    renderConfigCompare(state.snapshot);
    renderRuns(state.snapshot);
    installSliceExport();
    renderStructureDefaults(state.snapshot);
    renderEngineState(state.snapshot);
    renderLevelTree(state.snapshot);
    renderSelection();
    drawChart();
    return state.snapshot;
  }


  function installTermGlossary() {
    const glossary = q("[data-testid=term-glossary]");
    if (!glossary) return;
    glossary.querySelectorAll(".term-item").forEach((item) => {
      const dt = item.querySelector("dt");
      const dd = item.querySelector("dd");
      if (!dt || !dd) return;
      dd.hidden = true;
      dt.style.cursor = "pointer";
      dt.setAttribute("aria-expanded", "false");
      dt.addEventListener("click", () => {
        dd.hidden = !dd.hidden;
        dt.setAttribute("aria-expanded", dd.hidden ? "false" : "true");
      });
    });
    const title = glossary.querySelector(".term-glossary-title");
    if (title) {
      title.style.cursor = "pointer";
      title.addEventListener("click", () => {
        const allExpanded = [...glossary.querySelectorAll(".term-item dd")].every((dd) => !dd.hidden);
        glossary.querySelectorAll(".term-item").forEach((item) => {
          const dt = item.querySelector("dt");
          const dd = item.querySelector("dd");
          if (!dt || !dd) return;
          dd.hidden = allExpanded;
          dt.setAttribute("aria-expanded", allExpanded ? "false" : "true");
        });
      });
    }
  }


  function showError(message) {
    const node = setText("[data-testid=state-error-message]", message);
    if (node) node.setAttribute("data-state", "error");
    setHidden("[data-testid=state-error]", false);
    setState(setText("[data-testid=topbar-status]", "error"), "error");
    root.dataset.status = "error";
    setConnection("error", `加载失败：${message}`);
  }


  function setLoading(visible) {
    setHidden("[data-testid=state-loading]", !visible);
    if (visible) setConnection("connecting", "正在读取 dashboard.v2 snapshot…");
  }

  // 短 TTL 缓存：同一 URL 在 3 秒内重复请求直接命中，避免多余 round-trip。
  // 同时记录 in-flight Promise，避免并发请求都走 cache miss path
  // （典型场景：用户连点两次刷新按钮，两次都在第一次返回前触发）。
  // 条目按 LRU 淘汰（最多 8 项）。
  const SNAPSHOT_CACHE_MAX = 8;
  const SNAPSHOT_CACHE_TTL_MS = 3000;
  const snapshotCache = new Map();
  const inflightFetches = new Map();


  function cacheGet(url) {
    const entry = snapshotCache.get(url);
    if (!entry) return null;
    if (Date.now() - entry.at > SNAPSHOT_CACHE_TTL_MS) {
      snapshotCache.delete(url);
      return null;
    }
    // LRU touch
    snapshotCache.delete(url);
    snapshotCache.set(url, entry);
    return entry.snapshot;
  }


  function cachePut(url, snapshot) {
    snapshotCache.set(url, { at: Date.now(), snapshot });
    while (snapshotCache.size > SNAPSHOT_CACHE_MAX) {
      const oldest = snapshotCache.keys().next().value;
      if (oldest === url) break;
      snapshotCache.delete(oldest);
    }
  }


  async function _fetchSnapshot(url) {
    const response = await fetch(safeFetchUrl(url), { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  }

  // 数据确认新鲜：撤掉陈旧显示 + **重新计时**。
  // 两条加载路径（首屏 / 稳态后台刷新）都必须走这里 —— 少一条，那个路径就
  // 永远布不上防，页面会在阈值到点时谎报「数据陈旧」。
  function markFresh(engineStatus) {
    if (state.staleTimer !== null) {
      window.clearTimeout(state.staleTimer);
      state.staleTimer = null;
    }
    const key = engineStatus || "live";
    root.dataset.connection = "live";
    setState(setText("[data-testid=topbar-status]", key), key);
    setHidden("[data-testid=state-stale]", true);
    // 重新布防：接下来 STALE_AFTER_MS 内没有新鲜数据才算真陈旧
    state.staleTimer = window.setTimeout(() => {
      root.dataset.connection = "stale";
      setState(setText("[data-testid=topbar-status]", "stale"), "stale");
      setHidden("[data-testid=state-stale]", false);
    }, STALE_AFTER_MS);
  }


  async function loadSnapshot(url, { force = false } = {}) {
    if (!force) {
      const cached = cacheGet(url);
      if (cached) {
        // 命中缓存：同步渲染旧数据，同时在后台静默刷新一次
        render(cached);
        setHidden("[data-testid=state-error]", true);
        setConnection("live", `已加载 snapshot（缓存）· 正在后台刷新：${url}`);
        _fetchSnapshot(url)
          .then((fresh) => {
            cachePut(url, fresh);
            // 后台刷新只在用户当前没切走时回写（避免用新 BTC 数据覆盖刚加载的 ETH）
            if (state.snapshotUrl === url) {
              render(fresh);
              setConnection("live", `后台刷新完成：${url}`);
              // 稳态路径也要撤防 + 把顶栏写回真实状态，否则 stale 标记永挂
              const es = fresh && fresh.engine_state;
              markFresh(es && typeof es.status === "string" ? es.status : "live");
            }
          })
          .catch(() => undefined);
        return cached;
      }
      // 没有缓存但有 in-flight fetch：复用同一个 Promise，避免重复网络请求
      const inflight = inflightFetches.get(url);
      if (inflight) {
        return inflight;
      }
    }
    setLoading(true);
    const promise = (async () => {
      try {
        const snapshot = await _fetchSnapshot(url);
        cachePut(url, snapshot);
        render(snapshot);
        setHidden("[data-testid=state-error]", true);
        setConnection("live", `已加载 snapshot：${url}`);
        // 轮询**自启动**。2026-10-06 审计发现：startPolling 全仓只有「实时」按钮
        // 一个调用点（在 dash-chrome.js 的 click 回调里），页面加载后从不启动轮询
        // ⇒ 更新时间永远冻结，陈旧看门狗必然触发。
        //
        // ⚠️ 走 `globalThis.CPTDashboardOps` 而不是裸标识符：dash-*.js 各自是独立
        // IIFE，裸标识符跨文件**不可达**。原先这里写的是
        // `typeof startPolling === "function"` —— 它把「函数不可达」这件事
        // **静默吞掉**了，页面照样不轮询、照样报陈旧，却一个错都不报。
        // 这种守卫比没有更坏：它让死代码看起来是活的。取不到就直接抛。
        const ops = typeof globalThis !== "undefined" ? globalThis.CPTDashboardOps : null;
        if (!ops || typeof ops.startPolling !== "function") {
          throw new Error(
            "CPTDashboardOps.startPolling 不可达：dash-ops.js 的 IIFE 没有把轮询挂到 globalThis。" +
              "跨文件必须走 globalThis 显式挂载（范式见 cpt_job.js 的 global.CPTJob）。",
          );
        }
        ops.startPolling(url, POLL_INTERVAL_MS);
        markFresh(
          snapshot && snapshot.engine_state && typeof snapshot.engine_state.status === "string"
            ? snapshot.engine_state.status
            : "live",
        );
        return snapshot;
      } catch (error) {
        // 与 D2 行为一致：错误只体现在状态区，不向调用方抛出。
        showError(error && error.message ? error.message : String(error));
        return null;
      } finally {
        inflightFetches.delete(url);
        setLoading(false);
        setHidden("[data-testid=state-loading]", true);
      }
    })();
    if (!force) inflightFetches.set(url, promise);
    return promise;
  }


  function demoSnapshot() {
    return JSON.parse(DEMO_FIXTURE_JSON);
  }


  function loadDemo() {
    const snapshot = demoSnapshot();
    render(snapshot);
    setHidden("[data-testid=state-error]", true);
    setConnection(
      "offline",
      `离线 demo · 本地 demo fixture（${asArray(snapshot.candles).length} 根 K 线）· 无网络请求`,
    );
    return snapshot;
  }


  function clear() {
    render(null);
    setHidden("[data-testid=state-error]", true);
    return null;
  }

  /* --------------------------------------------------- 画布分发（R16-5）
   *
   * 四个画布（A 手写 SVG / B lightweight-charts / C plotly / D wbt 报告）
   * 并存，`?canvas=A|B|C|D` 或顶栏按钮切换，最后选一个。
   *
   * 关键不变式：**四个画布必须消费同一份归一化数据**。所以归一化（K 线、
   * 叠加层、价格域、像素坐标函数）留在 dashboard.js 里，由 buildCanvasView()
   * 统一产出，画布只负责"画"。
   *
   * 每个画布的 draw(view) 必须返回**自己真正画出来的**元素个数（不是"快照里
   * 有几个"），审计脚本据此断言四个画布计数两两相等。
   */
  const CANVAS_FALLBACK = "A";


  function canvasRegistry() {
    return window.CPT_CANVASES || null;
  }


  function canvasList() {
    const registry = canvasRegistry();
    return registry ? registry.list() : [{ id: "A", label: "A 手写 SVG", note: "" }];
  }

  // 归一化视图：四个画布共用的唯一数据入口（原 drawChart 前 50 行原样抽出）。

  function renderCanvasPlaceholder(view) {
    const { canvas, volumeNode, macdNode, axisNode } = view;
    clearRegion(canvas);
    clearRegion(volumeNode);
    if (macdNode) clearRegion(macdNode);
    clearRegion(axisNode);
    appendNote(
      canvas,
      !view.candles.length
        ? "暂无 K 线：等待 dashboard.v2 snapshot（empty）"
        : "图形区尚未完成布局，等待下一次重绘",
    );
    appendNote(volumeNode, "暂无成交量数据");
    if (macdNode) appendNote(macdNode, "暂无 MACD 数据");
    appendNote(axisNode, "时间轴：无数据（Unix 毫秒）");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", "K 线绘制区占位：分型、笔、中枢、走势类型由画布渲染");
    updateZoomLabel();
  }

  // 画布 A 的计数：直接数**画出来的 DOM 节点**（不是数输入数组），
  // 这样"计数相等"才真的证明四个画布画了同样多的结构元素。
  // K 线按 `data-bar-index` 去重：一根 K 线画 2 个节点（影线 line + 实体 rect），
  // 直接数节点会得到 2 倍（首轮审计实测 360 vs 180）。

  function canvasACounts(view) {
    const count = (kind) =>
      view.canvas.querySelectorAll(`[data-structure-kind=${kind}]`).length;
    const bars = new Set();
    view.canvas.querySelectorAll("[data-structure-kind=candle]").forEach((node) => {
      bars.add(node.getAttribute("data-bar-index"));
    });
    return {
      canvas: "A",
      candles: bars.size,
      fractals: count("fractal"),
      bis: count("bi"),
      zhongshus: count("zhongshu"),
      trendTypes: view.overlays.trend_types.length,
    };
  }


  function registerBuiltinCanvasA() {
    const registry = canvasRegistry();
    if (!registry) return;
    registry.register("A", {
      label: "A 手写 SVG",
      note: "零依赖手写 SVG（原有画布，行为未改）",
      draw: drawChartA,
    });
  }

  // 唯一分发点：所有重绘路径（缩放/滚轮/双击/级别筛选/render/ResizeObserver）
  // 都汇到这里，所以画布切换不需要改任何调用点。

  function dashboardApiBase(snapshotUrl) {
    const fallback = "/api/dashboard";
    const raw = String(snapshotUrl || "");
    const marker = "/api/dashboard";
    const index = raw.indexOf(marker);
    return index >= 0 ? raw.slice(0, index + marker.length) : fallback;
  }


  async function loadHealth() {
    const base = dashboardApiBase(snapshotEndpoint());
    let data = null;
    try {
      const response = await fetch(safeFetchUrl(`${base}/health`), { headers: { Accept: "application/json" } });
      if (response.ok) data = await response.json();
    } catch (error) {
      data = null;
    }
    const node = q("[data-testid=health-status]");
    if (node) {
      if (!isObject(data)) {
        node.dataset.state = "unknown";
        setText("[data-testid=health-status]", "无法获取");
      } else {
        const ok = data.ok === true;
        const degraded = data.degraded === true;
        node.dataset.state = ok && !degraded ? "ok" : "blocked";
        setText("[data-testid=health-status]", ok && !degraded ? "正常" : degraded ? "已降级" : "异常");
        setState(
          setText("[data-testid=health-read-only]", data.read_only === true ? "是" : "否"),
          data.read_only === true ? "true" : "false",
        );
        setState(
          setText("[data-testid=health-degraded]", degraded ? "是" : "否"),
          degraded ? "true" : "false",
        );
        const lastError = typeof data.last_error === "string" && data.last_error ? data.last_error : "";
        const errorRow = q("[data-testid=health-error-row]");
        if (errorRow) errorRow.hidden = !lastError;
        if (lastError) setText("[data-testid=health-last-error]", lastError);
      }
    }
    return data;
  }


  async function loadSources({ refresh = false } = {}) {
    const base = dashboardApiBase(snapshotEndpoint());
    const url = `${base}/sources${refresh ? "?refresh=1" : ""}`;
    let data = null;
    try {
      const response = await fetch(safeFetchUrl(url), { headers: { Accept: "application/json" } });
      if (response.ok) data = await response.json();
    } catch (error) {
      data = null;
    }
    const list = q("[data-testid=source-list]");
    if (!list) return data;
    const sources = data ? asArray(data.sources) : [];
    if (!sources.length) {
      // 保留空态占位；已有渲染结果时不要被一次瞬时失败清空。
      if (!list.querySelector("[data-testid=source-item]")) {
        setText("[data-testid=source-list-empty]", data ? "数据源列表为空" : "数据源探活失败（服务不可达）");
      }
      return data;
    }
    const items = sources.map((source) => {
      const item = document.createElement("li");
      item.dataset.testid = "source-item";
      item.dataset.sourceId = String(source.id || "");
      item.dataset.state = isObject(source.probe) ? String(source.probe.status || "") : "unknown";

      const title = document.createElement("span");
      title.className = "source-title";
      title.textContent = source.label || source.id || "未知数据源";
      item.appendChild(title);

      const meta = document.createElement("span");
      meta.className = "source-meta";
      const role = SOURCE_ROLE_LABELS[source.role] || source.role || "";
      const probe = isObject(source.probe) ? source.probe : null;
      const statusLabel = probe ? PROBE_STATUS_LABELS[probe.status] || probe.status || "" : "未探测";
      const latency = probe && num(probe.latency_ms) !== null ? `${Math.round(num(probe.latency_ms))}ms` : "";
      const quota = source.quota === "wind" ? "消耗额度" : "";
      meta.textContent = [role, statusLabel, latency, quota].filter(Boolean).join(" · ");
      item.appendChild(meta);

      const detail = probe && typeof probe.detail === "string" ? probe.detail : "";
      const note = typeof source.note === "string" ? source.note : "";
      item.title = [detail, note].filter(Boolean).join(" | ");
      return item;
    });
    list.replaceChildren(...items);
    return data;
  }


  function installSourcePanel() {
    const refreshButton = q("[data-testid=source-refresh]");
    if (refreshButton) {
      refreshButton.addEventListener("click", () => {
        refreshButton.disabled = true;
        Promise.all([loadHealth(), loadSources({ refresh: true })]).finally(() => {
          refreshButton.disabled = false;
        });
      });
    }
    loadHealth();
    loadSources();
  }


  function boot() {
    installRuntimeStyle();
    registerBuiltinCanvasA();
    installCanvasSwitch();
    installModeSwitch();
    installIntervalSwitch();
    installTimeRange();
    installSymbolSwitch();
    installRealtimeRefresh();
    installAlertObserver();
    installLevelFilter();
    installCrosshair();
    installZoomControls();
    ensureSelectionSection();
    installReplayControls();
    installTermGlossary();
    installSourcePanel();
    installOpsPanels();

    if (typeof window.ResizeObserver === "function") {
      const observer = new window.ResizeObserver(() => scheduleDraw());
      [q("[data-testid=chart-canvas-region]"), q("[data-testid=chart-volume-region]"), q("[data-testid=chart-macd-region]")].forEach((node) => {
        if (node) observer.observe(node);
      });
    }
    window.addEventListener("resize", scheduleDraw);

    const params = new URLSearchParams(window.location.search);
    // A 股市场模式（R17-3）。模块只负责"换 snapshot URL + 换顶栏控件"，
    // 渲染仍走 drawChart() 分发到四个画布 —— 所以这里不碰任何画布代码。
    const aShare =
      window.CPTAShare && typeof window.CPTAShare.install === "function"
        ? window.CPTAShare.install({ loadSnapshot })
        : null;
    state.aShare = aShare;
    // ``?snapshot=`` 优先于 ``data-snapshot-url``（R16-5 改）。
    // 原顺序（属性优先）让 ``?snapshot=`` 完全失效 —— 属性在 index.html 里恒非空，
    // 于是文档里写的"file:// + 内联 JSON 默认离线"根本走不到，离屏审计也无法把
    // 页面指到一份固定快照上。改为参数优先后：不传参数 = 原行为（走属性），
    // 传了就覆盖。``?demo=off`` 的语义不变。
    const explicitSnapshot = params.get("snapshot");
    const snapshotUrl = explicitSnapshot || root.dataset.snapshotUrl;
    const aShareMode =
      Boolean(aShare) && aShare.getMarket() === "a_share" && !explicitSnapshot;
    if (aShareMode) {
      // 交给市场模块去拉 A 股快照（它还要先取热门池填下拉），这里不重复发请求。
      aShare.init();
    } else {
      if (aShare) aShare.init();
      if (snapshotUrl) {
        state.snapshotUrl = snapshotUrl;
        loadSnapshot(snapshotUrl);
      } else if (params.get("demo") !== "off") loadDemo();
      else render(null);
    }

    root.dispatchEvent(new CustomEvent("cpt:dashboard-ready"));
  }

// >>>FUNCS
// <<<TAIL
  window.CPTDashboard = {
    schemaVersion: SCHEMA_VERSION,
    render,
    loadSnapshot,
    getAShare: () => state.aShare,
    demoSnapshot,
    loadDemo,
    clear,
    getSnapshot: () => state.snapshot,
    getSelection: () => state.selection,
    startPolling,
    stopPolling,
    // ---- 画布层（R16-5）：画布模块与审计脚本的公开入口 ----
    // buildView: 四个画布共用的归一化视图（同一份快照 + 同一套像素坐标）
    buildView: buildCanvasView,
    // clearRegion / appendNote: 画布模块重置区域与写占位文案的唯一契约
    clearRegion,
    appendNote,
    getCanvas: () => state.canvas,
    getCanvasStats: () => state.canvasStats,
    canvasList,
    setCanvas,
    redraw: drawChart,
    // ⚠️ R45 补暴露：另外三个文件（canvas_d / inspection_panel / market_a_share）
    // 的 fetch 也必须走 safeFetchUrl —— 相对路径在**带凭据的页面**上会继承
    // URL 里的 user:pwd@，撞 "Request cannot be constructed from a URL that
    // includes credentials"。这次修复漏了它们，统一出口在这里。
    // R51：canvas_d.js 已随画布 D 下线，剩 inspection_panel / market_a_share。
    safeFetchUrl,
  };


  boot();
})();
// >>>TAIL
