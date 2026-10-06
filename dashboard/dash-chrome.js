// dash-chrome.js — CPT 看板 · 由 dashboard/dashboard.js 按功能拆分（R45）
//
// 顶栏与全局状态：renderChrome（342 行串行 setText）+ 顶栏控件。
// 
// ⚠️ renderChrome 是**每次轮询都跑**的热路径，但它是「顺序 DOM 写入」
// 而不是复杂逻辑 —— **拆分不会让它变快**（提速要加 dirty check，
// 那是行为改动，风险高得多）。
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
        // 走 globalThis：dash-*.js 各自独立 IIFE，裸标识符跨文件不可达
        // （2026-10-06 实测此处一直是死代码，点「实时」也没启动轮询）。
        // 取不到就抛 —— 静默跳过只会让页面停在旧数据上却看不出原因。
        if (state.snapshotUrl) {
          const ops = globalThis.CPTDashboardOps;
          if (!ops || typeof ops.startPolling !== "function") {
            throw new Error("CPTDashboardOps.startPolling 不可达（dash-ops.js 未挂到 globalThis）");
          }
          ops.startPolling(state.snapshotUrl);
        }
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

// >>>FUNCS
})();
