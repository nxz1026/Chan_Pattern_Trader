// dash-structure.js — CPT 看板 · 由 dashboard/dashboard.js 按功能拆分（R45）
//
// 结构类面板：笔 / 中枢 / 级别树 / 研究细节 / 结构事件时间线。
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

// >>>FUNCS
})();
