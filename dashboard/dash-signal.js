// dash-signal.js — CPT 看板 · 由 dashboard/dashboard.js 按功能拆分（R45）
//
// 信号类面板：信号区 / 雷达 / 统计 / T+1 / parity / runs。
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
    // R59（审计 M27）：后端 key 从 alert_to_confirmed_rate 改名为 confirmed_rate
    // （口径 = confirmed 事件 / 全部事件，见 cpt/application/dashboard_stats.py）。
    // 标签同步改成「确认占比」—— 原来的「预警→确认率」把占比讲成了转化率，
    // 而事件流里根本没有「从 alert 出发」的配对。
    const rate = num(stats.confirmed_rate);
    row("确认占比", rate === null ? "—" : `${(rate * 100).toFixed(1)}%`);
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

// >>>FUNCS
})();
