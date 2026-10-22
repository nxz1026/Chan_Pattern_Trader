// dash-signal.js — CPT 看板 · 由 dashboard/dashboard.js 按功能拆分（R45）
//
// 信号类面板：信号区 / 雷达 / 统计 / T+1 / parity / runs。
//
// ⚠️ **机械搬运**：没有改任何一行逻辑。
//    共享头（工具函数、状态文案表、常量）**只在 `dash-core.js` 里有一份**
//    （R77 收敛）。本模块的 `// <<<HEAD` 段是空的 —— 加进去的声明会被
//    构建脚本静默丢弃（known-traps #44）。bundle 是单一 IIFE，跨模块可见。

// <<<HEAD
// ⚠️ R77：本模块**不再持有**共享头的副本。
// 权威头只在 `dash-core.js` 里；bundle 只拼 ORDER[0] 的 HEAD 段，
// 这里的任何声明都会被**静默丢弃**（known-traps #44）。
// 需要新的共享常量/工具函数 → 加进 dash-core.js 的 HEAD，不要加在这里。
// 本模块直接从作用域里用它（bundle 是单一 IIFE，跨模块可见）。
// >>>HEAD
// <<<FUNCS
  /* R71：三步进度条。数据来自 snapshot.summary.signal_progress（后端
     cpt/application/signal_progress.py 产出）。**没有数据就整块隐藏**，
     不拿占位文字糊弄——「没算」和「算了没走到」必须能区分。 */
  const PROGRESS_STATE_LABELS = {
    done: "✓",
    failed: "✗",
    pending: "·",
  };

  /* R71 Step3：失效原因码 → 中文标签。码是稳定标识，定义在
     cpt/application/signal_invalidation.py::REASON_CODES，前端只按码查文案，
     **不按顺序猜**——后端加一个新码，这里查不到就退回显示原始码，不会错位。
     文案只写「为什么作废」，措辞不含买卖建议（那是 signal_progress 的活）。 */
  const INVALIDATION_REASON_LABELS = {
    not_in_downtrend: "方向不对（不是下跌走势）",
    not_enough_centers: "中枢不到两个",
    no_divergence_leg: "中枢二之后没再往下走",
    previous_invalidated: "此前已作废（终态不回退）",
  };

  /* R78：全市场分布要用的人话。步号 0~3 与「不确定」桶。
     ⚠️ 键 "unknown" **不是**一个步号——它表示「这只票走到第几步不确定」，
     因为失效行没记原因。把它当成第 4 步读就错了。 */
  const STEP_INDEX_LABELS = {
    "0": "一步都没走完（中枢不到两个）",
    "1": "走完第一步、卡在第二步（中枢有了，但没再往下走）",
    "2": "走完前两步、卡在第三步（跌到位了，等反弹确认）",
    "3": "三步全走完",
    unknown: "走到第几步不确定（失效行没记原因）",
  };

  function renderSignalProgress(progress) {
    const node = q("[data-testid=signal-progress]");
    if (!node) return;
    const item = isObject(progress) ? progress : null;
    const steps = item && Array.isArray(item.steps) ? item.steps : null;
    if (!steps || !steps.length) {
      node.hidden = true;
      return;
    }
    node.hidden = false;
    setText("[data-testid=signal-progress-steps]", steps.map((step) => {
      const state = step && step.state ? step.state : "pending";
      const mark = PROGRESS_STATE_LABELS[state] || PROGRESS_STATE_LABELS.pending;
      const title = step && step.title ? step.title : "";
      return `${mark} ${title}`;
    }).join("   "));
    setText("[data-testid=signal-progress-explain]", item.explain ? String(item.explain) : "—");
    setText("[data-testid=signal-progress-action]", item.action ? String(item.action) : "—");
  }


  function renderSignalSection(signal, progress) {
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
    /* R71：三步进度（「走到哪一步 / 卡在哪 / 该做什么」） */
    renderSignalProgress(progress);
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
      // R76：「一卖信号（结构卖点…）」仍然带「卖点」二字。
      // 这条讲的是**上涨位置提示**这另一段结构，不是卖出指令。
      firstSellNode.textContent = "当前展示：上涨位置提示（另一段结构，不是卖出指令）";
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
    // R71 Step3：失效原因分布。后端 key = stats.invalidation_reasons
    // （cpt/application/dashboard_stats.py::signal_statistics）。
    // ``unknown`` 是 R71 之前落库的行——那时没记原因，如实单列，
    // 绝不能并进某个具体原因，否则统计出来的比例是编的。
    const reasonCounts = isObject(stats.invalidation_reasons) ? stats.invalidation_reasons : {};
    Object.keys(reasonCounts).forEach((key) => {
      const label = key === "unknown" ? "原因未记录（R71 之前的行）" : INVALIDATION_REASON_LABELS[key] || key;
      row(`失效 · ${label}`, String(reasonCounts[key]));
    });
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


  /* ---------------- R73 方案 A1：信号全景（按票） ---------------- */

  // 数据源 = /api/dashboard/signal-overview（cpt/application/signal_overview.py）。
  // 与上面「信号统计」的区别必须写死在文案里：那个数事件条数，这个数票。

  // 全景里只显示「还站在位置上」的状态；invalidated 是绝大多数（实测 127/202），
  // 全列出来会把真正该看的 54 只票埋掉。所以正文只列 standing，其余走一行汇总。
  const OVERVIEW_STANDING = ["structure_ready", "confirmed"];

  function buildOverviewRow(entry) {
    const item = document.createElement("li");
    item.dataset.testid = "signal-overview-row";
    const code = String(entry.code || "—");
    const status = STATUS_LABELS[entry.status] || entry.status || "—";
    const age = entry.age_days == null ? "未知" : `距最后跃迁 ${entry.age_days} 天`;
    // ⚠️ age_days 只是**下界**（状态没变就不写行，见 signal_overview 模块
    // docstring），所以这里写「距最后跃迁」而不是「卡了 N 天」。
    const stale = entry.stale === true ? " · ⚠️ 窗口内没再动过，这是最后已知状态" : "";
    item.textContent = `${code} · ${status} · ${age}${stale}`;
    return item;
  }

  /* R78：全市场「走完几步 / 卡在哪步、为什么」的人话分解。
     两块分开渲染，因为它们回答的是不同问题：
       · 上面一块 = 多少只票走完了 0/1/2/3 步（每只票都有一个格子）；
       · 下面一块 = 卡住的票各自因为什么卡住（只有记了原因的行才有）。
     两者相加**不等于** total，差的就是「没记原因」的那批 —— 必须说出来，
     不能凑平。 */
  // R80：关注列表「跑过没有 / 数据多旧」的提醒。
  //
  // 为什么要把「没跑过」和「跑了但旧」分开：两者对用户的下一步动作完全不同 ——
  // 前者要等自动任务（或手动跑一次），后者说明跑过了但这只票没再变化。
  // 合成一句「没有数据」会让人不知道该等还是该动手。
  //
  // 为什么没有关注时整块不渲染：见过一次空列表的人不会再想看到
  // 「关注 0 只、全部缺数据」这种吓人的字。
  function buildWatchlistAlert(ov) {
    const wrap = document.createElement("div");
    wrap.dataset.testid = "signal-overview-watchlist";
    const wl = isObject(ov.watchlist) ? ov.watchlist : null;
    if (!wl || wl.watched !== true) {
      wrap.hidden = true;
      return wrap;
    }
    const counts = isObject(wl.counts) ? wl.counts : {};
    const total = Number(counts.total) || 0;
    const neverRun = Number(counts.never_run) || 0;
    const stale = Number(counts.stale) || 0;
    const items = asArray(wl.items);
    // ⚠️ 「旧」的判据必须与后端 `stale` 一致：age_days >= stale_after_days。
    // 第一版只检查 `age_days != null`，实测把刚跑过的 0 天票也列进了「数据旧」名单
    // （node 桩上复现：`000002、000001 · 最后一次状态跃迁超过 7 天`）。
    const staleAfter = Number(ov.stale_after_days) || 7;
    const isOld = (it) =>
      it.never_run !== true && it.age_days != null && Number(it.age_days) >= staleAfter;
    const names = (flag) =>
      items
        .filter((it) => (flag === "never" ? it.never_run === true : isOld(it)))
        .map((it) => it.code)
        .slice(0, 10)
        .join("、");

    const heading = document.createElement("h4");
    heading.textContent = `我关注的票（共 ${total} 只）`;
    wrap.appendChild(heading);

    const dl = document.createElement("dl");
    dl.className = "kv";
    const REFRESH = Number(wl.refresh_hours) || 4;
    const addRow = (label, value, testid) => {
      const item = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = label;
      const detail = document.createElement("dd");
      detail.textContent = value;
      if (testid) detail.dataset.testid = testid;
      item.append(term, detail);
      dl.appendChild(item);
    };
    addRow("已在跑（有数据）", `${
      Number(counts.fresh) || 0
    } 只`, "signal-overview-watch-fresh");
    addRow(
      "还没跑过",
      neverRun
        ? `${neverRun} 只（${names("never")}）— 自动任务每 ${REFRESH} 小时内会跑一次，等一会儿再看`
        : "0 只",
      "signal-overview-watch-never",
    );
    addRow(
      "跑过但数据旧",
      stale ? `${stale} 只（${names("stale")}）— 最后一次状态跃迁超过 ${ov.stale_after_days || 7} 天` : "0 只",
      "signal-overview-watch-stale",
    );
    wrap.appendChild(dl);

    // 只有真缺数据时才给红字提醒 —— 全都新鲜就别主动制造焦虑。
    if (neverRun || stale) {
      const p = document.createElement("p");
      p.className = "hint";
      p.dataset.testid = "signal-overview-watch-warning";
      p.textContent =
        (neverRun ? `⚠️ ${neverRun} 只关注的票还没跑过数据。` : "") +
        (stale ? `⚠️ ${stale} 只关注的数据超过 7 天没更新。` : "") +
        `自动任务在后台跑；如果超过 ${REFRESH} 小时还是这样，说明后台任务出问题了（日志 /home/ubuntu/logs/watchlist-snapshot.log）。`;
      wrap.appendChild(p);
    }
    return wrap;
  }

  function buildStepDistribution(ov) {
    const wrap = document.createElement("div");
    wrap.dataset.testid = "signal-overview-steps";
    const dist = isObject(ov.step_distribution) ? ov.step_distribution : null;
    if (!dist) return wrap;

    const heading = document.createElement("h4");
    heading.textContent = "全市场：走到哪一步";
    wrap.appendChild(heading);

    const counts = isObject(dist.step_index_counts) ? dist.step_index_counts : {};
    // 顺序固定 0→3→unknown：按对象键序会随后端插入顺序漂移。
    const order = ["0", "1", "2", "3", "unknown"];
    const total = order.reduce(( acc, k) => acc + (Number(counts[k]) || 0), 0);
    const list = document.createElement("dl");
    list.className = "kv";
    order.forEach((key) => {
      const value = Number(counts[key]) || 0;
      if (!value && key !== "unknown") return; // 0 真步号不占位；unknown 恒显示
      const item = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = STEP_INDEX_LABELS[key] || key;
      const detail = document.createElement("dd");
      const pct = total ? Math.round((value / total) * 100) : 0;
      detail.textContent = `${value} 只（${pct}%）`;
      detail.dataset.testid = `signal-overview-step-${key}`;
      item.append(term, detail);
      list.appendChild(item);
    });
    wrap.appendChild(list);

    // 卡住的原因分解：第几步 → 哪条原因 → 多少只。
    const blocked = isObject(dist.blocked_reason_counts) ? dist.blocked_reason_counts : {};
    const blockedSteps = asArray(dist.blocked_at_order).filter((s) => isObject(blocked[s]));
    if (blockedSteps.length) {
      const h2 = document.createElement("h4");
      h2.textContent = "卡住的票：卡在哪一步、为什么";
      wrap.appendChild(h2);
      const dl = document.createElement("dl");
      dl.className = "kv";
      blockedSteps.forEach((step) => {
        const stepLabel = STEP_INDEX_LABELS[String(step === "two_centers" ? 0 : step === "divergence_leg" ? 1 : 2)] || step;
        Object.keys(blocked[step]).forEach((reason) => {
          const item = document.createElement("div");
          const term = document.createElement("dt");
          const reasonLabel = INVALIDATION_REASON_LABELS[reason] || reason;
          term.textContent = `${stepLabel} · ${reasonLabel}`;
          const detail = document.createElement("dd");
          detail.textContent = `${blocked[step][reason]} 只`;
          item.append(term, detail);
          dl.appendChild(item);
        });
      });
      wrap.appendChild(dl);
    }

    const unknown = Number(dist.no_reason_but_blocked) || 0;
    if (unknown) {
      const p = document.createElement("p");
      p.className = "hint";
      p.dataset.testid = "signal-overview-no-reason";
      p.textContent =
        `另有 ${unknown} 只失效票没记录原因：它们确定还没走完三步，但卡在哪一步查不出来。` +
        "R71 之前的行没落库原因，向上走势的作废按判定器设计也不产原因 —— 重跑补不出来，" +
        "如实留空，不猜。";
      wrap.appendChild(p);
    }
    return wrap;
  }


  /* R85（B）：第一屏横幅 —— 「全市场现在走到哪一步」。
     与下面 event-panel 里的 signal-overview 面板**读同一份数据**
     （remoteOps.signalOverview），但只回答一个问题：**现在能不能动手，为什么不能**。
     第一屏不放表格：表格在 event-panel，这里只放一句话 + 一行数字 + 一句该做什么。 */
  function buildMarketStepBanner(ov) {
    const wrap = q("[data-testid=market-step-banner]");
    if (!wrap) return;
    const dist = isObject(ov) && isObject(ov.step_distribution) ? ov.step_distribution : null;
    if (!dist) {
      wrap.hidden = true;
      return;
    }
    const counts = isObject(dist.step_index_counts) ? dist.step_index_counts : {};
    const n = (k) => Number(counts[k]) || 0;
    const total = ["0", "1", "2", "3", "unknown"].reduce((a, k) => a + n(k), 0);
    const standing = Number(dist.standing) || 0;
    const unknown = Number(dist.no_reason_but_blocked) || 0;
    const done3 = n("3");

    wrap.hidden = false;
    setText("[data-testid=market-step-headline]", standing
      ? `全市场 ${total} 只里，${standing} 只站在位置上（三步走完或差最后一步）`
      : `全市场 ${total} 只，没有一只站在位置上`);
    setText("[data-testid=market-step-numbers]",
      `三步全走完 ${done3} 只 · 差最后一步 ${n("2")} 只 · 卡在第二步 ${n("1")} 只` +
      (unknown ? ` · 不确定走到哪 ${unknown} 只` : ""));

    // 该做什么：**只描述观察动作**，不给买卖指令（R76 红线）。
    let action;
    if (standing === 0) {
      action = "现在什么都不用做：没有一只票走到位置。等有票进入「差最后一步」再来看。";
    } else {
      action = `可以在下面「信号全景」里看这 ${standing} 只具体是哪几只、卡在哪。` +
        "记住 R87 全市场实测：位置之后 20 日平均 +0.56%、胜率 45.6%，随便挑一天是 +0.22%、44.8%" +
        " —— 它是位置提示，不是买点。";
    }
    setText("[data-testid=market-step-action]", action);
  }


  function renderSignalOverview() {
    const panel = q("[data-testid=event-panel]");
    if (!panel) return;
    let section = q("[data-testid=signal-overview]");
    if (!section) {
      section = document.createElement("section");
      section.dataset.testid = "signal-overview";
      const heading = document.createElement("h3");
      heading.textContent = "信号全景（按票：现在走到哪一步）";
      section.appendChild(heading);
      panel.appendChild(section);
    }
    while (section.children.length > 1) section.removeChild(section.lastChild);

    const payload = remoteOps.signalOverview;
    const hint = (text) => {
      const p = document.createElement("p");
      p.className = "hint";
      p.dataset.testid = "signal-overview-basis";
      p.textContent = text;
      section.appendChild(p);
    };

    if (!isObject(payload)) {
      hint("口径：每只票取最新一条状态跃迁（尚未拉取）。");
      return;
    }
    if (payload.available !== true || !isObject(payload.overview)) {
      const p = document.createElement("p");
      p.textContent = `全景不可用：${reasonText(SIGNAL_STATS_REASON_LABELS, payload.reason, NETWORK_FALLBACK)}`;
      section.appendChild(p);
      return;
    }
    const ov = payload.overview;
    const { rows } = ov;
    hint(
      `口径：每只票**只取最新一条**状态跃迁（近 ${payload.days || remoteOps.days} 天）。` +
        "与上面「信号统计」不同——那个数的是事件条数，同一只票跃迁 5 次就算 5 条。" +
        "「距最后跃迁」只是下界：状态没变就不写行，它不等于「卡了这么久」。",
    );

    const stats = isObject(ov.status_counts) ? ov.status_counts : {};
    const list = document.createElement("dl");
    list.className = "kv";
    const row = (key, value, testid) => {
      const item = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = key;
      const detail = document.createElement("dd");
      detail.textContent = value;
      if (testid) detail.dataset.testid = testid;
      item.append(term, detail);
      list.appendChild(item);
    };
    row("覆盖票数", String(ov.total == null ? "—" : ov.total), "signal-overview-total");
    OVERVIEW_STANDING.forEach((key) => {
      row(`站在位置上 · ${STATUS_LABELS[key] || key}`, String(stats[key] || 0));
    });
    const invalidated = stats.invalidated || 0;
    row("已作废（不再评估）", String(invalidated));
    const blind = isObject(ov.blind_spots) ? ov.blind_spots.empty_structure || 0 : 0;
    if (blind) {
      row("结构不足两个中枢（连第一步都没到）", String(blind));
    }
    row(
      `距最后跃迁 ≥ ${ov.stale_after_days || 7} 天`,
      String(ov.stale_count == null ? "—" : ov.stale_count),
      "signal-overview-stale",
    );
    section.appendChild(list);
    section.appendChild(buildWatchlistAlert(ov));
    section.appendChild(buildStepDistribution(ov));

    const standing = asArray(rows).filter((entry) => OVERVIEW_STANDING.includes(entry.status));
    if (!standing.length) {
      const p = document.createElement("p");
      p.textContent = "当前没有票站在位置上（结构就绪或已确认）。";
      section.appendChild(p);
      return;
    }
    const ul = document.createElement("ul");
    ul.dataset.testid = "signal-overview-rows";
    standing.slice(0, 20).forEach((entry) => ul.appendChild(buildOverviewRow(entry)));
    section.appendChild(ul);
    if (standing.length > 20) {
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = `另有 ${standing.length - 20} 只未列出。`;
      section.appendChild(p);
    }
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
      // R76：类型名交给 signalTypeLabel() 渲染，这里只标出「它不是上面那条」。
      entries.push({ ...sigSell, _code: code, _market: "a_share", _source_label: "另一段结构" });
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
      "⚠️ 每条「信号」说的都是价格处在什么位置 —— 它不预测涨跌，也不是买卖指令。";
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
        priceDiv.textContent = `收盘价 ${formatPrice(entry.price)}（不是买点）`;
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
    // R71 Step3：失效的票必须当场说清原因。数据来自事件流最新一行的
    // invalidated_reason（由 center_ids 尾项还原）。拿不到就明说「未记录」，
    // 不要留空让人以为忘了渲染。
    if (signal.status === "invalidated") {
      const reasonCode = signal.invalidated_reason;
      const reason = document.createElement("p");
      reason.dataset.testid = "signal-history-reason";
      reason.className = "signal-invalidation-reason";
      reason.textContent = reasonCode
        ? `失效原因：${INVALIDATION_REASON_LABELS[reasonCode] || reasonCode}`
        : "失效原因：未记录（R71 之前的行不存原因，无法反推）";
      section.appendChild(reason);
    }
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
