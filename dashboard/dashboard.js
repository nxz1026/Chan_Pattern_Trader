/*
 * CPT Dashboard · 只读看板前端脚本（D3：K 线 / 成交量 / 缠论结构叠加渲染）
 *
 * 边界（docs/dashboard-plan.md）：
 * - 只消费 dashboard.v1 snapshot，不复制 domain 算法，不新增行情 HTTP 逻辑；
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
 *   window.CPTDashboard.schemaVersion         → "dashboard.v1"
 * 事件：cpt:dashboard-ready / cpt:structure-selected
 *
 * 数据流：snapshot → 归一化 → 像素布局（实测容器尺寸）→ SVG 元素；
 * 所有 DOM 文本走 textContent，禁止把 snapshot 内容拼进 HTML 字符串。
 */

(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const SCHEMA_VERSION = "dashboard.v1";
  const MS_PER_MINUTE = 60000;
  const RUNTIME_STYLE_ID = "cpt-dashboard-runtime-style";

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
  "schema_version": "dashboard.v1",
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

  const state = {
    snapshot: null,
    selection: null,
    selectedNode: null,
    drawPending: false,
    replayIndex: null,
    replayTimer: null,
    pollTimer: null,
    staleTimer: null,
    snapshotUrl: null,
    mode: new URLSearchParams(window.location.search).get("mode") || "research",
    level: null,
    crosshair: null,
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

  const formatAxisTime = (ms) => {
    if (ms === null) return "—";
    const date = new Date(ms);
    const clock = `${pad2(date.getUTCHours())}:${pad2(date.getUTCMinutes())}`;
    return `${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())} ${clock}`;
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

  const formatInterval = (ms) => (ms === null ? "—" : `${formatNumber(ms / MS_PER_MINUTE, 0)}m`);

  const directionLabel = (direction) => {
    if (direction === 1) return "向上（+1）";
    if (direction === -1) return "向下（-1）";
    if (direction === 0) return "未定（0）";
    return "—";
  };

  const directionState = (direction) => (direction === 1 ? "up" : direction === -1 ? "down" : "flat");

  const trendLabel = (item) => {
    const kind = typeof item.kind === "string" ? item.kind : "—";
    return item.direction === 1 ? `${kind} ↑` : item.direction === -1 ? `${kind} ↓` : kind;
  };

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
    return {
      fractals: structures(source.fractals),
      bis: structures(source.bis),
      zhongshus: structures(source.zhongshus),
      trend_types: structures(source.trend_types),
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

  function renderChrome(snapshot) {
    const market = isObject(snapshot) && isObject(snapshot.market) ? snapshot.market : {};
    const quality = isObject(snapshot) && isObject(snapshot.data_quality) ? snapshot.data_quality : {};
    const runtime = isObject(snapshot) && isObject(snapshot.runtime) ? snapshot.runtime : {};
    const candles = normalizeCandles(snapshot && snapshot.candles);

    root.querySelectorAll("[data-field]").forEach((node) => {
      node.textContent = formatFieldValue(node.dataset.field, resolvePath(snapshot, node.dataset.field));
    });

    const firstOpen = num(market.first_open_time);
    const lastOpen = num(market.last_open_time);
    setText("[data-testid=topbar-updated-at]", formatDateTime(lastOpen));
    setText("[data-testid=market-time-range]", firstOpen === null || lastOpen === null
      ? "—"
      : `${formatDateTime(firstOpen)} → ${formatDateTime(lastOpen)}`);
    setText("[data-testid=market-bar-count]", candles.length);
    setText("[data-testid=replay-schema-version]", (isObject(snapshot) && snapshot.schema_version) || SCHEMA_VERSION);

    const stale = quality.stale === true;
    const gap = quality.gap === true;
    setState(setText("[data-testid=data-quality-stale]", stale ? "true" : "false"), stale ? "true" : "false");
    setState(setText("[data-testid=data-quality-gap]", gap ? "true" : "false"), gap ? "true" : "false");

    const lastPrice = num(market.last_price);
    setText("[data-testid=market-last-price]", lastPrice === null ? "—" : formatPrice(lastPrice));
    const last = candles.length ? candles[candles.length - 1] : null;
    const first = candles.length ? candles[0] : null;
    const changeNode = q("[data-testid=market-change]");
    if (changeNode) {
      const change = first && first.open ? ((last.close - first.open) / first.open) * 100 : null;
      changeNode.textContent = change === null ? "—" : `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`;
      changeNode.dataset.state = change === null ? "flat" : change >= 0 ? "up" : "down";
    }
    const countdownNode = q("[data-testid=close-countdown]");
    if (countdownNode && last) {
      const interval = num(market.interval_ms) || 300000;
      const remaining = Math.max(0, last.openTime + interval - Date.now());
      countdownNode.textContent = `${Math.floor(remaining / 60000)}m ${Math.floor((remaining % 60000) / 1000)}s`;
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
    const totalVolume = candles.reduce((sum, bar) => sum + (bar.volume === null ? 0 : bar.volume), 0);
    const volumeNode = setText("[data-testid=market-volume]", candles.length ? formatVolume(totalVolume) : "—");
    if (volumeNode && candles.length) {
      volumeNode.setAttribute("title", "当前 snapshot 窗口累计成交量（非 24h）");
    }

    let stateKey = "empty";
    if (candles.length) {
      if (gap) stateKey = "gap";
      else if (stale) stateKey = "stale";
      else stateKey = runtime.status === "alert" ? "alert" : "confirmed";
    }
    setState(setText("[data-testid=topbar-status]", stateKey), stateKey);
    setHidden("[data-testid=state-empty]", candles.length > 0);
    setHidden("[data-testid=state-stale]", !(candles.length && stale));
    setHidden("[data-testid=state-gap]", !(candles.length && gap));

    root.dataset.status = stateKey;
    root.dataset.mode = typeof runtime.mode === "string" ? runtime.mode : "offline";
    root.dataset.dataSource = typeof runtime.data_source === "string" ? runtime.data_source : "unknown";

    if (!candles.length) {
      setConnection("offline", emptyMessage(snapshot));
    } else if (typeof runtime.mode === "string" && runtime.mode !== "realtime") {
      setConnection("offline", `离线 snapshot（${candles.length} 根 K 线）· 无网络请求`);
    } else {
      setConnection("live", `已渲染 snapshot（${candles.length} 根 K 线，mode=${root.dataset.mode}）`);
    }
  }

  /* ---------------------------------------------------------- 结构面板填充 */

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

  function renderSignalSection(signal) {
    const item = isObject(signal) ? signal : null;
    const status = item && typeof item.status === "string" ? item.status : "none";
    setState(setText("[data-testid=signal-status]", status), status);
    setText("[data-testid=signal-divergence-status]", item ? item.divergence_status : "—");
    setText("[data-testid=signal-source-revision]", item ? num(item.source_revision) : "—");
    setText("[data-testid=signal-structure-id]", item ? item.structure_id : "—");
    setText(
      "[data-testid=signal-source-ids]",
      item ? asArray(item.source_ids).join(" ") || "—" : "—",
    );
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
    section.appendChild(list);
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
      const payload = JSON.stringify(state.snapshot || {}, null, 2);
      const blob = new Blob([payload], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "cpt-dashboard-snapshot.json";
      link.click();
      URL.revokeObjectURL(url);
    });
    section.append(heading, button);
    panel.appendChild(section);
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
    const text = signal ? `当前：${signal.status || "unknown"} · 背驰：${signal.divergence_status || "unknown"}` : "暂无信号统计";
    const summary = document.createElement("p");
    summary.textContent = text;
    section.appendChild(summary);
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
    const items = Object.values(normalizeOverlays(snapshot && snapshot.overlays)).flat();
    const levels = [...new Set(items.map((item) => item.level).filter((level) => Number.isInteger(level)))].sort((a, b) => a - b);
    const list = document.createElement("ul");
    levels.forEach((level) => {
      const item = document.createElement("li");
      item.textContent = `level ${level} · ${items.filter((entry) => entry.level === level).length} elements`;
      list.appendChild(item);
    });
    if (!list.children.length) list.appendChild(document.createElement("li")).textContent = "暂无级别结构";
    section.appendChild(list);
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
    const list = document.createElement("ul");
    events.slice(-20).forEach((event) => {
      const item = document.createElement("li");
      item.textContent = `${event.event_type || "event"} · ${event.structure_id || "—"} · rev ${event.revision ?? "—"}`;
      list.appendChild(item);
    });
    if (!list.children.length) list.appendChild(document.createElement("li")).textContent = "暂无事件";
    section.appendChild(list);
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
    const row = document.createElement("p");
    row.textContent = signal ? `${signal.signal_id || "signal"} · ${signal.status || "none"} · ${signal.divergence_status || "—"}` : "暂无信号历史";
    section.appendChild(row);
  }

  function renderParityCharts(snapshot) {
    const chart = q("[data-testid=parity-chart]");
    if (!chart || !snapshot || !isObject(snapshot.parity)) return;
    const series = ["fractals", "bis", "zhongshus"].flatMap((kind) => {
      const value = snapshot.parity[kind];
      return isObject(value) && Array.isArray(value.items) ? value.items.map((item) => ({ ...item, kind })) : [];
    });
    ["cpt", "oracle"].forEach((side) => {
      const target = q(`[data-testid=parity-chart-${side}]`);
      if (!target) return;
      target.replaceChildren();
      const svg = createSvg("svg", { class: "cpt-parity-svg", viewBox: "0 0 640 150", role: "img", "aria-label": `${side} parity elements` });
      const title = createSvg("text", { x: 8, y: 18, class: "cpt-parity-title" }, side.toUpperCase());
      svg.appendChild(title);
      const visible = series.filter((item) => item[side] !== null && item[side] !== undefined);
      visible.forEach((item, index) => {
        const ref = item[side] || {};
        const x = 12 + (index % 24) * 26;
        const y = 45 + Math.floor(index / 24) * 35;
        const status = item.status || "matched";
        const node = createSvg("circle", { cx: x, cy: y, r: 7, class: `parity-${status}`, tabindex: "0", role: "button", "data-parity-kind": item.kind, "data-parity-status": status });
        const selectParity = () => {
          root.dataset.paritySelection = `${item.kind}:${status}:${JSON.stringify(ref)}`;
          const detail = q("[data-testid=parity-selection]");
          if (detail) detail.textContent = `${item.kind} · ${status} · ${ref.start_time ?? ref.bar_index ?? "—"}`;
          document.querySelectorAll("[data-parity-selected]").forEach((selected) => selected.removeAttribute("data-parity-selected"));
          node.setAttribute("data-parity-selected", "true");
          root.dispatchEvent(new CustomEvent("cpt:parity-selected", { detail: { side, kind: item.kind, status, cpt: item.cpt, oracle: item.oracle } }));
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
    });
  }

  function renderParity(snapshot) {
    const chart = q("[data-testid=parity-chart]");
    if (chart) {
      const parityPresent = snapshot && isObject(snapshot.parity) && Object.values(snapshot.parity).some((value) => isObject(value) && isObject(value.summary));
      chart.hidden = !parityPresent;
      ["cpt", "oracle"].forEach((side) => {
        const target = q(`[data-testid=parity-chart-${side}]`);
        if (target) target.textContent = parityPresent ? `${side.toUpperCase()} overlay ready` : "";
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
      heading.textContent = "Oracle 对比";
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
    const parts = Object.entries(parity).map(([kind, value]) => {
      const item = isObject(value) && isObject(value.summary) ? value.summary : {};
      return `${kind}: ${item.matched || 0} matched / ${item.missing || 0} missing / ${item.extra || 0} extra`;
    });
    summary.textContent = parts.join(" · ") || "无对比项";
    section.appendChild(summary);
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
        if (state.selection) localStorage.setItem(`cpt-note:${state.selection.kind}:${state.selection.sourceIds.join(",")}`, input.value);
      });
      section.append(heading, input);
      panel.appendChild(section);
    }
    const input = q("[data-testid=local-note-input]");
    if (input) input.value = payload ? localStorage.getItem(`cpt-note:${payload.kind}:${payload.sourceIds.join(",")}`) || "" : "";
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
      heading.textContent = "逐根检查器";
      inspector.appendChild(heading);
      panel.appendChild(inspector);
    }
    while (inspector.children.length > 1) inspector.removeChild(inspector.lastChild);
    const raw = payload && payload.kind === "candle" ? payload.raw : null;
    const rawList = document.createElement("dl");
    [["open_time", raw && raw.open_time], ["open", raw && raw.open], ["high", raw && raw.high], ["low", raw && raw.low], ["close", raw && raw.close], ["volume", raw && raw.volume], ["is_closed", raw && raw.is_closed]].forEach(([key, value]) => {
      const wrap = document.createElement("div");
      const label = document.createElement("dt");
      label.textContent = key;
      const valueNode = document.createElement("dd");
      valueNode.textContent = value == null ? "—" : String(value);
      wrap.append(label, valueNode);
      rawList.appendChild(wrap);
    });
    inspector.appendChild(rawList);
  }

  function renderSelection() {
    ensureSelectionSection();
    const status = q("[data-testid=structure-selection-status]");
    const payload = state.selection;
    if (!payload) {
      if (status) {
        status.textContent = "未选中：点击图中分型 / 笔 / 中枢 / 走势类型 / K 线查看结构详情。";
        status.setAttribute("data-state", "none");
      }
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

  /** 结构是否落在未收盘区间：snapshot 标记 alert 且结构覆盖了最后一根 K 线。 */
  const structureStateOf = (view, endTime) => {
    const runtime = isObject(state.snapshot) && isObject(state.snapshot.runtime) ? state.snapshot.runtime : {};
    if (runtime.status !== "alert") return "confirmed";
    return endTime !== null && endTime >= view.lastOpenTime ? "alert" : "confirmed";
  };

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

  const trendFill = (direction) =>
    direction === 1
      ? "rgba(22, 199, 132, 0.07)"
      : direction === -1
        ? "rgba(234, 57, 67, 0.07)"
        : "rgba(240, 185, 11, 0.07)";

  function drawTrendBackgrounds(group, view) {
    view.overlays.trend_types.forEach((item) => {
      const startIndex = view.indexForTime(num(item.start_time));
      const endIndex = view.indexForTime(num(item.end_time));
      if (startIndex === null || endIndex === null) return;
      const left = view.xForIndex(Math.min(startIndex, endIndex)) - view.geom.slot / 2;
      const width = Math.max(2, Math.abs(endIndex - startIndex) * view.geom.slot + view.geom.slot);
      const element = createSvg("rect", {
        x: left,
        y: view.geom.top,
        width,
        height: view.geom.plotHeight,
        "data-structure-kind": "trend_type",
        "data-trend-kind": item.kind,
        "data-level": num(item.level),
        "data-state": structureStateOf(view, num(item.end_time)),
        "data-source-ids": asArray(item.source_ids).join(" "),
      });
      paint(element, {
        fill: trendFill(num(item.direction)),
        stroke: "var(--color-accent)",
        "stroke-opacity": "0.3",
        "stroke-dasharray": "3 3",
      });
      group.appendChild(attachHit(element, payloadFor("trend_type", item, view, structureStateOf(view, num(item.end_time)))));
      group.appendChild(
        paint(createSvg("text", { x: left + 4, y: view.geom.top + 11 }, `走势类型 ${trendLabel(item)}`), {
          fill: "var(--color-accent)",
        }),
      );
    });
  }

  function drawZhongshus(group, view) {
    view.overlays.zhongshus.forEach((item) => {
      const startIndex = view.indexForTime(num(item.start_time));
      const endIndex = view.indexForTime(num(item.end_time));
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
        "data-state": structureStateOf(view, num(item.end_time)),
        "data-source-ids": asArray(item.bi_ids).join(" "),
      });
      paint(element, {
        fill: "rgba(76, 155, 232, 0.15)",
        stroke: "var(--color-loading)",
        "stroke-width": "1",
        "stroke-dasharray": "4 3",
      });
      group.appendChild(attachHit(element, payloadFor("zhongshu", item, view, structureStateOf(view, num(item.end_time)))));
      group.appendChild(
        paint(createSvg("text", { x: left + 4, y: top - 4 }, `中枢 L${num(item.level)} · ${formatPrice(high)}`), {
          fill: "var(--color-loading)",
        }),
      );
      group.appendChild(
        createSvg("text", { x: left + 4, y: top + height + 11 }, `中枢下沿 ${formatPrice(low)}`),
      );
    });
  }

  function drawCandles(group, view) {
    const bodyWidth = Math.max(1, Math.min(view.geom.slot * 0.62, 16));
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
      const element = createSvg("path", {
        d: `M ${view.xForIndex(startIndex)} ${view.yForPrice(startPrice)} L ${view.xForIndex(endIndex)} ${view.yForPrice(endPrice)}`,
        fill: "none",
        "data-structure-kind": "bi",
        "data-direction": direction,
        "data-level": num(item.level),
        "data-state": stateValue,
        "data-source-ids": asArray(item.source_ids).join(" "),
      });
      paint(element, {
        stroke: up ? "var(--color-up)" : "var(--color-down)",
        "stroke-width": "2",
        "stroke-linecap": "round",
        "stroke-dasharray": stateValue === "alert" ? "5 3" : "none",
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
    group.appendChild(
      paint(createSvg("text", { x: view.xForIndex(index), y: y - 4 }, `一买 ${stateValue} · ${formatPrice(price)}`), {
        fill: stateValue === "confirmed" ? "var(--color-confirmed)" : "var(--color-alert)",
      }),
    );
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

  function drawAxis(axisNode, view, axisHeight) {
    const svg = createSvg("svg", {
      class: "cpt-chart-svg",
      viewBox: `0 0 ${view.geom.width} ${axisHeight}`,
      preserveAspectRatio: "none",
      role: "presentation",
    });
    const group = createSvg("g", { class: "cpt-chart-axis" });
    svg.appendChild(group);
    const step = Math.max(1, Math.ceil(view.candles.length / 6));
    for (let index = step - 1; index < view.candles.length; index += step) {
      group.appendChild(
        createSvg("text", {
          x: view.xForIndex(index),
          y: axisHeight - 9,
          "text-anchor": "middle",
          "data-bar-index": index,
          "data-open-time": view.candles[index].openTime,
        }, formatAxisTime(view.candles[index].openTime)),
      );
    }
    axisNode.dataset.rendered = "true";
    axisNode.appendChild(svg);
  }

  function drawChart() {
    const canvas = q("[data-testid=chart-canvas-region]");
    const volumeNode = q("[data-testid=chart-volume-region]");
    const axisNode = q("[data-testid=chart-time-axis]");
    if (!canvas || !volumeNode || !axisNode) return;

    const snapshot = state.snapshot;
    const candles = normalizeCandles(snapshot && snapshot.candles);
    const overlays = normalizeOverlays(snapshot && snapshot.overlays);
    const domain = priceDomain(candles, overlays);
    const rect = canvas.getBoundingClientRect();
    const width = Math.round(rect.width);
    const height = Math.round(rect.height);

    if (!candles.length || !domain || width < 40 || height < 40) {
      clearRegion(canvas);
      clearRegion(volumeNode);
      clearRegion(axisNode);
      appendNote(
        canvas,
        !candles.length
          ? "暂无 K 线：等待 dashboard.v1 snapshot（empty）"
          : "图形区尚未完成布局，等待下一次重绘",
      );
      appendNote(volumeNode, "暂无成交量数据");
      appendNote(axisNode, "时间轴：无数据（Unix 毫秒）");
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", "K 线绘制区占位：分型、笔、中枢、走势类型由 dashboard.js 叠加渲染");
      return;
    }

    const geom = layoutPlot(width, height, candles.length);
    const view = {
      candles,
      overlays,
      geom,
      domain,
      lastOpenTime: candles[candles.length - 1].openTime,
      indexForTime: timeIndexOf(candles),
      xForIndex: (index) => geom.left + (index + 0.5) * geom.slot,
      yForPrice: (price) =>
        geom.top + ((domain.max - price) / (domain.max - domain.min)) * geom.plotHeight,
    };

    clearRegion(canvas);
    clearRegion(volumeNode);
    clearRegion(axisNode);
    canvas.dataset.rendered = "true";
    canvas.setAttribute("role", "group");
    canvas.setAttribute("aria-label", "K 线与缠论结构叠加图：分型、笔、中枢、走势类型可点击查看结构详情");

    const svg = createSvg("svg", {
      class: "cpt-chart-svg",
      viewBox: `0 0 ${width} ${height}`,
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
    drawAxis(axisNode, view, Math.max(20, Math.round(axisRect.height) || 28));

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

  function installSymbolSwitch() {
    const select = q("[data-testid=symbol-select]");
    if (!select) return;
    select.addEventListener("change", () => {
      const symbol = select.value;
      setText("[data-testid=topbar-symbol]", symbol);
      root.dataset.symbol = symbol;
      root.dispatchEvent(new CustomEvent("cpt:symbol-changed", { detail: { symbol } }));
    });
  }

  function installLevelFilter() {
    const select = q("[data-testid=level-select]");
    if (!select) return;
    select.addEventListener("change", () => {
      const level = select.value === "all" ? null : Number(select.value);
      state.level = level;
      root.dataset.level = level === null ? "all" : String(level);
      renderStructureDefaults(state.snapshot);
      drawChart();
      root.dispatchEvent(new CustomEvent("cpt:level-changed", { detail: { level } }));
    });
  }

  function installIntervalSwitch() {
    const select = q("[data-testid=interval-select]");
    if (!select) return;
    select.addEventListener("change", () => {
      const labels = { "60000": "1m", "300000": "5m", "900000": "15m", "3600000": "1h" };
      const label = labels[select.value] || select.value;
      setText("[data-testid=topbar-interval]", label);
      root.dataset.intervalMs = select.value;
      root.dispatchEvent(new CustomEvent("cpt:interval-changed", { detail: { intervalMs: Number(select.value), label } }));
    });
  }

  function installModeSwitch() {
    root.dataset.mode = state.mode;
    root.querySelectorAll("[data-mode-action]").forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.modeAction === state.mode ? "true" : "false");
      button.addEventListener("click", () => {
        state.mode = button.dataset.modeAction === "watch" ? "watch" : "research";
        root.dataset.mode = state.mode;
        root.querySelectorAll("[data-mode-action]").forEach((item) => item.setAttribute("aria-pressed", item.dataset.modeAction === state.mode ? "true" : "false"));
        const url = new URL(window.location.href);
        url.searchParams.set("mode", state.mode);
        window.history.replaceState({}, "", url);
        root.dispatchEvent(new CustomEvent("cpt:mode-changed", { detail: { mode: state.mode } }));
      });
    });
  }

  function installCrosshair() {
    const canvas = q("[data-testid=chart-canvas-region]");
    if (!canvas) return;
    const tooltip = document.createElement("div");
    tooltip.className = "cpt-crosshair-tooltip";
    tooltip.hidden = true;
    canvas.appendChild(tooltip);
    canvas.addEventListener("mousemove", (event) => {
      const candles = normalizeCandles(state.snapshot && state.snapshot.candles);
      if (!candles.length) return;
      const rect = canvas.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
      const candle = candles[Math.min(candles.length - 1, Math.floor(ratio * candles.length))];
      tooltip.textContent = `${formatDateTime(candle.openTime)}  O ${formatPrice(candle.open)} H ${formatPrice(candle.high)} L ${formatPrice(candle.low)} C ${formatPrice(candle.close)} V ${formatVolume(candle.volume || 0)}`;
      tooltip.style.left = `${Math.max(4, event.clientX - rect.left + 8)}px`;
      tooltip.style.top = `${Math.max(4, event.clientY - rect.top + 8)}px`;
      tooltip.hidden = false;
    });
    canvas.addEventListener("mouseleave", () => { tooltip.hidden = true; });
  }

  function installRuntimeStyle() {
    if (document.getElementById(RUNTIME_STYLE_ID)) return;
    const style = createHtml("style", { id: RUNTIME_STYLE_ID, "data-owner": "dashboard.js" });
    style.textContent = RUNTIME_CSS;
    document.head.appendChild(style);
  }

  function render(snapshot) {
    state.snapshot = isObject(snapshot) ? snapshot : null;
    state.replayIndex = state.snapshot ? asArray(state.snapshot.candles).length : null;
    renderReplayControls(state.snapshot);
    state.selection = null;
    state.selectedNode = null;
    renderChrome(state.snapshot);
    renderEvents(state.snapshot);
    renderParity(state.snapshot);
    renderParityCharts(state.snapshot);
    renderSignalHistory(state.snapshot);
    renderEventAudit(state.snapshot);
    renderSignalStats(state.snapshot);
    installSliceExport();
    renderStructureDefaults(state.snapshot);
    renderEngineState(state.snapshot);
    renderLevelTree(state.snapshot);
    renderSelection();
    drawChart();
    return state.snapshot;
  }

  function renderReplayControls(snapshot) {
    const count = snapshot ? asArray(snapshot.candles).length : 0;
    const index = state.replayIndex == null ? 0 : state.replayIndex;
    setText("[data-testid=replay-progress]", `${index} / ${count}`);
    const runtime = snapshot && isObject(snapshot.runtime) ? snapshot.runtime : {};
    setText("[data-testid=replay-window-size]", runtime.window_size == null ? count : runtime.window_size);
    setState(setText("[data-testid=replay-truncated]", runtime.truncated === true ? "true" : "false"), runtime.truncated === true ? "true" : "false");
    root.querySelectorAll("[data-replay-action]").forEach((button) => {
      button.disabled = count === 0;
    });
  }

  function renderEvents(snapshot) {
    const timeline = q("[data-testid=event-timeline]");
    if (!timeline) return;
    while (timeline.firstChild) timeline.removeChild(timeline.firstChild);
    const events = snapshot ? asArray(snapshot.events) : [];
    setHidden("[data-testid=event-timeline-empty]", events.length > 0);
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
    const snapshot = state.snapshot;
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
    if (!state.snapshot) return;
    state.replayIndex = Math.max(0, Math.min(index, asArray(state.snapshot.candles).length));
    const next = replayPrefix(state.replayIndex);
    render(next);
    renderEvents(next);
    renderReplayControls(state.snapshot);
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
    stopPolling();
    state.snapshotUrl = url;
    state.pollTimer = window.setInterval(() => {
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
        const count = state.snapshot ? asArray(state.snapshot.candles).length : 0;
        if (action === "play") {
          stopReplay();
          state.replayTimer = window.setInterval(() => {
            if ((state.replayIndex ?? 0) >= count) return stopReplay();
            applyReplay((state.replayIndex ?? 0) + 1);
          }, 250);
        } else if (action === "pause") stopReplay();
        else if (action === "step") applyReplay((state.replayIndex ?? 0) + 1);
        else if (action === "reset") applyReplay(0);
        else if (action === "seek") applyReplay(count);
      });
    });
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
    if (visible) setConnection("connecting", "正在读取 dashboard.v1 snapshot…");
  }

  async function loadSnapshot(url) {
    setLoading(true);
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const snapshot = await response.json();
      render(snapshot);
      setHidden("[data-testid=state-error]", true);
      setConnection("live", `已加载 snapshot：${url}`);
      if (state.staleTimer !== null) window.clearTimeout(state.staleTimer);
      state.staleTimer = window.setTimeout(() => {
        root.dataset.connection = "stale";
        setState(setText("[data-testid=topbar-status]", "stale"), "stale");
        setHidden("[data-testid=state-stale]", false);
      }, 15000);
      return snapshot;
    } catch (error) {
      // 与 D2 行为一致：错误只体现在状态区，不向调用方抛出。
      showError(error && error.message ? error.message : String(error));
      return null;
    } finally {
      setLoading(false);
      setHidden("[data-testid=state-loading]", true);
    }
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

  function boot() {
    installRuntimeStyle();
    installModeSwitch();
    installIntervalSwitch();
    installSymbolSwitch();
    installLevelFilter();
    installCrosshair();
    ensureSelectionSection();
    installReplayControls();

    if (typeof window.ResizeObserver === "function") {
      const observer = new window.ResizeObserver(() => scheduleDraw());
      [q("[data-testid=chart-canvas-region]"), q("[data-testid=chart-volume-region]")].forEach((node) => {
        if (node) observer.observe(node);
      });
    }
    window.addEventListener("resize", scheduleDraw);

    const params = new URLSearchParams(window.location.search);
    const snapshotUrl = root.dataset.snapshotUrl || params.get("snapshot");
    if (snapshotUrl) loadSnapshot(snapshotUrl);
    else if (params.get("demo") !== "off") loadDemo();
    else render(null);

    root.dispatchEvent(new CustomEvent("cpt:dashboard-ready"));
  }

  window.CPTDashboard = {
    schemaVersion: SCHEMA_VERSION,
    render,
    loadSnapshot,
    demoSnapshot,
    loadDemo,
    clear,
    getSnapshot: () => state.snapshot,
    getSelection: () => state.selection,
    startPolling,
    stopPolling,
  };

  boot();
})();
