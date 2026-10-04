// dash-ops.js — CPT 看板 · 由 dashboard/dashboard.js 按功能拆分（R45）
//
// 运维/研究面板：对比、多 run、自选、回放、LLM 面板、事件流。
// 
// 这些只在特定交互里用到，**不参与 30s 轮询的热路径**。
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
// >>>FUNCS
})();
