// dashboard.bundle.js — **自动生成，请勿手改**
//
// 由 scripts/build_dashboard_bundle.py 从 7 个按功能拆分的源文件拼接而成：
//   · dash-core.js
//   · dash-chrome.js
//   · dash-structure.js
//   · dash-signal.js
//   · dash-chart.js
//   · dash-alert.js
//   · dash-ops.js
//
// ⚠️ 改了任何一个 dash-*.js 都要重跑 `python scripts/build_dashboard_bundle.py`。
//    CI 有门禁检查这个 bundle 是否与源文件同步（见 .github/workflows/ci.yml）。
//
// 为什么拼接成一个而不是发 7 个请求：
//   · 跨模块调用共 88 处，分成 7 个独立 IIFE 会**全部断掉**
//     （实测 `startPolling is not defined`）；
//   · 一个请求对缓存与首屏都更好；
//   · 源文件仍按功能分开，**维护成本**不受影响。

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
        // ⇒ 更新时间永远冻结，15s 后 stale 定时器必然触发，页面周期性地谎报
        // 「数据陈旧」，而此时 health.ok / data_quality.stale 全是正常值。
        // startPolling 内部先 stopPolling()，幂等，所以每次成功加载都调是安全的。
        if (typeof startPolling === "function") startPolling(url, POLL_INTERVAL_MS);
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


  function renderChrome(snapshot) {
    const market = isObject(snapshot) && isObject(snapshot.market) ? snapshot.market : {};
    const quality = isObject(snapshot) && isObject(snapshot.data_quality) ? snapshot.data_quality : {};
    const runtime = isObject(snapshot) && isObject(snapshot.runtime) ? snapshot.runtime : {};
    const candles = normalizeCandles(snapshot && snapshot.candles);

    root.querySelectorAll("[data-field]").forEach((node) => {
      node.textContent = formatFieldValue(node.dataset.field, resolvePath(snapshot, node.dataset.field));
    });

    // 证券名称（A 股有，加密没有）。**不放进 data-field 绑定**：那套把 undefined
    // 格式化成 "—"，加密模式下会在顶栏留一个孤零零的破折号。这里空则直接隐藏。
    //
    // 之所以要在顶栏显示名称：A 股只有六位数字，600519/600815 这种一眼看岔，
    // 而"看的是不是我想的那只票"是看盘第一件要确认的事。
    const securityName = typeof market.name === "string" ? market.name.trim() : "";
    const nameNode = setText("[data-testid=topbar-security-name]", securityName);
    if (nameNode) {
      nameNode.hidden = securityName === "";
      const board = typeof market.board === "string" ? market.board.trim() : "";
      nameNode.setAttribute("title", board ? `${securityName} · ${board}` : securityName);
    }

    // 页面标题也带上标的：同时开几个标签页时，"看岔"就发生在标签栏这一层。
    // 名称放最前面——标签被截断时先看到的是它。
    const titleSymbol = typeof market.symbol === "string" ? market.symbol.trim() : "";
    if (titleSymbol) {
      document.title = securityName ? `${securityName} ${titleSymbol} · CPT` : `${titleSymbol} · CPT`;
    }

    const firstOpen = num(market.first_open_time);
    const lastOpen = num(market.last_open_time);
    // 更新时间读 snapshot 的生成时刻（runtime.generated_at），不是 K 线窗口起点；
    // 这样 30s 轮询时显示会真实滚动，反映 snapshot 何时被生成。
    const generatedAt = num(runtime.generated_at) || num(snapshot && snapshot.reproducibility && snapshot.reproducibility.generated_at);
    const updatedNode = setText(
      "[data-testid=topbar-updated-at]",
      generatedAt !== null ? formatDateTime(generatedAt) : "—",
    );
    if (updatedNode) {
      updatedNode.setAttribute(
        "title",
        generatedAt !== null
          ? "snapshot 生成时刻（runtime.generated_at）"
          : "上游未注入 generated_at 时间戳",
      );
    }
    setText("[data-testid=market-time-range]", firstOpen === null || lastOpen === null
      ? "—"
      : `${formatDateTime(firstOpen)} → ${formatDateTime(lastOpen)}`);
    setText("[data-testid=market-bar-count]", candles.length);
    setText("[data-testid=replay-schema-version]", (isObject(snapshot) && snapshot.schema_version) || SCHEMA_VERSION);

    const runtimeStale = runtime.stale === true;
    const stale = quality.stale === true || runtimeStale;
    const gap = quality.gap === true;
    setState(setText("[data-testid=data-quality-stale]", stale ? "true" : "false"), stale ? "true" : "false");
    setState(setText("[data-testid=data-quality-gap]", gap ? "true" : "false"), gap ? "true" : "false");

    const reason = quality.reason;
    setText("[data-testid=data-quality-reason]", typeof reason === "string" && reason ? reason : "—");
    const factorFetch = isObject(quality.factor_fetch) ? quality.factor_fetch : null;
    if (factorFetch) {
      const fetched = factorFetch.fetched === true;
      const rows = factorFetch.rows != null ? `${factorFetch.rows} 行` : "";
      const reasonSuffix = typeof factorFetch.reason === "string" ? ` (${factorFetch.reason})` : "";
      setText("[data-testid=data-quality-factor-fetch]", fetched ? `已获取 ${rows}`.trim() : `未获取${reasonSuffix}`);
    } else {
      setText("[data-testid=data-quality-factor-fetch]", "—");
    }

    // C7：数据质量明细（severity / 缺口数 / 乱序数 + 明细列表）。
    // 后端这几个字段缺失（老快照）时整块隐藏，既有输出一个字不动。
    const qualityList = q("[data-testid=data-quality] .quality-list");
    if (qualityList) {
      const upsertQualityRow = (testid) => {
        let node = q(`[data-testid=${testid}]`);
        if (!node) {
          node = document.createElement("li");
          node.className = "quality-wide";
          node.dataset.testid = testid;
          qualityList.appendChild(node);
        }
        return node;
      };
      const severity = typeof quality.severity === "string" ? quality.severity : "";
      const gapCount = num(quality.gap_count);
      const outOfOrderCount = num(quality.out_of_order_count);
      const gaps = asArray(quality.gaps);
      const outOfOrder = asArray(quality.out_of_order);

      const detailRow = upsertQualityRow("data-quality-detail");
      if (!severity && gapCount === null && outOfOrderCount === null) {
        detailRow.hidden = true;
        detailRow.replaceChildren();
      } else {
        detailRow.hidden = false;
        // 严重度直接用后端 severity 标 data-state；缺省时按既有 gap/stale 口径兜底。
        detailRow.dataset.state = severity || (gap ? "gap" : stale ? "stale" : "ok");
        const label = document.createElement("span");
        label.textContent = "质量明细";
        const value = document.createElement("span");
        value.dataset.testid = "data-quality-detail-text";
        const parts = [];
        if (severity) parts.push(`severity ${severity}`);
        if (gapCount) parts.push(`缺口 ${gapCount} 处`);
        if (outOfOrderCount) parts.push(`乱序 ${outOfOrderCount} 处`);
        value.textContent = parts.length ? parts.join(" · ") : "无异常";
        detailRow.replaceChildren(label, value);
      }

      const listRow = upsertQualityRow("data-quality-detail-list");
      const describeQualityEntry = (kind, entry) => {
        if (isObject(entry)) {
          const start = num(entry.start_ms);
          const end = num(entry.end_ms);
          if (start !== null || end !== null) {
            return `${kind} · ${start === null ? "—" : formatDateTime(start)} → ${end === null ? "—" : formatDateTime(end)}`;
          }
          const at = num(entry.open_time);
          if (at !== null) return `${kind} · ${formatDateTime(at)}`;
          return `${kind} · ${JSON.stringify(entry)}`;
        }
        return `${kind} · ${String(entry)}`;
      };
      const entries = [
        ...gaps.map((entry) => describeQualityEntry("缺口", entry)),
        ...outOfOrder.map((entry) => describeQualityEntry("乱序", entry)),
      ];
      if (!entries.length) {
        listRow.hidden = true;
        listRow.replaceChildren();
      } else {
        listRow.hidden = false;
        listRow.dataset.state = "gap";
        const detailList = document.createElement("ul");
        detailList.dataset.testid = "data-quality-detail-entries";
        entries.slice(0, 20).forEach((text) => {
          const item = document.createElement("li");
          item.textContent = text;
          detailList.appendChild(item);
        });
        if (entries.length > 20) {
          const more = document.createElement("li");
          more.textContent = `… 仅显示前 20 条（共 ${entries.length} 条）`;
          detailList.appendChild(more);
        }
        listRow.replaceChildren(detailList);
      }
    }

    // T+1 交易日历（A 股专属）：快照未带上这一块时整行隐藏，加密模式不受影响。
    const tPlusOne = isObject(snapshot) && isObject(snapshot.t_plus_one) ? snapshot.t_plus_one : null;
    const tPlusOneNode = q("[data-testid=t-plus-one]");
    if (tPlusOneNode) {
      if (tPlusOne && tPlusOne.available !== undefined) {
        tPlusOneNode.hidden = false;
        const available = tPlusOne.available === true;
        const today = typeof tPlusOne.today === "string" ? tPlusOne.today : null;
        const nextDate = typeof tPlusOne.next_trade_date === "string" ? tPlusOne.next_trade_date : null;
        const reason = typeof tPlusOne.reason === "string" ? tPlusOne.reason : "";
        let statusText = available ? "今日可买" : "今日不可买";
        if (today) statusText += `（${today}）`;
        if (!available && nextDate) statusText += `，下一交易日 ${nextDate}`;
        setText("[data-testid=t-plus-one-status]", statusText);
        tPlusOneNode.dataset.state = available ? "ok" : "blocked";
        tPlusOneNode.setAttribute("title", reason || "");
      } else {
        tPlusOneNode.hidden = true;
      }
    }

    // 历史区间查询（replay/range 模式）：live 模式快照无 range 字段 → 整行隐藏。
    const rangeInfo = isObject(snapshot) && isObject(snapshot.range) ? snapshot.range : null;
    const rangeNode = q("[data-testid=range-query]");
    if (rangeNode) {
      const rangeStart = rangeInfo ? num(rangeInfo.start_ms) : null;
      const rangeEnd = rangeInfo ? num(rangeInfo.end_ms) : null;
      if (rangeInfo && rangeStart !== null && rangeEnd !== null) {
        rangeNode.hidden = false;
        const rangeAvailable = rangeInfo.available === true;
        const rangeCount = rangeInfo.bar_count != null ? `（${rangeInfo.bar_count} 根）` : "";
        const rangeReason =
          typeof rangeInfo.reason === "string" && rangeInfo.reason
            ? RANGE_REASON_LABELS[rangeInfo.reason] || rangeInfo.reason
            : "";
        let rangeText = `${formatDateTime(rangeStart)} → ${formatDateTime(rangeEnd)}${rangeCount}`;
        if (!rangeAvailable && rangeReason) rangeText += ` · ${rangeReason}`;
        setText("[data-testid=range-query-status]", rangeText);
        rangeNode.dataset.state = rangeAvailable ? "ok" : "blocked";
        rangeNode.setAttribute(
          "title",
          rangeAvailable ? "本次历史区间查询返回的 K 线区间" : `区间查询未成功：${rangeReason || "原因未知"}`,
        );
      } else {
        rangeNode.hidden = true;
      }
    }

    const lastPrice = num(market.last_price);
    setText("[data-testid=market-last-price]", lastPrice === null ? "—" : formatPrice(lastPrice));
    const last = candles.length ? candles[candles.length - 1] : null;
    const first = candles.length ? candles[0] : null;
    const changeNode = q("[data-testid=market-change]");
    if (changeNode) {
      const market24h = isObject(snapshot) && isObject(snapshot.market_24h) ? snapshot.market_24h : null;
      const has24h = market24h && market24h.available === true;
      const upstreamPct = has24h ? Number(market24h.price_change_pct) : NaN;
      const changePct = Number.isFinite(upstreamPct) ? upstreamPct : null;
      changeNode.textContent = changePct === null ? "—" : `${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`;
      changeNode.dataset.state = changePct === null ? "flat" : changePct >= 0 ? "up" : "down";
      changeNode.dataset.source = has24h && Number.isFinite(upstreamPct) ? "24h" : "unavailable";
      changeNode.setAttribute(
        "title",
        has24h && Number.isFinite(upstreamPct) ? "上游 24h 涨跌幅" : "上游 24h 涨跌幅不可用（A 股暂无数据源）",
      );
    }
    const countdownNode = q("[data-testid=close-countdown]");
    if (countdownNode) {
      // R34：这里是 `close_countdown`（snake_case），不是 `closeCountdown`。
      // 写错的代价特别隐蔽：取到 undefined → 三个分支全落空 → 掉进
      // `else if (last)` 显示「距下一根 K 线」，**一个看着挺合理的错标签**，
      // 而服务算好的 available/reason 永远没人读。
      const cc = snapshot.close_countdown;
      if (cc && cc.available && cc.is_open) {
        const mins = Math.floor(cc.seconds_to_close / 60);
        const secs = cc.seconds_to_close % 60;
        countdownNode.textContent = `${mins}m ${secs}s`;
        countdownNode.title = `距 A 股收盘 (${cc.close_time})`;
      } else if (cc && !cc.available) {
        countdownNode.textContent = "—";
        countdownNode.title = cc.reason === "not_a_trade_day" ? "今日非交易日" : "收盘倒计时不可用";
      } else if (last) {
        const interval = num(market.interval_ms) || 300000;
        const remaining = Math.max(0, last.openTime + interval - Date.now());
        countdownNode.textContent = `${Math.floor(remaining / 60000)}m ${Math.floor((remaining % 60000) / 1000)}s`;
        countdownNode.title = "距下一根 K 线";
      }
    }
    // 双数据集对比（R21 Phase 6 P2）
    const dualSection = q("[data-testid=dual-compare]");
    if (dualSection) {
      // R34：同上，服务发的是 `dual_compare`。写错的后果是
      // `dc.available` 恒为 undefined → 走 else → `dualSection.hidden = true`，
      // **整个双数据集对比面板永久隐藏**，而且没有任何报错。
      const dc = snapshot.dual_compare;
      if (dc && dc.available) {
        dualSection.hidden = false;
        setText("[data-testid=dual-cpt-close]", dc.cpt_close ? formatPrice(dc.cpt_close) : "—");
        setText("[data-testid=dual-realtime-price]", dc.realtime_price ? formatPrice(dc.realtime_price) : "—");
        const div = dc.divergence_pct;
        setText("[data-testid=dual-divergence]", div === null ? "—" : `${div >= 0 ? "+" : ""}${div.toFixed(2)}%`);
      } else {
        dualSection.hidden = true;
      }
    }
    const previous = candles.length > 1 ? candles[candles.length - 2] : null;
    const trendDirection = last && previous ? (last.close > previous.close ? 1 : last.close < previous.close ? -1 : 0) : 0;
    setState(
      setText(
        "[data-testid=market-price-direction]",
        last && previous ? (trendDirection === 1 ? "上涨" : trendDirection === -1 ? "下跌" : "持平") : "持平",
      ),
      last && previous ? directionState(trendDirection) : "flat",
    );

    const market24h = isObject(snapshot) && isObject(snapshot.market_24h) ? snapshot.market_24h : null;
    const market24hAvailable = market24h && market24h.available === true;
    const high24 = setText("[data-testid=market-high-24h]", market24hAvailable ? formatPrice(market24h.high) : "不可用");
    if (high24) high24.setAttribute("title", market24hAvailable ? "上游 24h 聚合" : "上游未提供真实 24h 聚合");
    const low24 = setText("[data-testid=market-low-24h]", market24hAvailable ? formatPrice(market24h.low) : "不可用");
    if (low24) low24.setAttribute("title", market24hAvailable ? "上游 24h 聚合" : "上游未提供真实 24h 聚合");
    const quoteVolume = market24hAvailable ? Number(market24h.quote_volume) : NaN;
    const volumeNode = q("[data-testid=market-volume]");
    if (volumeNode) {
      if (Number.isFinite(quoteVolume)) {
        volumeNode.textContent = `${formatVolume(quoteVolume)} USDT`;
        volumeNode.setAttribute("title", "上游 24h USDT 成交额");
        volumeNode.dataset.source = "24h";
      } else if (candles.length) {
        const totalVolume = candles.reduce(
          (sum, bar) => sum + (bar.volume === null ? 0 : bar.volume),
          0
        );
        volumeNode.textContent = formatVolume(totalVolume);
        volumeNode.setAttribute("title", "当前 snapshot 窗口累计成交量（非 24h）");
        volumeNode.dataset.source = "window";
      } else {
        volumeNode.textContent = "—";
        volumeNode.setAttribute("title", "上游未提供 24h 成交额");
        volumeNode.dataset.source = "none";
      }
    }

    // 上游降级：后端在 fetch 失败 / 数据守卫拒绝时返回带 degraded 标记的空快照。
    // 必须显式呈现，否则用户只看到「图表保持空占位」，无法区分「上游挂了」和
    // 「本来就没数据」。degraded 优先于 empty 态。
    const degraded = runtime.degraded === true;
    const degradedReason =
      typeof runtime.degraded_reason === "string" ? runtime.degraded_reason : "";

    let stateKey = "empty";
    if (candles.length) {
      if (gap) stateKey = "gap";
      else if (stale) stateKey = "stale";
      else stateKey = runtime.status === "alert" ? "alert" : "confirmed";
    }
    if (degraded) stateKey = "degraded";
    setState(setText("[data-testid=topbar-status]", stateKey), stateKey);
    // 降级时不显示「暂无数据」空态：其文案是「离线 demo」，会掩盖真实原因
    setHidden("[data-testid=state-empty]", candles.length > 0 || degraded);
    setHidden("[data-testid=state-stale]", !(candles.length && stale));
    setHidden("[data-testid=state-gap]", !(candles.length && gap));

    root.dataset.status = stateKey;
    const dataSource = typeof runtime.data_source === "string" ? runtime.data_source.toLowerCase() : "";
    const isRealtime = dataSource.includes("realtime") || dataSource.includes("binance");
    root.dataset.runtimeMode = isRealtime ? "realtime" : "offline";
    root.dataset.viewMode = state.mode;
    root.dataset.dataSource = typeof runtime.data_source === "string" ? runtime.data_source : "unknown";

    root.dataset.degraded = degraded ? "true" : "false";
    if (degraded) {
      root.dataset.degradedReason = degradedReason || "unknown";
      showDegraded(degradedReason);
    } else {
      root.removeAttribute("data-degraded-reason");
      setHidden("[data-testid=state-degraded]", true);
      if (!candles.length) {
        setConnection("offline", emptyMessage(snapshot));
      } else if (!isRealtime) {
        setConnection("offline", `离线 snapshot（${candles.length} 根 K 线，${runtime.data_source || "fixture"}）· 无网络请求`);
      } else {
        setConnection("live", `实时 snapshot（${candles.length} 根 K 线，binance_realtime）`);
      }
    }

    // 结构预警（A 股专属）：summary.structural_alert = 笔序列首次跌破前低。
    // 加密快照没有这个字段 → 恒为 false，横幅保持 hidden。
    const summary = isObject(snapshot) && isObject(snapshot.summary) ? snapshot.summary : {};
    const structuralAlert = summary.structural_alert === true;
    setHidden("[data-testid=structural-alert]", !structuralAlert);
  }

  /* ---------------------------------------------------------- 结构面板填充 */


  function updateZoomLabel() {
    const label = q("[data-testid=chart-zoom-level]");
    if (!label) return;
    const candles = state.snapshot ? normalizeCandles(state.snapshot.candles) : [];
    const total = candles.length;
    if (!total) {
      label.textContent = "全部";
    } else if (state.visibleWindow) {
      label.textContent = `${applyZoomWindow(candles).length} / ${total}`;
    } else if (total > DEFAULT_VISIBLE_BARS) {
      label.textContent = `最近 ${DEFAULT_VISIBLE_BARS} / ${total}`;
    } else {
      label.textContent = `${total} / ${total}`;
    }
  }

  /** 结构是否落在未收盘区间：snapshot 标记 alert 且结构覆盖了最后一根 K 线。 */
  const structureStateOf = (view, endTime) => {
    const runtime = isObject(state.snapshot) && isObject(state.snapshot.runtime) ? state.snapshot.runtime : {};
    if (runtime.status !== "alert") return "confirmed";
    return endTime !== null && endTime >= view.lastOpenTime ? "alert" : "confirmed";
  };


  function installSymbolSwitch() {
    const select = q("[data-testid=symbol-select]");
    if (!select) return;
    select.addEventListener("change", () => {
      const symbol = select.value;
      setText("[data-testid=topbar-symbol]", symbol);
      root.dataset.symbol = symbol;
      setConnection("connecting", `正在切换交易对：${symbol}`);
      root.dispatchEvent(new CustomEvent("cpt:symbol-changed", { detail: { symbol } }));
      refreshSelectedSnapshot();
    });
  }


  function installIntervalSwitch() {
    const select = q("[data-testid=interval-select]");
    if (!select) return;
    select.addEventListener("change", () => {
      const option = select.selectedOptions && select.selectedOptions[0];
      const label = option && typeof option.textContent === "string" && option.textContent.trim() !== ""
        ? option.textContent.trim()
        : String(select.value);
      setText("[data-testid=topbar-interval]", label);
      root.dataset.intervalMs = select.value;
      setConnection("connecting", `正在切换周期：${label}`);
      root.dispatchEvent(new CustomEvent("cpt:interval-changed", { detail: { intervalMs: Number(select.value), label } }));
      refreshSelectedSnapshot();
    });
  }

  /** 本地时区的 `datetime-local` 文本（YYYY-MM-DDTHH:mm），与手动输入的解析口径一致。 */
  const toLocalInputValue = (ms) => {
    const pad = (value) => String(value).padStart(2, "0");
    const date = new Date(ms);
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };

  /** 历史区间会话：固定区间后停止自动刷新，避免实时快照把固定窗口冲掉。 */

  function installTimeRange() {
    const startInput = q("[data-testid=range-start]");
    const endInput = q("[data-testid=range-end]");
    const applyButton = q("[data-testid=range-apply]");
    const liveButton = q("[data-testid=range-live]");
    if (!applyButton && !liveButton) return;

    const setRangeStatus = (text) => setText("[data-testid=range-status]", text);
    const setRangeState = (value) => {
      const node = q("[data-testid=range-status]");
      if (node) node.dataset.state = value;
    };

    if (applyButton) {
      applyButton.addEventListener("click", () => {
        const startValue = startInput ? startInput.value : "";
        const endValue = endInput ? endInput.value : "";
        if (!startValue || !endValue) {
          setRangeStatus("请选择开始与结束时间");
          return;
        }
        const startMs = new Date(startValue).getTime();
        const endMs = new Date(endValue).getTime();
        if (!Number.isInteger(startMs) || !Number.isInteger(endMs)) {
          setRangeStatus("时间格式无效");
          return;
        }
        if (startMs >= endMs) {
          setRangeStatus("结束时间必须晚于开始时间");
          return;
        }
        // 必须在发起请求**之前**置位：绘制发生在 loadSnapshot 内部，
        // 晚一步设置会让窗口被默认的「最近 N 根」截断（2026-09-23 实测）。
        const previousRange = state.pinnedRange;
        state.pinnedRange = { startMs, endMs };
        Promise.resolve(refreshSelectedSnapshot({ startMs, endMs })).then((snapshot) => {
          if (!snapshot) {
            state.pinnedRange = previousRange;
            setRangeStatus("区间快照加载失败，区间未固定");
            return;
          }
          setRangeStatus(`历史区间 ${new Date(startMs).toLocaleString()} ~ ${new Date(endMs).toLocaleString()}`);
          setRangeState("pinned");
          stopPolling();
          scheduleDraw();
        });
      });
    }

    if (applyButton) {
      root.querySelectorAll("[data-range-quick]").forEach((button) => {
        button.addEventListener("click", () => {
          const days = Number(String(button.dataset.rangeQuick || "").replace(/d$/, ""));
          if (!Number.isInteger(days) || days <= 0) return;
          const endMs = Math.floor(Date.now() / 60000) * 60000;
          const startMs = endMs - days * 24 * 3600 * 1000;
          if (startInput) startInput.value = toLocalInputValue(startMs);
          if (endInput) endInput.value = toLocalInputValue(endMs);
          applyButton.click();
        });
      });
    }

    if (liveButton) {
      liveButton.addEventListener("click", () => {
        const wasPinned = state.pinnedRange !== null;
        state.pinnedRange = null;
        if (startInput) startInput.value = "";
        if (endInput) endInput.value = "";
        setRangeStatus("实时");
        setRangeState("live");
        // 立即拉一次实时快照，否则画面会停留在历史区间直到下一次轮询（最长 30 秒）。
        if (wasPinned) {
          Promise.resolve(refreshSelectedSnapshot()).then((snapshot) => {
            if (snapshot) scheduleDraw();
          });
        }
        if (state.snapshotUrl) startPolling(state.snapshotUrl);
      });
    }
  }


  function renderReplayControls() {
    // 进度分母取权威全量快照（回放时 state.snapshot 已被切成前缀）
    const full = state.fullSnapshot || state.snapshot;
    const count = full ? asArray(full.candles).length : 0;
    const index = state.replayIndex == null ? 0 : state.replayIndex;
    setText("[data-testid=replay-progress]", `${index} / ${count}`);
    const runtime = full && isObject(full.runtime) ? full.runtime : {};
    setText("[data-testid=replay-window-size]", runtime.window_size == null ? count : runtime.window_size);
    setState(setText("[data-testid=replay-truncated]", runtime.truncated === true ? "true" : "false"), runtime.truncated === true ? "true" : "false");
    // D4 回放状态机已接通：play / pause / step / reset / seek 全部可用。
    // - 播放中：只留 pause 可点（step/reset/seek 跳帧会乱）
    // - 非播放中：全部可点；play 在末尾仍可用，点击自动从第 1 根重播
    const isPlaying = state.replayTimer !== null;
    root.querySelectorAll("[data-replay-action]").forEach((button) => {
      const action = button.dataset.replayAction;
      const disabled = isPlaying ? action !== "pause" : count === 0;
      button.disabled = disabled;
      button.setAttribute("aria-disabled", disabled ? "true" : "false");
      if (disabled) {
        button.setAttribute("title", isPlaying ? "回放进行中，先「暂停」" : "等待 snapshot 加载");
      } else {
        const atEnd = index >= count && count > 0;
        const titles = {
          play: atEnd ? "从头重播" : "开始回放",
          pause: "暂停回放",
          step: "前进 1 根 K 线",
          reset: "回到第 1 根 K 线",
          seek: "跳到末根 K 线",
        };
        button.setAttribute("title", titles[action] || "");
      }
    });
  }


  function renderTrendSection(item) {
    const selected = item || null;
    setText("[data-testid=trend-type-kind]", selected ? selected.kind : "—");
    setText("[data-testid=trend-type-direction]", selected ? directionLabel(num(selected.direction)) : "—");
    setText("[data-testid=trend-type-level]", selected ? num(selected.level) : "—");
    setText("[data-testid=trend-type-revision]", "—");
  }


  function renderBiSection(item) {
    const selected = item || null;
    setText("[data-testid=bi-direction]", selected ? directionLabel(num(selected.direction)) : "—");
    setText("[data-testid=bi-start-time]", selected ? formatDateTime(num(selected.start_time)) : "—");
    setText("[data-testid=bi-end-time]", selected ? formatDateTime(num(selected.end_time)) : "—");
    setText("[data-testid=bi-range]", selected ? formatRange(num(selected.low), num(selected.high)) : "—");
  }


  function renderZhongshuSection(item, total) {
    setText("[data-testid=zhongshu-count]", total);
    const selected = item || null;
    setText("[data-testid=zhongshu-range]", selected ? formatRange(num(selected.low), num(selected.high)) : "—");
    setText("[data-testid=zhongshu-level]", selected ? num(selected.level) : "—");
  }


  function renderStructureDefaults(snapshot) {
    const overlays = normalizeOverlays(snapshot && snapshot.overlays);
    const signal = isObject(snapshot) && isObject(snapshot.signal) ? snapshot.signal : null;
    const lastOf = (list) => (list.length ? list[list.length - 1] : null);
    renderTrendSection(lastOf(overlays.trend_types));
    renderBiSection(lastOf(overlays.bis));
    renderZhongshuSection(lastOf(overlays.zhongshus), overlays.zhongshus.length);
    renderSignalSection(signal);
  }

  /* ------------------------------------------------------------- 选中联动 */


  function ensureSelectionSection() {
    const panel = q("[data-testid=structure-panel]");
    if (!panel || q("[data-testid=structure-selection]")) return;
    const section = createHtml("section", {
      "data-testid": "structure-selection",
      class: "cpt-selection",
      "aria-labelledby": "structure-selection-title",
    });
    const title = createHtml("h3", { id: "structure-selection-title" }, "选中结构");
    const status = createHtml("p", {
      "data-testid": "structure-selection-status",
      class: "cpt-selection-status",
      role: "status",
      "aria-live": "polite",
      "data-state": "none",
    });
    const guide = createHtml("p", {
      "data-testid": "structure-selection-guide",
      class: "cpt-selection-guide",
    });
    guide.textContent = "尚未选中结构。点击 K 线上的分型/笔/笔中枢/走势类型后此处显示详情。";
    const list = createHtml("dl", { class: "kv" });
    const rows = [
      ["selection-testid", "selection-kind", "kind"],
      ["selection-testid", "selection-level", "level"],
      ["selection-testid", "selection-direction", "方向"],
      ["selection-testid", "selection-time", "时间"],
      ["selection-testid", "selection-range", "区间"],
      ["selection-testid", "selection-state", "状态"],
      ["selection-testid", "selection-source-ids", "source_ids"],
    ];
    rows.forEach((row) => {
      const wrap = createHtml("div");
      wrap.appendChild(createHtml("dt", {}, row[2]));
      wrap.appendChild(createHtml("dd", { "data-testid": row[1] }, "—"));
      list.appendChild(wrap);
    });
    section.appendChild(title);
    section.appendChild(status);
    section.appendChild(guide);
    section.appendChild(list);
    section.dataset.hasSelection = "false";
    panel.appendChild(section);
  }


  function selectionText(payload) {
    const at = formatDateTime(payload.startTime);
    const until = formatDateTime(payload.endTime);
    const range = formatRange(payload.low, payload.high);
    if (payload.kind === "candle") {
      return `已选中 K 线 ${at} · 收 ${formatPrice(payload.close)}${payload.isClosed ? "" : "（未收盘 alert）"}`;
    }
    if (payload.kind === "fractal") {
      return `已选中${payload.fractalKind === "top" ? "顶" : "底"}分型 · ${at} · ${range}`;
    }
    if (payload.kind === "bi") {
      return `已选中笔 · ${directionLabel(payload.direction)} · ${at} → ${until}`;
    }
    if (payload.kind === "zhongshu") {
      return `已选中笔中枢 · ${range} · ${at} → ${until}`;
    }
    if (payload.kind === "trend_type") {
      return `已选中走势类型 ${payload.trendKind || "—"} · ${at} → ${until}`;
    }
    if (payload.kind === "signal") {
      return `已选中一买信号 · status=${payload.status || "none"} · ${formatPrice(payload.price)}`;
    }
    return `已选中 ${payload.kind}`;
  }

  /* ---- Phase D2：远程运维面板（C1–C7） ---- */

  // fetch 结果不在 snapshot 里（C5/C6/C3/C4 是独立路由），存这里供重渲染；
  // 只由 boot() 与手动刷新按钮触发，**不进 30s 轮询**（避免每轮多打几个请求）。
  const remoteOps = { signalStats: null, watchlist: null, compare: null, multiRun: null, structureEvents: null, structureTimeline: null, llmCalls: null, llmTimer: null, days: "30" };

  const SIGNAL_STATS_REASON_LABELS = { signal_history_unavailable: "信号历史不可用" };
  const STRUCTURE_EVENTS_REASON_LABELS = { structure_event_stream_unavailable: "结构事件流不可用（数据库未就绪）" };
  const STRUCTURE_KIND_LABELS = { bi: "笔", fractal: "分型", zhongshu: "中枢", trend_type: "走势类型" };
  const STRUCTURE_EVENT_TYPE_LABELS = {
    created: "新建",
    updated: "更新",
    confirmed: "确认",
    reclassified: "重分类",
    invalidated: "失效",
    closed: "闭合",
  };
  const WATCHLIST_REASON_LABELS = { pool_unavailable: "上游股票池不可用" };
  const COMPARE_REASON_LABELS = { run_body_unavailable: "运行正文不可用（该 run 未保存数据集）" };
  const MULTI_RUN_REASON_LABELS = { run_body_unavailable: "运行正文不可用（所选 run 未保存数据集）" };
  const WATCH_METRICS_REASON_LABELS = { bars_unavailable: "K 线不可用" };

  const reasonText = (map, reason, fallback = "原因未知") =>
    typeof reason === "string" && reason ? map[reason] || reason : fallback;

  // 路由型面板（C3/C4/C5/C6）走网络：拿不到 reason 时多半是服务不可达 / 响应异常，
  // 这比"原因未知"对用户更有用。
  const NETWORK_FALLBACK = "服务不可达或响应异常";

  /**
   * 统一的 JSON 请求封装：**永不抛**。
   *
   * 返回 `{ok, status, body}`；网络中断 / 非 JSON 正文各自降级成 `status:0` / `body:null`，
   * 调用方据此渲染中文降级文案——后端 404 或服务不可达都不能把异常抛进渲染链。
   */

  async function loadStructureEvents() {
    const params = new URLSearchParams({ limit: "60" });
    const { body } = await requestJson(`${DASHBOARD_BASE()}/structure-events?${params.toString()}`);
    remoteOps.structureEvents = isObject(body) ? body : null;
    renderStructureEvents();
  }


  async function loadStructureTimeline(structureId) {
    const params = new URLSearchParams({ structure_id: structureId, limit: "100" });
    const { body } = await requestJson(
      `${DASHBOARD_BASE()}/structure-events/timeline?${params.toString()}`,
    );
    remoteOps.structureTimeline = isObject(body) ? body : null;
    renderStructureEvents();
  }


  function buildStructureEventRows(events) {
    const list = document.createElement("ol");
    list.className = "event-timeline";
    list.dataset.testid = "structure-event-rows";
    events.forEach((event) => {
      const item = document.createElement("li");
      item.dataset.state = String(event.status || "");
      const payload = isObject(event.payload) ? event.payload : {};
      const kind = String(payload.kind || "—");
      const title = document.createElement("strong");
      title.textContent =
        `${STRUCTURE_EVENT_TYPE_LABELS[event.event_type] || event.event_type || "事件"}` +
        ` · ${STRUCTURE_KIND_LABELS[kind] || kind}`;
      const detail = document.createElement("span");
      detail.textContent = `rev ${event.revision ?? "—"} · ${formatDateTime(num(event.occurred_at))}`;
      const id = document.createElement("button");
      id.type = "button";
      id.className = "cpt-structure-event-id";
      id.textContent = String(event.structure_id || "—");
      id.title = "查看该结构的完整时间线";
      id.addEventListener("click", () => {
        loadStructureTimeline(String(event.structure_id || ""));
      });
      item.append(id, title, detail);
      list.appendChild(item);
    });
    return list;
  }


  function renderStructureEvents() {
    const panel = q("[data-testid=event-panel]");
    if (!panel) return;
    let section = q("[data-testid=structure-events]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "structure-events";
      const heading = document.createElement("h3");
      heading.textContent = "结构事件流（累计）";
      const note = document.createElement("p");
      note.className = "cpt-structure-events-note";
      // 自报口径：这是「发生过多少次变化」，不是「现在有多少个结构」。
      note.textContent = "跨重启累积的事件流；与上方「本轮变化」不是同一个口径。点结构 id 看完整时间线。";
      section.append(heading, note);
      panel.appendChild(section);
    }
    while (section.children.length > 2) section.removeChild(section.lastChild);

    const data = remoteOps.structureEvents;
    if (!isObject(data)) {
      // 从未拉取成功：不占版面。与 signal-stats 的折叠纪律一致。
      section.hidden = true;
      return;
    }
    section.hidden = false;
    if (data.available !== true) {
      const p = document.createElement("p");
      p.dataset.testid = "structure-events-unavailable";
      p.textContent = reasonText(
        STRUCTURE_EVENTS_REASON_LABELS,
        data.reason,
        NETWORK_FALLBACK,
      );
      section.appendChild(p);
      return;
    }
    const events = asArray(data.events);
    if (!events.length) {
      const p = document.createElement("p");
      p.dataset.testid = "structure-events-empty";
      p.textContent = "暂无结构事件（事件只在结构真的变化时产生）";
      section.appendChild(p);
      return;
    }
    section.appendChild(buildStructureEventRows(events));
    section.appendChild(renderStructureTimelineDetail());
  }


  function renderStructureTimelineDetail() {
    const wrap = document.createElement("div");
    wrap.className = "cpt-structure-timeline";
    const data = remoteOps.structureTimeline;
    if (!isObject(data)) return wrap;
    const title = document.createElement("h4");
    title.textContent = `时间线 · ${data.structure_id || "—"}`;
    wrap.appendChild(title);
    if (data.available !== true) {
      const p = document.createElement("p");
      p.textContent = reasonText(
        STRUCTURE_EVENTS_REASON_LABELS,
        data.reason,
        NETWORK_FALLBACK,
      );
      wrap.appendChild(p);
      return wrap;
    }
    const events = asArray(data.events);
    if (!events.length) {
      const p = document.createElement("p");
      p.textContent = "该结构暂无事件记录";
      wrap.appendChild(p);
      return wrap;
    }
    // revision 升序：最早在前 —— 这是「时间线」的读法。
    const list = document.createElement("ol");
    list.className = "event-timeline";
    list.dataset.testid = "structure-timeline-rows";
    events.forEach((event) => {
      const item = document.createElement("li");
      item.dataset.state = String(event.status || "");
      const payload = isObject(event.payload) ? event.payload : {};
      const kind = String(payload.kind || "—");
      const label = document.createElement("strong");
      label.textContent =
        `rev ${event.revision ?? "—"} · ` +
        `${STRUCTURE_EVENT_TYPE_LABELS[event.event_type] || event.event_type || "事件"}` +
        ` · ${STRUCTURE_KIND_LABELS[kind] || kind}`;
      const detail = document.createElement("span");
      detail.textContent = formatDateTime(num(event.occurred_at));
      item.append(label, detail);
      list.appendChild(item);
    });
    wrap.appendChild(list);
    return wrap;
  }

  /* ---------------- C6 自选盯盘列表 ---------------- */


  function renderConfigCompare(snapshot) {
    const panel = q("[data-testid=event-panel]");
    if (!panel) return;
    let section = q("[data-testid=config-compare]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "config-compare";
      section.className = "cpt-config-compare";
      const heading = document.createElement("h3");
      heading.textContent = "配置对比";
      section.appendChild(heading);
      panel.appendChild(section);
    }
    while (section.children.length > 1) section.removeChild(section.lastChild);
    const data = isObject(snapshot) && isObject(snapshot.config_compare) ? snapshot.config_compare : null;
    if (!data) {
      const item = document.createElement("p");
      item.textContent = "当前 snapshot 未提供 config_compare（可在 config_compare.json 中保存对照基准）";
      section.appendChild(item);
      return;
    }
    const list = document.createElement("ul");
    (data.differences || []).forEach((entry) => {
      const row = document.createElement("li");
      row.textContent = `${entry.field}: ${JSON.stringify(entry.left)} → ${JSON.stringify(entry.right)}`;
      list.appendChild(row);
    });
    if (!list.children.length) {
      const empty = document.createElement("li");
      empty.textContent = "无差异（与对照基准完全一致）";
      list.appendChild(empty);
    }
    section.appendChild(list);
  }


  function renderEngineState(snapshot) {
    const panel = q("[data-testid=structure-panel]");
    if (!panel) return;
    let section = q("[data-testid=engine-state]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "engine-state";
      const heading = document.createElement("h3");
      heading.textContent = "引擎内部状态";
      section.appendChild(heading);
      panel.appendChild(section);
    }
    while (section.children.length > 1) section.removeChild(section.lastChild);
    const stateValue = snapshot && isObject(snapshot.engine_state) ? snapshot.engine_state : {};
    const list = document.createElement("dl");
    [["revision", stateValue.revision], ["pending", stateValue.pending_count], ["buffer", stateValue.buffer_size], ["window", stateValue.window_size], ["truncated", stateValue.truncated], ["reason", stateValue.truncation_reason]].forEach(([key, value]) => {
      const wrap = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = String(key);
      const detail = document.createElement("dd");
      detail.textContent = value == null ? "—" : String(value);
      wrap.append(term, detail);
      list.appendChild(wrap);
    });
    section.appendChild(list);
  }


  function renderLevelTree(snapshot) {
    const panel = q("[data-testid=structure-panel]");
    if (!panel) return;
    let section = q("[data-testid=level-tree]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "level-tree";
      const heading = document.createElement("h3");
      heading.textContent = "级别递归";
      section.appendChild(heading);
      panel.appendChild(section);
    }
    while (section.children.length > 1) section.removeChild(section.lastChild);
    const list = document.createElement("ul");
    // C2/C7：优先读后端 level_tree（含 parent_level 链接与元素数）；
    // 缺失或 available=false 时回退到本地从 overlays 现算，保证不空图。
    const backend = isObject(snapshot) && isObject(snapshot.level_tree) ? snapshot.level_tree : null;
    const backendLevels = backend && backend.available === true ? asArray(backend.levels) : [];
    if (backendLevels.length) {
      list.dataset.source = "backend";
      backendLevels.forEach((entry) => {
        const item = document.createElement("li");
        const level = num(entry.level);
        const count = asArray(entry.elements).length;
        const parent = num(entry.parent_level);
        let text = `level ${level === null ? "—" : level} · ${count} elements`;
        if (parent !== null) text += ` · ← 上级 ${parent}`;
        item.textContent = text;
        item.dataset.state = "ok";
        list.appendChild(item);
      });
    } else {
      list.dataset.source = "overlays";
      const items = Object.values(normalizeOverlays(snapshot && snapshot.overlays)).flat();
      const levels = [...new Set(items.map((item) => item.level).filter((level) => Number.isInteger(level)))].sort((a, b) => a - b);
      levels.forEach((level) => {
        const item = document.createElement("li");
        item.textContent = `level ${level} · ${items.filter((entry) => entry.level === level).length} elements`;
        list.appendChild(item);
      });
    }
    if (!list.children.length) list.appendChild(document.createElement("li")).textContent = "暂无级别结构";
    section.appendChild(list);
  }


  function freshnessLabel(ms) {
    if (ms == null) return "—";
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s} 秒前`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} 分钟前`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h} 小时前`;
    const d = Math.floor(h / 24);
    return `${d} 天前`;
  }

  const RADAR_STATUS_RANK = {
    confirmed: 0,
    candidate: 1,
    alert: 2,
    structure_ready: 3,
    invalidated: 4,
    none: 5,
  };


  function sideLabel(side, snapshot) {
    if (side === "cpt") return "本仓 Native";
    const ref = snapshot && snapshot.parity && snapshot.parity.reference;
    const src = ref && typeof ref.source === "string" ? ref.source.trim() : "";
    if (!src || src === "none") return "参照（未启用）";
    return `参照 ${src}`;
  }


  function noteKey(payload) {
    const start = payload && (payload.start_time ?? payload.startTime ?? payload.bar_index ?? "none");
    const level = payload && payload.level != null ? payload.level : "none";
    const kind = payload && payload.kind ? payload.kind : "none";
    const symbol = state.snapshot && state.snapshot.market ? state.snapshot.market.symbol : "unknown";
    return `cpt-note:${symbol}:${level}:${kind}:${start}`;
  }


  function renderLocalNote(payload) {
    const panel = q("[data-testid=structure-panel]");
    if (!panel) return;
    let section = q("[data-testid=local-note]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "local-note";
      section.className = "cpt-local-note";
      const heading = document.createElement("h3");
      heading.textContent = "本地研究注释";
      const input = document.createElement("textarea");
      input.dataset.testid = "local-note-input";
      input.placeholder = "仅保存浏览器本地，不进入数据集或 hash";
      input.rows = 3;
      input.addEventListener("input", () => {
        if (state.selection) localStorage.setItem(noteKey(state.selection), input.value);
      });
      section.append(heading, input);
      panel.appendChild(section);
    }
    const input = q("[data-testid=local-note-input]");
    if (input) input.value = payload ? localStorage.getItem(noteKey(payload)) || "" : "";
  }


  function renderResearchDetails(payload) {
    const panel = q("[data-testid=structure-panel]");
    if (!panel) return;
    let tree = q("[data-testid=provenance-tree]");
    if (!tree) {
      tree = document.createElement("section");
      tree.dataset.testid = "provenance-tree";
      tree.className = "cpt-provenance-tree";
      const heading = document.createElement("h3");
      heading.textContent = "结构溯源";
      tree.appendChild(heading);
      panel.appendChild(tree);
    }
    while (tree.children.length > 1) tree.removeChild(tree.lastChild);
    const list = document.createElement("ul");
    const sources = payload && Array.isArray(payload.sourceIds) ? payload.sourceIds : [];
    if (!sources.length) {
      const empty = document.createElement("li");
      empty.textContent = "未选中结构或当前结构没有 source_ids";
      list.appendChild(empty);
    } else {
      sources.forEach((source) => {
        const item = document.createElement("li");
        item.dataset.sourceId = String(source);
        item.textContent = String(source);
        list.appendChild(item);
      });
    }
    tree.appendChild(list);
    let inspector = q("[data-testid=bar-inspector]");
    if (!inspector) {
      inspector = document.createElement("section");
      inspector.dataset.testid = "bar-inspector";
      inspector.className = "cpt-bar-inspector";
      const heading = document.createElement("h3");
      heading.textContent = "逐根检查器 (B3 trace_containment)";
      inspector.appendChild(heading);
      panel.appendChild(inspector);
    }
    while (inspector.children.length > 1) inspector.removeChild(inspector.lastChild);
    const raw = payload && payload.kind === "candle" ? payload.raw : null;
    const form = document.createElement("div");
    form.className = "cpt-inspect-form";
    const label = document.createElement("label");
    label.textContent = "检查 bar_index: ";
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.dataset.testid = "inspect-bar-index";
    input.placeholder = "0";
    input.value = raw && Number.isInteger(raw.bar_index) ? String(raw.bar_index) : (payload && payload.bar_index != null ? String(payload.bar_index) : "0");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "调用后端 inspect";
    button.dataset.testid = "inspect-run";
    const output = document.createElement("pre");
    output.dataset.testid = "inspect-output";
    output.className = "cpt-inspect-output";
    button.addEventListener("click", async () => {
      const idx = Number(input.value);
      if (!Number.isFinite(idx) || idx < 0) {
        output.textContent = "bar_index 必须是非负整数";
        return;
      }
      output.textContent = "调用 /api/dashboard/inspect 中…";
      const result = await fetchInspect(idx);
      if (!result || result.available === false) {
        output.textContent = `inspect 不可用：${result ? result.reason : "unknown"}`;
        return;
      }
      output.textContent = JSON.stringify(result, null, 2);
    });
    form.append(label, input, button);
    inspector.appendChild(form);
    inspector.appendChild(output);
    const rawList = document.createElement("dl");
    rawList.dataset.testid = "inspect-raw";
    [["open_time", raw && raw.open_time], ["open", raw && raw.open], ["high", raw && raw.high], ["low", raw && raw.low], ["close", raw && raw.close], ["volume", raw && raw.volume], ["is_closed", raw && raw.is_closed]].forEach(([key, value]) => {
      const wrap = document.createElement("div");
      const labelNode = document.createElement("dt");
      labelNode.textContent = key;
      const valueNode = document.createElement("dd");
      valueNode.textContent = value == null ? "—" : String(value);
      wrap.append(labelNode, valueNode);
      rawList.appendChild(wrap);
    });
    inspector.appendChild(rawList);
  }


  function renderSelection() {
    ensureSelectionSection();
    const status = q("[data-testid=structure-selection-status]");
    const guide = q("[data-testid=structure-selection-guide]");
    const list = q("[data-testid=structure-selection] dl");
    const section = q("[data-testid=structure-selection]");
    const payload = state.selection;
    if (!payload) {
      if (status) {
        status.textContent = "未选中：点击图中分型 / 笔 / 中枢 / 走势类型 / K 线查看结构详情。";
        status.setAttribute("data-state", "none");
      }
      if (guide) guide.hidden = false;
      if (list) list.hidden = true;
      if (section) section.dataset.hasSelection = "false";
      [
        "selection-kind",
        "selection-level",
        "selection-direction",
        "selection-time",
        "selection-range",
        "selection-state",
        "selection-source-ids",
      ].forEach((testid) => setText(`[data-testid=${testid}]`, "—"));
      delete root.dataset.selection;
      renderResearchDetails(null);
      return;
    }
    if (guide) guide.hidden = true;
    if (list) list.hidden = false;
    if (section) section.dataset.hasSelection = "true";
    if (status) {
      status.textContent = selectionText(payload);
      status.setAttribute("data-state", payload.state);
    }
    setText("[data-testid=selection-kind]", payload.kind);
    setText("[data-testid=selection-level]", payload.level);
    setText("[data-testid=selection-direction]", directionLabel(payload.direction));
    setText(
      "[data-testid=selection-time]",
      payload.startTime === null && payload.endTime === null
        ? "—"
        : `${formatDateTime(payload.startTime)} → ${formatDateTime(payload.endTime)}`,
    );
    setText("[data-testid=selection-range]", formatRange(payload.low, payload.high));
    setText("[data-testid=selection-state]", payload.state);
    setText("[data-testid=selection-source-ids]", payload.sourceIds.join(" ") || "—");
    root.dataset.selection = payload.kind;
    renderResearchDetails(payload);
    renderEngineState(state.snapshot);
    renderLevelTree(state.snapshot);
    renderLocalNote(payload);
    renderParity(state.snapshot);
    renderParityCharts(state.snapshot);
    renderSignalHistory(state.snapshot);
    renderEventAudit(state.snapshot);

    if (payload.kind === "bi") renderBiSection(payload.raw);
    if (payload.kind === "zhongshu") {
      renderZhongshuSection(payload.raw, normalizeOverlays(state.snapshot && state.snapshot.overlays).zhongshus.length);
    }
    if (payload.kind === "trend_type") renderTrendSection(payload.raw);
    if (payload.kind === "signal") renderSignalSection(payload.raw);
  }


  function selectStructure(node, payload) {
    if (state.selectedNode && state.selectedNode !== node) {
      state.selectedNode.removeAttribute("data-selected");
    }
    state.selectedNode = node;
    state.selection = payload;
    node.setAttribute("data-selected", "true");
    renderSelection();
    scheduleDraw();
    root.dispatchEvent(new CustomEvent("cpt:structure-selected", { detail: payload }));
  }


  function attachHit(node, payload) {
    node.classList.add("cpt-chart-hit");
    node.setAttribute("tabindex", "0");
    node.setAttribute("role", "button");
    node.setAttribute("aria-label", selectionText(payload));
    node.addEventListener("click", () => selectStructure(node, payload));
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
        event.preventDefault();
        selectStructure(node, payload);
      }
    });
    return node;
  }

  /* ------------------------------------------------------------- 图形布局 */

  const timeIndexOf = (candles) => {
    const times = candles.map((bar) => bar.openTime);
    return (ms) => {
      if (ms === null || !times.length) return null;
      if (ms <= times[0]) return 0;
      let low = 0;
      let high = times.length - 1;
      if (ms >= times[high]) return high;
      while (high - low > 1) {
        const middle = (low + high) >> 1;
        if (times[middle] <= ms) low = middle;
        else high = middle;
      }
      // 结构时间落在某根 K 线区间内时吸附到该根 K 线（floor），保证与端点分型对齐。
      return low;
    };
  };


  function refreshLevelSelect(snapshot) {
    const select = q("[data-testid=level-select]");
    if (!select) return;
    const multi = isObject(snapshot) && isObject(snapshot.multi_level) ? snapshot.multi_level : null;
    const available = multi && multi.available === true && isObject(multi.levels) ? multi.levels : null;
    const previous = state.level;
    select.replaceChildren();
    const allOption = document.createElement("option");
    allOption.value = "all";
    allOption.textContent = "全部";
    select.appendChild(allOption);
    let restore = previous;
    if (available) {
      const levels = Object.keys(available)
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value))
        .sort((a, b) => a - b);
      levels.forEach((level) => {
        const entry = available[String(level)] || {};
        const total = (entry.fractals || 0) + (entry.bis || 0) + (entry.zhongshus || 0);
        const option = document.createElement("option");
        option.value = String(level);
        option.textContent = `${level}m · ${total} 元素`;
        select.appendChild(option);
      });
      if (previous !== null && previous !== undefined && !levels.includes(previous)) {
        restore = null;
      }
    }
    select.value = restore === null || restore === undefined ? "all" : String(restore);
    state.level = select.value === "all" ? null : Number(select.value);
    root.dataset.level = state.level === null ? "all" : String(state.level);
  }


  function installLevelFilter() {
    const select = q("[data-testid=level-select]");
    if (!select) return;
    select.addEventListener("change", () => {
      const level = select.value === "all" ? null : Number(select.value);
      state.level = level;
      root.dataset.level = level === null ? "all" : String(level);
      const hasEndpoint = Boolean(snapshotEndpoint());
      if (level !== null && hasEndpoint) {
        setConnection("connecting", `正在加载级别 ${level} 的真实叠加结构…`);
        refreshSelectedSnapshot({ level }).then((result) => {
          if (result) setConnection("live", `级别 ${level} 结构已加载`);
        });
      } else {
        renderStructureDefaults(state.snapshot);
        drawChart();
        setConnection("live", level === null ? "已显示全部级别结构" : `已筛选级别：${level}`);
      }
      root.dispatchEvent(new CustomEvent("cpt:level-changed", { detail: { level } }));
    });
  }


  function renderSignalSection(signal) {
    const item = isObject(signal) ? signal : null;
    const status = item && typeof item.status === "string" ? item.status : "none";
    const statusNode = setText("[data-testid=signal-status]", statusLabel(status));
    setState(statusNode, status);
    setText("[data-testid=signal-status-education]", statusEducation(status));
    setText("[data-testid=signal-divergence-status]", item ? divergenceLabel(item.divergence_status) : "—");
    setText("[data-testid=signal-source-revision]", item ? num(item.source_revision) : "—");
    setText("[data-testid=signal-structure-id]", item ? item.structure_id : "—");
    /* Phase N0：price 正名 */
    if (item && typeof item.price === "number") {
      setText("[data-testid=signal-price]", formatPrice(item.price));
    } else {
      setText("[data-testid=signal-price]", "—");
    }
    /* Phase N0-4：标题动态化（一买/一卖） */
    const titleEl = q("#structure-signal-title");
    if (titleEl) {
      const signalType = item && item.signal_type ? item.signal_type : "first_buy";
      titleEl.textContent = signalTypeLabel(signalType);
    }
    /* Phase N0-4：一卖信号并列展示 */
    renderSignalFirstSellSection(signal);
  }


  function renderSignalFirstSellSection(signal) {
    const panel = q("[data-testid=structure-signal]");
    if (!panel) return;
    let firstSellNode = q("[data-testid=signal-first-sell]");
    const item = isObject(signal) ? signal : null;
    const hasFirstSell = item && item.signal_type === "first_sell";
    if (!firstSellNode) {
      firstSellNode = document.createElement("p");
      firstSellNode.dataset.testid = "signal-first-sell";
      firstSellNode.className = "signal-first-sell";
      panel.appendChild(firstSellNode);
    }
    if (hasFirstSell) {
      firstSellNode.textContent = "当前展示：一卖信号（结构卖点，非交易指令）";
      firstSellNode.hidden = false;
    } else {
      firstSellNode.hidden = true;
    }
  }


  function installSliceExport() {
    const panel = q("[data-testid=event-panel]");
    if (!panel || q("[data-testid=slice-export]")) return;
    const section = document.createElement("section");
    section.dataset.testid = "slice-export";
    const heading = document.createElement("h3");
    heading.textContent = "范围导出";
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "导出当前快照 JSON";
    button.addEventListener("click", () => {
      if (!state.snapshot) {
        showError("snapshot 尚未加载，无法导出");
        return;
      }
      try {
        const payload = JSON.stringify(state.snapshot, null, 2);
        const blob = new Blob([payload], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "cpt-dashboard-snapshot.json";
        link.click();
        URL.revokeObjectURL(url);
        setConnection("live", `已导出当前 snapshot（${asArray(state.snapshot.candles).length} 根 K 线）`);
      } catch (error) {
        showError(`导出失败：${error && error.message ? error.message : String(error)}`);
      }
    });

    // 区间导出（C1）：范围模式快照带 snapshot.range，直接用它预填；没有则留空。
    const rangeControls = document.createElement("div");
    rangeControls.className = "ops-controls";
    const startInput = document.createElement("input");
    startInput.type = "number";
    startInput.dataset.testid = "slice-start-ms";
    startInput.placeholder = "start_ms";
    startInput.setAttribute("aria-label", "区间开始（Unix 毫秒）");
    const endInput = document.createElement("input");
    endInput.type = "number";
    endInput.dataset.testid = "slice-end-ms";
    endInput.placeholder = "end_ms";
    endInput.setAttribute("aria-label", "区间结束（Unix 毫秒）");
    const rangePreview = document.createElement("span");
    rangePreview.className = "panel-subtle";
    rangePreview.dataset.testid = "slice-range-preview";
    rangePreview.textContent = "—";
    const rangeButton = document.createElement("button");
    rangeButton.type = "button";
    rangeButton.dataset.testid = "slice-export-range";
    rangeButton.textContent = "导出所选范围";

    const preview = () => {
      const start = num(startInput.value);
      const end = num(endInput.value);
      rangePreview.textContent =
        start === null || end === null
          ? "—"
          : `${formatDateTime(start)} → ${formatDateTime(end)}`;
    };
    startInput.addEventListener("input", preview);
    endInput.addEventListener("input", preview);

    const hydrateRange = () => {
      const range = isObject(state.snapshot) && isObject(state.snapshot.range) ? state.snapshot.range : null;
      const start = range ? num(range.start_ms) : null;
      const end = range ? num(range.end_ms) : null;
      if (start !== null && end !== null) {
        startInput.value = String(start);
        endInput.value = String(end);
      }
      preview();
    };
    hydrateRange();
    section.dataset.hydrated = state.snapshot ? "true" : "false";

    rangeButton.addEventListener("click", async () => {
      hydrateRange();
      const start = num(startInput.value);
      const end = num(endInput.value);
      if (start === null || end === null) {
        setConnection("degraded", "当前快照未提供区间，请先填入 start_ms / end_ms");
        return;
      }
      if (end < start) {
        setConnection("degraded", "区间非法：end_ms 小于 start_ms");
        return;
      }
      rangeButton.disabled = true;
      const url = `${DASHBOARD_BASE()}/export?start_ms=${start}&end_ms=${end}`;
      const { status, body } = await requestJson(url);
      rangeButton.disabled = false;
      const failure = errorMessage(body);
      if (failure) {
        setConnection("degraded", `区间导出失败：${failure}`);
        return;
      }
      if (!isObject(body) || body.available !== true || !isObject(body.snapshot)) {
        const reason = isObject(body) ? body.reason : null;
        setConnection("degraded", `区间导出失败：${reasonText(EXPORT_REASON_LABELS, reason)}`);
        return;
      }
      try {
        const candles = asArray(body.snapshot.candles).length;
        const blob = new Blob([JSON.stringify(body.snapshot, null, 2)], { type: "application/json" });
        const url2 = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url2;
        link.download = `cpt-dashboard-${start}-${end}-${candles}bars.json`;
        link.click();
        URL.revokeObjectURL(url2);
        setConnection("live", `已导出区间快照（${candles} 根 K 线）`);
      } catch (error) {
        setConnection("degraded", `区间导出失败：${error && error.message ? error.message : String(error)}`);
      }
    });

    rangeControls.append(startInput, endInput, rangeButton, rangePreview);
    section.append(heading, button, rangeControls);
    panel.appendChild(section);
  }

  /* ---------------- C7 盯盘指标 ---------------- */


  function renderWatchMetrics(snapshot) {
    const metrics = isObject(snapshot) && isObject(snapshot.watch_metrics) ? snapshot.watch_metrics : null;
    const note = q("[data-testid=watch-metrics-note]");
    const setNote = (text) => {
      if (!note) return;
      note.hidden = !text;
      if (text) note.textContent = text;
    };
    if (!metrics || metrics.available !== true) {
      ["[data-testid=watch-last-price]", "[data-testid=watch-window-high]", "[data-testid=watch-window-low]", "[data-testid=watch-window-volume]"].forEach(
        (selector) => setText(selector, "—"),
      );
      setState(setText("[data-testid=watch-change-pct]", "—"), "flat");
      setNote(
        metrics
          ? `盯盘指标不可用：${reasonText(WATCH_METRICS_REASON_LABELS, metrics.reason)}`
          : "当前快照未提供 watch_metrics（盯盘指标）。",
      );
      return;
    }
    const lastPrice = num(metrics.last_price);
    const changePct = num(metrics.change_pct);
    setText("[data-testid=watch-last-price]", lastPrice === null ? "—" : formatPrice(lastPrice));
    // 涨跌色跟既有 market-change 口径：>=0 记 "up"（本仓 --color-up 是涨色）。
    setState(
      setText("[data-testid=watch-change-pct]", changePct === null ? "—" : `${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`),
      changePct === null ? "flat" : changePct >= 0 ? "up" : "down",
    );
    const high = num(metrics.window_high);
    const low = num(metrics.window_low);
    const volume = num(metrics.window_volume);
    setText("[data-testid=watch-window-high]", high === null ? "—" : formatPrice(high));
    setText("[data-testid=watch-window-low]", low === null ? "—" : formatPrice(low));
    setText("[data-testid=watch-window-volume]", volume === null ? "—" : formatVolume(volume));
    setNote("");
  }

  /* ---------------- C5 信号统计 ---------------- */


  function buildSignalStatsAggregate() {
    const wrap = document.createElement("div");
    wrap.dataset.testid = "signal-stats-aggregate";
    const daysLabel = document.createElement("label");
    daysLabel.className = "panel-subtle";
    daysLabel.textContent = "统计窗口（天）";
    const days = document.createElement("select");
    days.dataset.testid = "signal-stats-days";
    ["7", "30", "90"].forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = `${value} 天`;
      days.appendChild(option);
    });
    days.value = remoteOps.days;
    days.addEventListener("change", () => {
      remoteOps.days = days.value;
      loadSignalStats();
    });
    wrap.append(daysLabel, days);

    const payload = remoteOps.signalStats;
    const basis = isObject(payload) ? payload.basis : null;
    const explain = document.createElement("p");
    explain.className = "hint";
    explain.dataset.testid = "signal-stats-basis";
    explain.textContent = isObject(payload)
      ? `口径：${basis === "signal_event_transitions" || !basis ? "信号状态跃迁事件" : String(basis)}（近 ${payload.days || remoteOps.days} 天）· 不是"当前若干只票的状态"`
      : "口径：信号状态跃迁事件（尚未拉取）";
    wrap.appendChild(explain);

    if (!isObject(payload)) {
      const p = document.createElement("p");
      p.textContent = "聚合统计尚未加载。";
      wrap.appendChild(p);
      return wrap;
    }
    if (payload.available !== true) {
      const p = document.createElement("p");
      p.textContent = `聚合统计不可用：${reasonText(SIGNAL_STATS_REASON_LABELS, payload.reason, NETWORK_FALLBACK)}`;
      wrap.appendChild(p);
      return wrap;
    }
    const stats = isObject(payload.stats) ? payload.stats : {};
    const list = document.createElement("dl");
    list.className = "kv";
    const row = (key, value) => {
      const item = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = key;
      const detail = document.createElement("dd");
      detail.textContent = value;
      item.append(term, detail);
      list.appendChild(item);
    };
    row("总事件数", stats.total == null ? "—" : String(stats.total));
    const statusCounts = isObject(stats.status_counts) ? stats.status_counts : {};
    Object.keys(statusCounts).forEach((key) => {
      row(`状态 · ${STATUS_LABELS[key] || key}`, String(statusCounts[key]));
    });
    const divergenceCounts = isObject(stats.divergence_counts) ? stats.divergence_counts : {};
    Object.keys(divergenceCounts).forEach((key) => {
      row(`背驰 · ${DIVERGENCE_LABELS[key] || key}`, String(divergenceCounts[key]));
    });
    // status_counts 为空时不显示任何状态行——补一行占位，避免用户以为漏渲染
    if (!Object.keys(statusCounts).length && !Object.keys(divergenceCounts).length) {
      row("分项", "无跃迁事件");
    }
    const rate = num(stats.alert_to_confirmed_rate);
    row("预警→确认率", rate === null ? "—" : `${(rate * 100).toFixed(1)}%`);
    row("失效数", stats.invalidated_count == null ? "—" : String(stats.invalidated_count));
    wrap.appendChild(list);
    return wrap;
  }


  function renderReproducibility(snapshot) {
    const panel = q("[data-testid=event-panel]");
    if (!panel) return;
    let section = q("[data-testid=reproducibility-panel]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "reproducibility-panel";
      section.className = "cpt-reproducibility-panel";
      const heading = document.createElement("h3");
      heading.textContent = "可复现性";
      section.appendChild(heading);
      panel.appendChild(section);
    }
    while (section.children.length > 1) section.removeChild(section.lastChild);
    const list = document.createElement("dl");
    list.className = "kv";
    const data = isObject(snapshot) && isObject(snapshot.reproducibility) ? snapshot.reproducibility : {};
    if (!Object.keys(data).length) {
      const item = document.createElement("p");
      item.textContent = "当前 snapshot 未包含 reproducibility 字段";
      section.appendChild(item);
      return;
    }
    [
      ["config_hash", data.config_hash],
      ["dataset_hash", data.dataset_hash],
      // R34：这里原来写的是 `config_version`，而 reproducibility 里**从来没有**
      // 这个字段（真实键：config_hash / dataset_hash / **rules_version** /
      // schema_version / engine_version / dataset_scope / …）。于是这一行永远
      // 显示「—」，而真正的规则版本号 `rules_version` 前端一个字都没读过。
      ["rules_version", data.rules_version],
      ["schema_version", data.schema_version],
    ].forEach(([key, value]) => {
      const row = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = key;
      const dd = document.createElement("dd");
      dd.textContent = value === undefined || value === null ? "—" : String(value);
      row.append(dt, dd);
      list.appendChild(row);
    });
    section.appendChild(list);
  }


  function renderRuns(snapshot) {
    const panel = q("[data-testid=event-panel]");
    if (!panel) return;
    let section = q("[data-testid=runs-panel]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "runs-panel";
      section.className = "cpt-runs-panel";
      const heading = document.createElement("h3");
      heading.textContent = "数据集运行索引";
      section.appendChild(heading);
      panel.appendChild(section);
    }
    while (section.children.length > 1) section.removeChild(section.lastChild);
    const runs = isObject(snapshot) && Array.isArray(snapshot.runs) ? snapshot.runs : [];
    if (!runs.length) {
      // 空索引直接整块折叠，避免反复展示"暂无"占位
      section.hidden = true;
      return;
    }
    section.hidden = false;
    const list = document.createElement("ul");
    runs.forEach((entry) => {
      const row = document.createElement("li");
      row.textContent = `${entry.run_id || entry.id || "—"} · ${entry.symbol || "—"} · ${entry.dataset_hash || ""} · ${entry.generated_at || entry.created_at || ""}`;
      list.appendChild(row);
    });
    section.appendChild(list);
  }


  function renderSignalStats(snapshot) {
    const panel = q("[data-testid=event-panel]");
    if (!panel) return;
    let section = q("[data-testid=signal-stats]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "signal-stats";
      const heading = document.createElement("h3");
      heading.textContent = "信号统计";
      section.appendChild(heading);
      panel.appendChild(section);
    }
    while (section.children.length > 1) section.removeChild(section.lastChild);
    const signal = snapshot && isObject(snapshot.signal) ? snapshot.signal : null;
    // 只要拿到过 C5 响应（哪怕 available=false）就展示，好把降级原因说清楚；
    // 只有"当前无信号 且 从未拉取"才沿用旧行为整块折叠。
    const hasAggregate = isObject(remoteOps.signalStats);
    if ((!signal || !signal.status) && !hasAggregate) {
      // 当前信号与聚合都为空时整块折叠，避免"暂无信号统计"占位
      section.hidden = true;
      return;
    }
    section.hidden = false;
    if (signal && signal.status) {
      const text = `当前：${statusLabel(signal.status)} · 背驰：${divergenceLabel(signal.divergence_status)}`;
      const summary = document.createElement("p");
      summary.textContent = text;
      section.appendChild(summary);
    }
    section.appendChild(buildSignalStatsAggregate());
  }


  function renderSignalRadar(snapshot) {
    const panel = q("[data-testid=signal-radar]");
    if (!panel) return;
    const summary = snapshot && isObject(snapshot.summary) ? snapshot.summary : {};
    const sig = summary && summary.signal ? summary.signal : null;
    const sigSell = summary && summary.signal_first_sell ? summary.signal_first_sell : null;

    const market = snapshot && isObject(snapshot.market) ? snapshot.market : {};
    const code = market.symbol || "—";
    const entries = [];
    if (sig && sig.status && sig.status !== "none") {
      entries.push({ ...sig, _code: code, _market: "crypto" });
    }
    if (sigSell && sigSell.status && sigSell.status !== "none") {
      entries.push({ ...sigSell, _code: code, _market: "a_share", _source_label: "一卖" });
    }

    /* 排序：confirmed > candidate > alert > structure_ready，同级按新鲜度 */
    entries.sort((a, b) => {
      const ra = RADAR_STATUS_RANK[a.status] ?? 99;
      const rb = RADAR_STATUS_RANK[b.status] ?? 99;
      if (ra !== rb) return ra - rb;
      const ta = a.alert_time ?? a.candidate_time ?? a.confirmed_time ?? 0;
      const tb = b.alert_time ?? b.candidate_time ?? b.confirmed_time ?? 0;
      return tb - ta;
    });

    panel.replaceChildren();
    const heading = document.createElement("h3");
    heading.textContent = "信号雷达";
    panel.appendChild(heading);

    /* 免责声明 */
    const disclaimer = document.createElement("p");
    disclaimer.className = "signal-radar-disclaimer";
    disclaimer.textContent =
      "⚠️ 所有「信号/状态」均为缠论结构术语，不构成投资建议。市场有风险，投资须谨慎。";
    panel.appendChild(disclaimer);

    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "signal-radar-empty";
      empty.textContent = "当前无信号。";
      panel.appendChild(empty);
      return;
    }

    const list = document.createElement("ul");
    list.className = "signal-radar-list";
    entries.forEach((entry) => {
      const li = document.createElement("li");
      li.className = `signal-radar-item signal-status-${entry.status}`;

      const main = document.createElement("div");
      main.className = "signal-radar-main";
      const codeSpan = document.createElement("span");
      codeSpan.className = "signal-radar-code";
      codeSpan.textContent = entry._code || entry.code || "—";
      main.appendChild(codeSpan);
      const typeSpan = document.createElement("span");
      typeSpan.className = "signal-radar-type";
      typeSpan.textContent = signalTypeLabel(entry.signal_type);
      main.appendChild(typeSpan);
      if (entry._source_label) {
        const srcSpan = document.createElement("span");
        srcSpan.className = "signal-radar-source";
        srcSpan.textContent = entry._source_label;
        main.appendChild(srcSpan);
      }
      li.appendChild(main);

      const statusDiv = document.createElement("div");
      statusDiv.className = "signal-radar-status-row";
      const statusSpan = document.createElement("span");
      statusSpan.className = "signal-radar-status";
      statusSpan.textContent = statusLabel(entry.status);
      statusDiv.appendChild(statusSpan);
      const freshSpan = document.createElement("span");
      freshSpan.className = "signal-radar-freshness";
      const lastTime = entry.confirmed_time ?? entry.candidate_time ?? entry.alert_time;
      freshSpan.textContent = freshnessLabel(lastTime ? Date.now() - lastTime : null);
      statusDiv.appendChild(freshSpan);
      li.appendChild(statusDiv);

      if (typeof entry.price === "number") {
        const priceDiv = document.createElement("div");
        priceDiv.className = "signal-radar-price";
        priceDiv.textContent = `收盘价 ${formatPrice(entry.price)}（非买入价）`;
        li.appendChild(priceDiv);
      }

      list.appendChild(li);
    });
    panel.appendChild(list);
  }


  function renderSignalHistory(snapshot) {
    const panel = q("[data-testid=event-panel]");
    if (!panel) return;
    let section = q("[data-testid=signal-history]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "signal-history";
      const heading = document.createElement("h3");
      heading.textContent = "信号历史";
      section.appendChild(heading);
      panel.appendChild(section);
    }
    while (section.children.length > 1) section.removeChild(section.lastChild);
    const signal = snapshot && isObject(snapshot.signal) ? snapshot.signal : null;
    if (!signal || !signal.status) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    const row = document.createElement("p");
    row.textContent = `${signal.signal_id || "signal"} · ${statusLabel(signal.status)} · ${divergenceLabel(signal.divergence_status)}`;
    section.appendChild(row);
    // 信号变化提醒（R21 Phase 4 P1）
    const changeType = snapshot && snapshot.summary && snapshot.summary.signal_change_type;
    if (snapshot && snapshot.summary && snapshot.summary.signal_changed && changeType) {
      const note = document.createElement("p");
      note.className = "signal-change-note";
      note.textContent = `⚡ 信号状态变化: ${statusLabel(changeType)}`;
      section.appendChild(note);
    }
  }

  /**
   * 差异面板两侧的**可读标签**（R45）。
   *
   * 参照侧原来叫 "ORACLE"，有两个问题：
   *   1. **和生产机器同名** —— 看板上看到这标签，第一反应是
   *      「拿本仓和这台机器对比？」，而参照侧**跟那台机器毫无关系**；
   *   2. **不准确** —— 参照侧实际是 **czsc**（腾讯兜底），
   *      payload 写得明明白白：
   *        {"source": "czsc",
   *         "detail": "同批 K 线、czsc 后端 vs 生产后端 NativeChanlunBackend"}
   * ⇒ 名字**取自 payload**：降级到腾讯时标签自己变，不用改代码。
   * 这比在两处硬编码「参照」更准确 —— 硬编码只是把一个错误换成一个笼统词。
   */

  function renderParityCharts(snapshot) {
    const chart = q("[data-testid=parity-chart]");
    if (!chart || !snapshot || !isObject(snapshot.parity)) return;
    const series = ["fractals", "bis", "zhongshus"].flatMap((kind) => {
      const value = snapshot.parity[kind];
      return isObject(value) && Array.isArray(value.items) ? value.items.map((item) => ({ ...item, kind })) : [];
    });
    // 面板主标题也用真实参照名，别写死
    const head = q("#parity-heading");
    if (head) head.textContent = `本仓 Native vs ${sideLabel("oracle", snapshot)}`;
    ["cpt", "oracle"].forEach((side) => {
      const target = q(`[data-testid=parity-chart-${side}]`);
      if (!target) return;
      target.replaceChildren();
      // ⚠️ viewBox 高度原先**写死 150**，而布局是 24 列 × 35 行距 + 半径 7：
      // 元素超过 72 个（3 行）时第 4 行 cy=150，圆心+半径=157 **越界 7px 被裁掉**。
      // 实测 Oracle 上 CPT 侧 84 个点 ⇒ 第 4 行正好被切一半（R45 截图抓到）。
      // ⇒ 高度按实际行数算，别写死。
      const PER_ROW = 24;
      const ROW_H = 35;
      const R = 7;
      const visible = series.filter((item) => item[side] !== null && item[side] !== undefined);
      const rows = Math.max(1, Math.ceil(visible.length / PER_ROW));
      const vbH = 45 + rows * ROW_H + R;
      const svg = createSvg("svg", { class: "cpt-parity-svg", viewBox: `0 0 640 ${vbH}`, role: "img", "aria-label": `${side} parity elements` });
      const title = createSvg("text", { x: 8, y: 18, class: "cpt-parity-title" }, sideLabel(side, snapshot));
      svg.appendChild(title);
      visible.forEach((item, index) => {
        const ref = item[side] || {};
        const x = 12 + (index % PER_ROW) * 26;
        const y = 45 + Math.floor(index / PER_ROW) * ROW_H;
        const status = item.status || "matched";
        const node = createSvg("circle", { cx: x, cy: y, r: R, class: `parity-${status}`, tabindex: "0", role: "button", "data-parity-kind": item.kind, "data-parity-status": status });
        const selectParity = () => {
          root.dataset.paritySelection = `${item.kind}:${status}:${JSON.stringify(ref)}`;
          const detail = q("[data-testid=parity-selection]");
          if (detail) detail.textContent = `${item.kind} · ${status} · ${ref.start_time ?? ref.bar_index ?? "—"}`;
          document.querySelectorAll("[data-parity-selected]").forEach((selected) => selected.removeAttribute("data-parity-selected"));
          node.setAttribute("data-parity-selected", "true");
          const anchor = ref.start_time ?? ref.bar_index ?? null;
          root.dispatchEvent(new CustomEvent("cpt:parity-selected", { detail: { side, kind: item.kind, status, anchor, cpt: item.cpt, oracle: item.oracle } }));
          document.querySelectorAll("[data-parity-anchor]").forEach((element) => element.removeAttribute("data-parity-anchor"));
          document.querySelectorAll(`[data-open-time="${anchor}"]`).forEach((element) => element.setAttribute("data-parity-anchor", "true"));
        };
        node.addEventListener("click", selectParity);
        node.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectParity();
          }
        });
        svg.appendChild(node);
      });
      target.appendChild(svg);
      // 图例：三个状态色是 R45 补的样式（原先三态全是默认黑，等于没配色），
      // 但**没有图例的话颜色是歧义的** —— 观者无从知道红点代表「缺」还是「多」。
      // 放在最后一个图后面，且**同时统计真实数量** —— 数字比颜色更有用。
      // 四态，别只数三个 —— 漏一个就等于「有出入的元素数量对不上」
      // （实测 Oracle 上 mismatched 真有值，R45 第一次只写了三个）。
      const refName = (sideLabel("oracle", snapshot).replace(/^参照\s*/, "") || "参照");
      const counts = { matched: 0, missing: 0, extra: 0, mismatched: 0 };
      visible.forEach((item) => {
        const key = item.status || "matched";
        if (key in counts) counts[key] += 1;
      });
      const legend = document.createElement("div");
      legend.className = "cpt-parity-legend";
      [
        ["matched", "一致"],
        ["mismatched", "有出入"],
        ["missing", `${refName} 缺`],
        ["extra", `${refName} 多`],
      ].forEach(([key, label]) => {
        const chip = document.createElement("span");
        chip.className = "cpt-parity-legend-item";
        const dot = document.createElement("i");
        dot.className = `parity-${key}`;
        chip.appendChild(dot);
        chip.appendChild(document.createTextNode(`${label} ${counts[key]}`));
        legend.appendChild(chip);
      });
      target.appendChild(legend);
    });
  }


  function renderParity(snapshot) {
    const chart = q("[data-testid=parity-chart]");
    if (chart) {
      const parityPresent = snapshot && isObject(snapshot.parity) && Object.values(snapshot.parity).some((value) => isObject(value) && isObject(value.summary));
      chart.hidden = !parityPresent;
      ["cpt", "oracle"].forEach((side) => {
        const target = q(`[data-testid=parity-chart-${side}]`);
        if (target) target.textContent = parityPresent ? `${sideLabel(side, snapshot)} overlay ready` : "";
      });
    }
    const panel = q("[data-testid=event-panel]");
    if (!panel) return;
    let section = q("[data-testid=parity-panel]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "parity-panel";
      section.className = "cpt-parity-panel";
      const heading = document.createElement("h3");
      heading.textContent = "本仓 vs 参照结构";
      section.appendChild(heading);
      panel.appendChild(section);
    }
    while (section.children.length > 1) section.removeChild(section.lastChild);
    const parity = snapshot && isObject(snapshot.parity) ? snapshot.parity : null;
    const summary = document.createElement("p");
    if (!parity) {
      summary.textContent = "当前 snapshot 未提供 parity 结果。";
      section.appendChild(summary);
      return;
    }
    if (parity.available === false) {
      summary.textContent = `参照结构对比未启用：${parity.reason || "unavailable"}`;
      section.appendChild(summary);
      return;
    }
    // R35：**按已知 kind 列表**走，不要遍历 payload 的键 —— payload 顶层除了三个
    // kind 還有 available / reason / reference 这些元数据，遍历它们会渲染出
    // "available: 0 matched" 这种假行。列表与 renderParityCharts 里那份一致。
    const parts = ["fractals", "bis", "zhongshus"].map((kind) => {
      const value = parity[kind];
      const item = isObject(value) && isObject(value.summary) ? value.summary : null;
      if (!item) return null;
      // R35：把「容差内的数值漂移」也说出来 —— 否则 matched 27 会被读成
      // 「27 条完全一样」，而实际上 price 字段有 0.5% 量级的舍入差。
      const drift = Number.isFinite(item.max_drift_pct) && item.max_drift_pct > 0
        ? ` (±${item.max_drift_pct}%)` : "";
      return `${kind}: ${item.matched || 0} matched / ${item.missing || 0} missing / ${item.extra || 0} extra${drift}`;
    }).filter(Boolean);
    const ref = isObject(parity.reference) ? parity.reference : null;
    const refNote = ref && ref.source && ref.source !== "none"
      ? `（参照：${ref.source}${ref.detail ? " · " + ref.detail : ""}）`
      : "";
    summary.textContent = (parts.join(" · ") || "无对比项") + refNote;
    section.appendChild(summary);
  }


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
    // geom / windowStart / xForIndex，画布 B/C 拿去会算出 undefined 的参数
    // （首轮审计实测：当时的画布 D 对 /api/canvas/wbt 发了 start_ms=undefined
    // 的 400 请求；该画布已于 R51 下线，判据本身仍成立）。
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

  function installAlertObserver() {
    const alert = q("[data-testid=realtime-alert]");
    const refresh = q("[data-testid=realtime-refresh]");
    const syncOffline = () => {
      const realtime = root.dataset.runtimeMode === "realtime";
      if (alert) alert.hidden = !realtime;
      if (refresh) refresh.hidden = !realtime;
    };
    installBrowserAlerts(alert);
    root.addEventListener("cpt:realtime-updated", (event) => {
      const detail = event.detail || {};
      if (!alert) return;
      if (detail.triggered) state.lastAlertDetail = detail;
      // 保留最近一次提醒：后端只在状态切换那一轮带上 alerts，
      // 后续轮询 alerts 为空，不能因此把已显示的提醒刷回 idle。
      const shown = state.lastAlertDetail;
      if (shown) {
        alert.textContent = `信号提醒：${shown.currentStatus || "alert"}`;
        alert.dataset.state = "alert";
        if (shown.at != null) alert.setAttribute("data-alert-at", String(shown.at));
      } else {
        alert.textContent = "无新信号提醒";
        alert.dataset.state = "idle";
        alert.removeAttribute("data-alert-at");
      }
    });
    new MutationObserver(syncOffline).observe(root, { attributes: true, attributeFilter: ["data-runtime-mode"] });
    syncOffline();
  }

  // 消费 snapshot.alerts（后端 _RealtimeProvider 仅在信号状态切换那一轮填充）
  // 并转成 ``cpt:realtime-updated`` 事件——这是通知/蜂鸣/文案的唯一触发源。

  function syncAlerts(snapshot) {
    const alerts = snapshot && Array.isArray(snapshot.alerts) ? snapshot.alerts : [];
    const first = alerts.find((item) => isObject(item)) || null;
    root.dispatchEvent(
      new CustomEvent("cpt:realtime-updated", {
        detail: first
          ? {
              triggered: true,
              kind: first.kind || "signal_transition",
              currentStatus: first.status || null,
              previousStatus: first.previous_status || null,
              reason: first.reason || null,
              at: first.at == null ? null : first.at,
            }
          : { triggered: false },
      }),
    );
  }


  function isNotificationSupported() {
    return typeof window !== "undefined" && "Notification" in window;
  }


  function playAlertBeep() {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.45);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.5);
    } catch (error) {
      // 浏览器策略或权限阻止时静默失败；不应阻塞主流程。
      console.warn("playAlertBeep failed", error);
    }
  }


  function installBrowserAlerts(alertNode) {
    if (!alertNode) return;
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.testid = "enable-browser-alerts";
    button.textContent = "启用浏览器通知 + 蜂鸣";
    button.addEventListener("click", async () => {
      // denied 后 JS 再 requestPermission 也无效，避免死循环，直接引导用户去设置
      const live = isNotificationSupported() && typeof window.Notification.permission === "string"
        ? window.Notification.permission
        : "default";
      if (live === "denied") {
        state.notificationPermission = "denied";
        updateBrowserAlertsLabel();
        return;
      }
      if (live === "unsupported") {
        state.notificationPermission = "unsupported";
        updateBrowserAlertsLabel();
        return;
      }
      try {
        const permission = await window.Notification.requestPermission();
        state.notificationPermission = permission;
      } catch (error) {
        state.notificationPermission = "denied";
      }
      updateBrowserAlertsLabel();
    });
    alertNode.appendChild(button);
    if (isNotificationSupported() && typeof window.Notification.permission === "string") {
      state.notificationPermission = window.Notification.permission;
    }
    updateBrowserAlertsLabel();
    root.addEventListener("cpt:realtime-updated", (event) => {
      const detail = event.detail || {};
      if (!detail.triggered) return;
      const title = "CPT 信号变化";
      const body = `${detail.previousStatus || "—"} → ${detail.currentStatus || "alert"}`;
      const signature = `${detail.previousStatus || ""}->${detail.currentStatus || ""}@${detail.at || ""}`;
      if (signature === state.lastAlertSignature) return;
      state.lastAlertSignature = signature;
      playAlertBeep();
      if (isNotificationSupported() && window.Notification.permission === "granted") {
        try {
          new window.Notification(title, { body, tag: "cpt-realtime-alert" });
        } catch (error) {
          console.warn("Notification failed", error);
        }
      }
    });
  }


  function updateBrowserAlertsLabel() {
    const button = q("[data-testid=enable-browser-alerts]");
    if (!button) return;
    const permission = state.notificationPermission;
    if (permission === "granted") {
      button.textContent = "浏览器通知已启用（点击重试蜂鸣）";
      button.setAttribute("title", "点击只重试蜂鸣，不再重复弹授权框");
    } else if (permission === "denied") {
      button.textContent = "浏览器通知已被拒绝 · 请到站点设置开启";
      button.setAttribute("title", "浏览器 Notification.permission 已是 denied，无法用 JS 再弹授权；请到地址栏左侧锁形/站点设置中放行通知权限后刷新页面");
    } else if (permission === "unsupported") {
      button.textContent = "当前环境不支持浏览器通知（蜂鸣仍可用）";
      button.setAttribute("title", "Notification API 不存在；蜂鸣仍可触发");
    } else {
      button.textContent = "启用浏览器通知 + 蜂鸣";
      button.setAttribute("title", "首次点击会弹浏览器授权框；选择「允许」即可收到信号变化通知");
    }
  }


  async function loadSignalStats() {
    const params = new URLSearchParams({ days: remoteOps.days });
    const symbol = isObject(state.snapshot) && isObject(state.snapshot.market) ? state.snapshot.market.symbol : null;
    if (typeof symbol === "string" && /^\d{6}$/.test(symbol)) params.set("code", symbol);
    const { body } = await requestJson(`${DASHBOARD_BASE()}/signal-stats?${params.toString()}`);
    remoteOps.signalStats = isObject(body) ? body : null;
    renderSignalStats(state.snapshot);
  }

  /* ---------------- R27 结构事件流 ---------------- */

  // 与上面 snapshot.events 是**两个不同的东西**，标题必须分开写：
  //
  // - `[data-testid=event-timeline]`（HTML 里的「结构事件」）渲染的是
  //   `snapshot.events` = **本轮** diff 出来的变化。它接了 replay 的时间轴过滤
  //   （见 replayPrefix），所以不能改。
  // - 本函数渲染的是 `cpt_structure_event` 的**累计事件流**，跨重启可比。
  //
  // 为什么必须有后者：R26 实测确认 `snapshot.events` 在**稳态下恒为空** ——
  // 每轮都 diff，而绝大多数轮次结构没变。这不是 bug，是它的口径（「本次算出什么
  // 变化」）。所以光靠它，时间线面板在绝大多数时候只会显示「暂无事件」，而库里
  // 其实已经攒了 700+ 条。
  /* ---------------- R28 LLM 面板 ---------------- */

  // 接口早就有了（`/api/dashboard/llm/calls` + `POST .../llm/explain`），R28 之前
  // 看板上看不到：排查「为什么 LLM 不可用」只能 SSH 上翻 journalctl。
  // `unavailable_reason` / `config` 就是为此回给前端的（`list_calls` 的 docstring
  // 写得很清楚：最费时间的就是分不清「没 enable / 没 key / base_url 写错」）。

  const LLM_STATUS_LABELS = {
    queued: "排队中",
    running: "调用中",
    ok: "成功",
    error: "失败",
    rate_limited: "限流退避中",
    interrupted: "已中断",
  };

  const LLM_REASON_LABELS = {
    llm_disabled: "未启用（设 CPT_LLM_ENABLED=1）",
    llm_missing_api_key: "缺 API key",
    llm_missing_base_url: "缺 base_url",
    llm_missing_model: "缺 model",
  };

  const LLM_TERMINAL = new Set(["ok", "error", "interrupted"]);


  async function loadLlmCalls() {
    const { body } = await requestJson(`${DASHBOARD_BASE()}/llm/calls?limit=20`);
    remoteOps.llmCalls = isObject(body) ? body : null;
    renderLlmPanel();
    // 还有在途任务就继续跟：退避重入可能要等几十秒（cap 默认 60s），
    // 停在 queued/running 上会让用户以为「卡住了」。
    const pending = asArray(remoteOps.llmCalls && remoteOps.llmCalls.calls).some(
      (call) => !LLM_TERMINAL.has(String(call.status || "")),
    );
    window.clearTimeout(remoteOps.llmTimer);
    if (pending) {
      remoteOps.llmTimer = window.setTimeout(() => {
        loadLlmCalls();
      }, 2000);
    }
  }


  function explainSelectedStructure() {
    const selection = state.selection;
    // 只对结构类选中项提供解释；signal 选中项走的是 signal_id，不在这个端点范围内
    if (!isObject(selection) || !isObject(selection.raw)) return;
    if (!["bi", "zhongshu", "trend_type"].includes(String(selection.kind || ""))) return;
    const symbol = isObject(state.snapshot) && isObject(state.snapshot.market)
      ? state.snapshot.market.symbol
      : "";
    if (!/^\d{6}$/.test(String(symbol))) return; // explain 端点是 A 股专用

    const button = q("[data-testid=llm-explain-selected]");
    if (button) button.disabled = true;
    return fetch(safeFetchUrl(`${DASHBOARD_BASE()}/a-share/llm/explain?code=${encodeURIComponent(symbol)}`), {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(selection.raw),
    })
      .then((response) => response.json().catch(() => null))
      .then((body) => {
        if (isObject(body) && body.available === false) {
          setText("[data-testid=llm-submit-note]", `提交失败：${body.reason || "未知原因"}`);
        } else {
          setText(
            "[data-testid=llm-submit-note]",
            `已提交（call_id=${(body && body.call_id) || "—"}），结果会出现在下方列表`,
          );
        }
        return loadLlmCalls();
      })
      .catch((error) => {
        setText("[data-testid=llm-submit-note]", `提交失败：${error && error.message}`);
      })
      .finally(() => {
        if (button) button.disabled = false;
      });
  }


  function renderLlmPanel() {
    const panel = q("[data-testid=event-panel]");
    if (!panel) return;
    let section = q("[data-testid=llm-panel]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "llm-panel";
      const heading = document.createElement("h3");
      heading.textContent = "LLM 调用（R28）";
      const note = document.createElement("p");
      note.className = "cpt-structure-events-note";
      note.textContent =
        "独立 LLM 层的调用审计。规则解释是异步的，提交后按 call_id 在下方列表跟踪。";
      const toolbar = document.createElement("div");
      toolbar.className = "cpt-llm-toolbar";
      const refresh = document.createElement("button");
      refresh.type = "button";
      refresh.dataset.testid = "llm-refresh";
      refresh.textContent = "刷新";
      refresh.addEventListener("click", () => loadLlmCalls());
      const explain = document.createElement("button");
      explain.type = "button";
      explain.dataset.testid = "llm-explain-selected";
      explain.textContent = "解释选中的结构";
      explain.addEventListener("click", () => explainSelectedStructure());
      const submitNote = document.createElement("span");
      submitNote.dataset.testid = "llm-submit-note";
      submitNote.className = "cpt-llm-note";
      toolbar.append(refresh, explain, submitNote);
      section.append(heading, note, toolbar);
      panel.appendChild(section);
    }
    // 重建按钮区（状态依赖当前选中项），其余保持
    while (section.children.length > 3) section.removeChild(section.lastChild);

    const data = remoteOps.llmCalls;
    if (!isObject(data)) {
      section.hidden = true;
      return;
    }
    section.hidden = false;

    // 「解释」按钮只在「A 股 + 选中了结构」时可用
    const selection = state.selection;
    const explain = q("[data-testid=llm-explain-selected]");
    if (explain) {
      const kind = isObject(selection) ? String(selection.kind || "") : "";
      const symbol = isObject(state.snapshot) && isObject(state.snapshot.market)
        ? state.snapshot.market.symbol
        : "";
      const usable =
        isObject(selection) &&
        isObject(selection.raw) &&
        ["bi", "zhongshu", "trend_type"].includes(kind) &&
        /^\d{6}$/.test(String(symbol));
      explain.disabled = !usable;
      explain.title = usable
        ? `解释选中的${kind}`
        : "需要：处于 A 股市场模式，且在画布上选中一个笔 / 中枢 / 走势类型";
    }

    // 配置状态行：排查「为什么不可用」的第一现场
    const config = isObject(data.config) ? data.config : {};
    const status = document.createElement("p");
    status.dataset.testid = "llm-config-line";
    const unavailable = typeof data.unavailable_reason === "string" ? data.unavailable_reason : "";
    status.textContent = unavailable
      ? `LLM 不可用：${LLM_REASON_LABELS[unavailable] || unavailable}`
      : `模型 ${config.model || "—"} · ${config.provider || "—"} · 队列中 ${
          Number(data.queued) || 0
        } 个`;
    if (unavailable) status.dataset.state = "blocked";
    section.appendChild(status);

    const calls = asArray(data.calls);
    if (!calls.length) {
      const empty = document.createElement("p");
      empty.dataset.testid = "llm-calls-empty";
      empty.textContent = "暂无调用记录";
      section.appendChild(empty);
      return;
    }
    const list = document.createElement("ol");
    list.className = "cpt-llm-calls";
    calls.forEach((call) => {
      const item = document.createElement("li");
      item.dataset.status = String(call.status || "");
      const head = document.createElement("strong");
      head.textContent = `${LLM_STATUS_LABELS[call.status] || call.status} · ${
        call.purpose || "—"
      }`;
      const meta = document.createElement("span");
      const tokens = call.total_tokens == null ? "" : ` · ${call.total_tokens} tok`;
      const latency = call.latency_ms == null ? "" : ` · ${call.latency_ms}ms`;
      meta.textContent = `${call.subject_id || "—"} · ${formatDateTime(num(call.created_at))}${tokens}${latency}`;
      item.append(head, meta);
      const text = call.result_text || call.error_detail || "";
      if (text) {
        const body = document.createElement("pre");
        body.className = "cpt-llm-text";
        body.textContent = text;
        item.appendChild(body);
      }
      list.appendChild(item);
    });
    section.appendChild(list);
  }


  function renderWatchlistPanel() {
    const rows = q("[data-testid=watchlist-rows]");
    if (!rows) return;
    const note = q("[data-testid=watchlist-note]");
    const asOf = q("[data-testid=watchlist-as-of]");
    const payload = remoteOps.watchlist;
    if (asOf) {
      asOf.textContent = isObject(payload) && typeof payload.as_of === "string" ? `as_of ${payload.as_of}` : "—";
    }
    const setNote = (text) => {
      if (!note) return;
      note.hidden = !text;
      if (text) note.textContent = text;
    };
    if (!isObject(payload)) {
      rows.replaceChildren(objectRow(["加载失败：服务不可达", "", "", "", ""], "watchlist-empty"));
      setNote("自选列表不可用：服务不可达。");
      return;
    }
    if (payload.available !== true) {
      rows.replaceChildren(objectRow(["—", "", "", "", ""], "watchlist-empty"));
      setNote(`自选列表不可用：${reasonText(WATCHLIST_REASON_LABELS, payload.reason, NETWORK_FALLBACK)}`);
      return;
    }
    const entries = asArray(payload.rows);
    if (!entries.length) {
      rows.replaceChildren(objectRow(["（空）", "", "", "", ""], "watchlist-empty"));
      setNote("");
      return;
    }
    rows.replaceChildren(
      ...entries.map((entry) => {
        const available = entry.available === true;
        const cells = [
          entry.symbol == null ? "—" : String(entry.symbol),
          num(entry.last_price) === null ? "—" : formatPrice(num(entry.last_price)),
          num(entry.change_pct) === null ? "—" : `${num(entry.change_pct) >= 0 ? "+" : ""}${num(entry.change_pct).toFixed(2)}%`,
          entry.signal_status ? STATUS_LABELS[entry.signal_status] || String(entry.signal_status) : "—",
          available ? "可点" : "无因子数据",
        ];
        const row = document.createElement("tr");
        row.dataset.testid = "watchlist-row";
        row.dataset.symbol = String(entry.symbol || "");
        row.dataset.state = available ? (entry.alert === true ? "alert" : "ok") : "unavailable";
        cells.forEach((text, index) => {
          const cell = document.createElement("td");
          cell.textContent = text;
          if (index === 2 && num(entry.change_pct) !== null) {
            cell.setAttribute("data-state", num(entry.change_pct) >= 0 ? "up" : "down");
          }
          row.appendChild(cell);
        });
        return row;
      }),
    );
    setNote("");
  }

  const objectRow = (cells, testid) => {
    const row = document.createElement("tr");
    row.dataset.testid = testid;
    cells.forEach((text) => {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.appendChild(cell);
    });
    return row;
  };


  async function loadWatchlist() {
    const { body } = await requestJson(`${DASHBOARD_BASE()}/watchlist`);
    remoteOps.watchlist = isObject(body) ? body : null;
    renderWatchlistPanel();
  }

  /* ---------------- C3 运行对比 ---------------- */

  let runOptionsSignature = null;

  const snapshotRunIds = (snapshot) =>
    asArray(isObject(snapshot) ? snapshot.runs : [])
      .map((entry) => (isObject(entry) ? entry.run_id || entry.id : null))
      .filter((id) => typeof id === "string" && id);


  function renderRunOptions(snapshot) {
    const ids = snapshotRunIds(snapshot);
    const signature = ids.join("|");
    if (signature === runOptionsSignature) return;
    runOptionsSignature = signature;
    ["[data-testid=compare-left]", "[data-testid=compare-right]"].forEach((selector, index) => {
      const node = q(selector);
      if (!node) return;
      const previous = node.value;
      node.replaceChildren(
        ...ids.map((id) => {
          const option = document.createElement("option");
          option.value = id;
          option.textContent = id;
          return option;
        }),
      );
      node.value = ids.includes(previous) ? previous : ids[Math.min(index, Math.max(ids.length - 1, 0))] || "";
    });
    const box = q("[data-testid=multi-run-selects]");
    if (box) {
      if (!ids.length) {
        const span = document.createElement("span");
        span.className = "panel-subtle";
        span.textContent = "当前快照未提供 runs，暂无可对比的运行。";
        box.replaceChildren(span);
      } else {
        box.replaceChildren(
          ...ids.map((id) => {
            const label = document.createElement("label");
            const input = document.createElement("input");
            input.type = "checkbox";
            input.value = id;
            input.dataset.testid = "multi-run-id";
            const span = document.createElement("span");
            span.textContent = id;
            label.append(input, span);
            return label;
          }),
        );
      }
    }
  }

  // `{__summary__: "candles", count: 100, hash: "ab12.."}` 必须专门渲染，
  // 不能 JSON.stringify 出一坨——后端对序列型字段就是降级成这个形状的。

  function formatCompareValue(value) {
    if (value === null || value === undefined) return "—";
    if (isObject(value) && typeof value.__summary__ === "string") {
      const count = value.count == null ? "?" : value.count;
      const hash = typeof value.hash === "string" && value.hash ? ` · hash ${value.hash}` : "";
      return `${value.__summary__} · ${count} 项${hash}`;
    }
    if (isObject(value) || Array.isArray(value)) return JSON.stringify(value);
    return String(value);
  }


  function renderCompareResult() {
    const rows = q("[data-testid=compare-rows]");
    if (!rows) return;
    const note = q("[data-testid=compare-note]");
    const setNote = (text) => {
      if (!note) return;
      note.hidden = !text;
      if (text) note.textContent = text;
    };
    const payload = remoteOps.compare;
    if (!isObject(payload)) {
      rows.replaceChildren(objectRow(["尚未对比", "", ""], "compare-empty"));
      setNote("");
      return;
    }
    const failure = errorMessage(payload);
    if (failure) {
      rows.replaceChildren(objectRow(["对比失败", "", ""], "compare-empty"));
      setNote(`对比失败：${failure}`);
      return;
    }
    if (payload.available !== true) {
      rows.replaceChildren(objectRow(["—", "", ""], "compare-empty"));
      setNote(`对比不可用：${reasonText(COMPARE_REASON_LABELS, payload.reason, NETWORK_FALLBACK)}`);
      return;
    }
    const leftHash = typeof payload.left_dataset_hash === "string" ? payload.left_dataset_hash : "";
    const rightHash = typeof payload.right_dataset_hash === "string" ? payload.right_dataset_hash : "";
    setNote(
      leftHash && rightHash && leftHash === rightHash
        ? `两份运行数据集一致（dataset_hash ${leftHash}）`
        : "",
    );
    const differences = asArray(payload.differences);
    if (!differences.length) {
      rows.replaceChildren(objectRow(["无差异", "—", "—"], "compare-empty"));
      return;
    }
    rows.replaceChildren(
      ...differences.map((entry) => {
        const row = document.createElement("tr");
        const field = document.createElement("td");
        field.textContent = String(entry.field || "—");
        const left = document.createElement("td");
        left.textContent = formatCompareValue(entry.left);
        const right = document.createElement("td");
        right.textContent = formatCompareValue(entry.right);
        row.append(field, left, right);
        return row;
      }),
    );
  }


  async function loadRunCompare(left, right) {
    if (!left || !right) {
      remoteOps.compare = { error: { code: "missing_run_id", message: "请先选择两个运行" } };
      renderCompareResult();
      return;
    }
    const { body } = await requestJson(
      `${DASHBOARD_BASE()}/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`,
    );
    remoteOps.compare = isObject(body) ? body : null;
    renderCompareResult();
  }

  /* ---------------- C4 多数据集对比 ---------------- */

  const MULTI_RUN_ROW_LIMIT = 200;


  function renderMultiRunResult() {
    const head = q("[data-testid=multi-run-head]");
    const rows = q("[data-testid=multi-run-rows]");
    if (!head || !rows) return;
    const note = q("[data-testid=multi-run-note]");
    const setNote = (text) => {
      if (!note) return;
      note.hidden = !text;
      if (text) note.textContent = text;
    };
    const payload = remoteOps.multiRun;
    const failure = errorMessage(payload);
    if (failure) {
      rows.replaceChildren(objectRow(["对比失败", ""], "multi-run-empty"));
      setNote(`多数据集对比失败：${failure}`);
      return;
    }
    if (!isObject(payload)) {
      rows.replaceChildren(objectRow(["尚未加载", ""], "multi-run-empty"));
      setNote("");
      return;
    }
    if (payload.available !== true) {
      rows.replaceChildren(objectRow(["—", ""], "multi-run-empty"));
      setNote(`多数据集对比不可用：${reasonText(MULTI_RUN_REASON_LABELS, payload.reason, NETWORK_FALLBACK)}`);
      return;
    }
    const points = asArray(payload.points);
    const runCount = num(payload.run_count) === null ? 0 : num(payload.run_count);
    const header = document.createElement("tr");
    const timeHead = document.createElement("th");
    timeHead.textContent = "时间";
    header.appendChild(timeHead);
    for (let index = 0; index < runCount; index += 1) {
      const cell = document.createElement("th");
      cell.textContent = `run_${index}`;
      header.appendChild(cell);
    }
    head.replaceChildren(header);
    const shown = points.slice(0, MULTI_RUN_ROW_LIMIT);
    rows.replaceChildren(
      ...shown.map((point) => {
        const row = document.createElement("tr");
        const time = document.createElement("td");
        time.textContent = formatDateTime(num(point.open_time));
        row.appendChild(time);
        for (let index = 0; index < runCount; index += 1) {
          const cell = document.createElement("td");
          const candle = isObject(point[`run_${index}`]) ? point[`run_${index}`] : null;
          const close = candle ? num(candle.close) : null;
          cell.textContent = close === null ? "—" : formatPrice(close);
          row.appendChild(cell);
        }
        return row;
      }),
    );
    if (!shown.length) rows.replaceChildren(objectRow(["（无数据点）", ""], "multi-run-empty"));
    setNote(points.length > MULTI_RUN_ROW_LIMIT ? `仅显示前 ${MULTI_RUN_ROW_LIMIT} 行（共 ${points.length} 行）` : "");
  }


  async function loadMultiRun(ids) {
    if (ids.length < 2 || ids.length > 5) {
      remoteOps.multiRun = { error: { code: "invalid_run_ids", message: "请勾选 2–5 个运行" } };
      renderMultiRunResult();
      return;
    }
    const { body } = await requestJson(`${DASHBOARD_BASE()}/multi-run?run_ids=${ids.map(encodeURIComponent).join(",")}`);
    remoteOps.multiRun = isObject(body) ? body : null;
    renderMultiRunResult();
  }


  function installOpsPanels() {
    const watchlistButton = q("[data-testid=watchlist-refresh]");
    if (watchlistButton) {
      watchlistButton.addEventListener("click", () => {
        watchlistButton.disabled = true;
        loadWatchlist().finally(() => {
          watchlistButton.disabled = false;
        });
      });
    }
    const compareButton = q("[data-testid=compare-run]");
    if (compareButton) {
      compareButton.addEventListener("click", () => {
        const left = q("[data-testid=compare-left]");
        const right = q("[data-testid=compare-right]");
        compareButton.disabled = true;
        loadRunCompare(left ? left.value : "", right ? right.value : "").finally(() => {
          compareButton.disabled = false;
        });
      });
    }
    const multiButton = q("[data-testid=multi-run-load]");
    if (multiButton) {
      multiButton.addEventListener("click", () => {
        const ids = [...root.querySelectorAll("[data-testid=multi-run-id]")]
          .filter((input) => input.checked)
          .map((input) => input.value);
        multiButton.disabled = true;
        loadMultiRun(ids).finally(() => {
          multiButton.disabled = false;
        });
      });
    }
    loadWatchlist();
    loadSignalStats();
    // R27：结构事件流。同样**不进 30s 轮询** —— 这张表只在结构真变了才追加，
    // 30s 轮一次几乎永远是同一批数据，纯浪费。
    loadStructureEvents();
    // R28：LLM 调用记录。这个**要自己轮询**（不是 30s 那个）—— 提交一次解释后
    // 需要看到状态推进到 ok/error，而退避重入可能要等几十秒。
    loadLlmCalls();
  }


  function renderEventAudit(snapshot) {
    const panel = q("[data-testid=event-panel]");
    if (!panel) return;
    let section = q("[data-testid=event-audit]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "event-audit";
      const heading = document.createElement("h3");
      heading.textContent = "事件审计";
      section.appendChild(heading);
      panel.appendChild(section);
    }
    while (section.children.length > 1) section.removeChild(section.lastChild);
    const events = snapshot && Array.isArray(snapshot.events) ? snapshot.events : [];
    if (!events.length) {
      // 空事件列表整块折叠
      section.hidden = true;
      return;
    }
    section.hidden = false;
    const list = document.createElement("ul");
    events.slice(-20).forEach((event) => {
      const item = document.createElement("li");
      item.textContent = `${event.event_type || "event"} · ${event.structure_id || "—"} · rev ${event.revision ?? "—"}`;
      list.appendChild(item);
    });
    section.appendChild(list);
  }

  /* ---- Phase N1：信号雷达 ---- */

  function renderEvents(snapshot) {
    const timeline = q("[data-testid=event-timeline]");
    if (!timeline) return;
    while (timeline.firstChild) timeline.removeChild(timeline.firstChild);
    const events = snapshot ? asArray(snapshot.events) : [];
    if (!events.length) {
      // 空态必须**重新画出来**，不能靠 index.html 里那个占位 <li>。
      // 原实现是「清空 <ol>（连占位一起删）→ 再对已被摘出文档的占位调
      // setHidden」—— 于是第一次空渲染之后占位就永久消失，面板变成一个
      // 什么解释都没有的空白框。R26 实测 snapshot.events 稳态恒为空，
      // 所以这不是边角情况，而是**常态**。
      //
      // 文案也一并更正：「离线 demo 未提供」是 R26 之前的说法，现在
      // snapshot.events 是「本轮 diff 出的变化」，空是正常的。
      const empty = document.createElement("li");
      empty.dataset.state = "empty";
      empty.textContent = "本轮无结构变化（这是正常状态：结构没变就不会产生新事件）";
      timeline.appendChild(empty);
      return;
    }
    events.forEach((event) => {
      const item = document.createElement("li");
      item.className = "event-item";
      item.dataset.eventType = String(event.event_type || "event");
      const title = document.createElement("strong");
      title.textContent = `${event.event_type || "event"} · ${event.structure_id || "—"}`;
      const detail = document.createElement("span");
      detail.textContent = `revision ${event.revision ?? "—"} · ${formatDateTime(num(event.occurred_at))}`;
      item.append(title, detail);
      timeline.appendChild(item);
    });
  }


  function replayPrefix(index) {
    // 始终从权威全量快照切片（不能用 state.snapshot——回放时它已是前缀）
    const snapshot = state.fullSnapshot || state.snapshot;
    if (!snapshot) return null;
    const candles = asArray(snapshot.candles);
    const prefix = Math.max(0, Math.min(index, candles.length));
    const next = JSON.parse(JSON.stringify(snapshot));
    next.candles = candles.slice(0, prefix);
    next.events = asArray(snapshot.events).filter((event) => Number(event.occurred_at) <= Number(candles[Math.max(0, prefix - 1)]?.open_time ?? Infinity));
    if (next.market) {
      next.market.bar_count = next.candles.length;
      next.market.last_price = next.candles.length ? next.candles[next.candles.length - 1].close : null;
    }
    return next;
  }


  function applyReplay(index) {
    const full = state.fullSnapshot || state.snapshot;
    if (!full) return;
    const count = asArray(full.candles).length;
    state.replayIndex = Math.max(0, Math.min(index, count));
    const next = replayPrefix(state.replayIndex);
    render(next, { replay: true });
  }


  function stopPolling() {
    if (state.pollTimer !== null) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (state.staleTimer !== null) {
      window.clearTimeout(state.staleTimer);
      state.staleTimer = null;
    }
  }


  function startPolling(url, intervalMs = 5000) {
    // A 股日线收盘后不再变，轮询纯属浪费（还会让"数据没变"看起来像卡住）。
    // 用 root.dataset.market 而不是另存一份 state，避免与 market_a_share.js 双头状态。
    if (root.dataset.market === "a_share") {
      state.snapshotUrl = url;
      return null;
    }
    stopPolling();
    state.snapshotUrl = url;
    state.pollTimer = window.setInterval(() => {
      if (state.pinnedRange) return;
      loadSnapshot(url).catch(() => undefined);
    }, Math.max(1000, Number(intervalMs) || 5000));
    return state.pollTimer;
  }


  function stopReplay() {
    if (state.replayTimer !== null) {
      window.clearInterval(state.replayTimer);
      state.replayTimer = null;
    }
  }


  function installReplayControls() {
    root.querySelectorAll("[data-replay-action]").forEach((button) => {
      button.addEventListener("click", () => {
        const action = button.dataset.replayAction;
        const full = state.fullSnapshot || state.snapshot;
        const count = full ? asArray(full.candles).length : 0;
        if (action === "play") {
          // 若已到末尾，先回到起点
          if ((state.replayIndex ?? 0) >= count) {
            applyReplay(0);
          }
          stopReplay();
          state.replayTimer = window.setInterval(() => {
            if ((state.replayIndex ?? 0) >= count) {
              stopReplay();
              renderReplayControls();
              return;
            }
            applyReplay((state.replayIndex ?? 0) + 1);
          }, 250);
          renderReplayControls();
        } else if (action === "pause") {
          stopReplay();
          renderReplayControls();
        } else if (action === "step") applyReplay((state.replayIndex ?? 0) + 1);
        else if (action === "reset") applyReplay(0);
        else if (action === "seek") applyReplay(count);
      });
    });
  }

  /* ---- Phase N2：术语即点即懂 ---- */

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
