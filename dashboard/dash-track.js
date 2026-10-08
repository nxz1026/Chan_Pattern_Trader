// dash-track.js — 「我的追踪」独立页面脚本（段 2）
//
// 与 dashboard/* 解耦：不读 snapshot、不依赖 window.CPTDashboard、不引 vendor。
// 只调 /api/dashboard/track* 6 端点 + 用户名靠 X-CPT-User 头。
//
// 模块边界（IIFE 风格，与 dash-chrome.js 同形；便于以后拿到所有仪表脚本）

(() => {
  "use strict";

  const LS_KEY = "cpt_track_user";
  const API = "/api/dashboard/track";
  const FETCH_OPTS = { credentials: "omit" };

  // ── DOM 工具 ────────────────────────────────────────────────

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "html") node.innerHTML = v;
        else if (k.startsWith("data-")) node.setAttribute(k, String(v));
        else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
        else node.setAttribute(k, String(v));
      }
    }
    if (children) {
      for (const c of children) {
        if (c == null) continue;
        node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      }
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      return d.toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
    } catch (_) {
      return iso;
    }
  }

  function num(v) {
    if (v == null) return null;
    if (typeof v === "number") return Number.isFinite(v) ? v : null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function price(v) {
    const n = num(v);
    return n == null ? "—" : n.toFixed(2);
  }

  function setStatus(target, text, kind) {
    target.textContent = text || "";
    target.removeAttribute("data-kind");
    if (kind) target.setAttribute("data-kind", kind);
  }

  // ── 用户名 ───────────────────────────────────────────────────

  function getUser() {
    try {
      const u = localStorage.getItem(LS_KEY);
      if (u && /^[A-Za-z0-9._-]{1,32}$/.test(u)) return u;
    } catch (_) {}
    return "default";
  }

  function saveUser(name) {
    if (!/^[A-Za-z0-9._-]{0,32}$/.test(name)) return false;
    try {
      if (name) localStorage.setItem(LS_KEY, name);
      else localStorage.removeItem(LS_KEY);
      return true;
    } catch (_) {
      return false;
    }
  }

  // ── API 调用 ─────────────────────────────────────────────────

  async function apiList() {
    const r = await fetch(API, withUser({ method: "GET" }));
    return [await r.json(), r.status];
  }

  async function apiAdd(code, note) {
    const r = await fetch(
      API,
      withUser({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, note: note || null }),
      }),
    );
    return [await r.json(), r.status];
  }

  async function apiRemove(code) {
    const r = await fetch(
      API,
      withUser({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, op: "remove" }),
      }),
    );
    let body = null;
    try {
      body = await r.json();
    } catch (_) {
      body = null;
    }
    return [body, r.status];
  }

  async function apiRestore(code) {
    const r = await fetch(
      API,
      withUser({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, op: "restore" }),
      }),
    );
    return [await r.json(), r.status];
  }

  async function apiAdvice(code) {
    const r = await fetch(`${API}/${encodeURIComponent(code)}/advice`, withUser({ method: "GET" }));
    return [await r.json(), r.status];
  }

  async function apiHistory(code, days) {
    const url = `${API}/${encodeURIComponent(code)}/history?days=${days}`;
    const r = await fetch(url, withUser({ method: "GET" }));
    return [await r.json(), r.status];
  }

  async function apiSpeak(code) {
    const r = await fetch(
      `${API}/${encodeURIComponent(code)}/speak`,
      withUser({ method: "POST" }),
    );
    let body = null;
    try {
      body = await r.json();
    } catch (_) {
      body = null;
    }
    return [body, r.status];
  }

  async function apiLlmStatus(callId) {
    const user = getUser();
    const url = `/api/dashboard/llm/calls?subject_id=${encodeURIComponent(`track:${user}:`)}&limit=10`;
    const r = await fetch(url, withUser({ method: "GET" }));
    if (!r.ok) return null;
    const body = await r.json();
    if (!body || !Array.isArray(body.calls)) return null;
    return body.calls.find((c) => c && c.call_id === callId) || null;
  }

  function withUser(opts) {
    return Object.assign({}, FETCH_OPTS, opts, {
      headers: Object.assign({}, opts && opts.headers, { "X-CPT-User": getUser() }),
    });
  }

  // ── LLM 轮询 + 单卡刷新 ─────────────────────────────────────

  const TERMINAL = new Set(["succeeded", "failed", "error", "interrupted"]);
  const POLL_INTERVAL_MS = 2000;
  const POLL_TIMEOUT_MS = 30000;

  async function pollSpeak(callId) {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
      const row = await apiLlmStatus(callId);
      if (row && TERMINAL.has(row.status)) {
        return row.status === "succeeded";
      }
    }
    return false;
  }

  async function refreshOne(code) {
    const card = document.querySelector(`[data-body="${code}"]`);
    if (!card) return;
    clear(card);
    card.appendChild(el("div", { class: "hint", text: "拉取中…" }));
    const [payload, status] = await apiAdvice(code);
    clear(card);
    card.appendChild(renderAdvice(code, payload, status));
  }

  // ── 卡片渲染 ─────────────────────────────────────────────────

  function renderPointsRow(label, pt) {
    const tr = el("tr");
    tr.appendChild(el("td", { text: label }));
    const ref = pt && pt.reference;
    const conf = pt && pt.confirmed;
    tr.appendChild(
      el("td", { className: ref == null ? "num null" : "num", text: ref == null ? "—" : ref.toFixed(2) }),
    );
    tr.appendChild(
      el("td", { className: conf == null ? "num null" : "num", text: conf == null ? "—" : conf.toFixed(2) }),
    );
    return tr;
  }

  function renderPointsTable(points) {
    const buy = (points && points.buy) || {};
    const sell = (points && points.sell) || {};
    const stop = points && points.stop_loss_reference;
    const tbl = el("table", { class: "points" });
    const head = el("tr");
    head.appendChild(el("th", { text: "动作" }));
    head.appendChild(el("th", { text: "reference" }));
    head.appendChild(el("th", { text: "confirmed" }));
    tbl.appendChild(head);
    tbl.appendChild(renderPointsRow("BUY", buy));
    tbl.appendChild(renderPointsRow("SELL", sell));
    const tr = el("tr");
    tr.appendChild(el("td", { text: "止损参考" }));
    tr.appendChild(
      el("td", { className: stop == null ? "num null" : "num", colspan: "2", text: stop == null ? "—" : stop.toFixed(2) }),
    );
    tbl.appendChild(tr);
    return tbl;
  }

  function renderCurrent(c) {
    const wrap = el("div");
    const line1 = el("div", { class: "status-line" });
    line1.appendChild(el("span", { text: "现价 " }));
    line1.appendChild(el("span", { text: price(c && c.price) }));
    if (c && c.raw_close != null) {
      line1.appendChild(el("span", { text: " · 原始价 " }));
      line1.appendChild(el("span", { text: price(c.raw_close) }));
    }
    if (c && c.price_ratio != null) {
      line1.appendChild(el("span", { text: " · 倍率 " }));
      line1.appendChild(el("span", { text: price(c.price_ratio) }));
    }
    wrap.appendChild(line1);
    if (c && c.action_label) {
      const badge = el("span", { class: "badge warn", text: c.action_label });
      const line2 = el("div", { class: "status-line" });
      line2.appendChild(el("span", { text: "建议动作：" }));
      line2.appendChild(badge);
      if (c.status) {
        line2.appendChild(el("span", { text: `  · 状态 ${c.status}` }));
      }
      wrap.appendChild(line2);
    }
    if (c && c.headline) wrap.appendChild(el("div", { class: "status-line", text: c.headline }));
    if (c && c.reason) wrap.appendChild(el("div", { class: "status-line", text: `原因：${c.reason}` }));
    return wrap;
  }

  function renderAlgorithm(a) {
    const wrap = el("div");
    const line = el("div", { class: "status-line" });
    line.appendChild(el("span", { text: `规则：${a && a.rule ? a.rule : "—"}` }));
    if (a && a.status) line.appendChild(el("span", { text: ` · 状态 ${a.status}` }));
    wrap.appendChild(line);
    const f = a && a.facts;
    if (f) {
      const fline = el("div", { class: "status-line" });
      fline.appendChild(el("span", { text: "事实：" }));
      fline.appendChild(el("span", { text: `反向笔=${f.has_reversal_bi ? "yes" : "no"}` }));
      if (f.divergence_status) {
        fline.appendChild(el("span", { text: ` · 背离=${f.divergence_status}` }));
      }
      wrap.appendChild(fline);
    }
    const trig = (a && a.triggers_to_confirm) || [];
    if (trig.length) {
      const tline = el("div", { class: "status-line" });
      tline.appendChild(el("span", { text: "何时跃迁到 confirmed：反向笔收盘确认。" }));
      wrap.appendChild(tline);
    }
    const inv = (a && a.what_would_invalidate) || [];
    if (inv.length) {
      wrap.appendChild(el("div", { class: "status-line", text: `失效触发：${inv.join(" / ")}` }));
    }
    return wrap;
  }

  function renderHuman(human, code) {
    const wrap = el("div");
    if (human && human.text) {
      wrap.appendChild(el("div", { class: "status-line", text: human.text }));
      if (human.generated_at) {
        wrap.appendChild(el("div", { class: "hint", text: `生成于 ${fmtDate(human.generated_at)}` }));
      }
    } else {
      wrap.appendChild(el("div", { class: "hint", text: "暂无人话（6h 缓存为空）。" }));
    }
    const btn = el(
      "button",
      { type: "button", "data-action": "speak-again", "data-code": code },
      "再讲一次人话",
    );
    wrap.appendChild(btn);
    const status = el("span", { "data-status": `speak-${code}`, style: { marginLeft: "8px" } });
    wrap.appendChild(status);
    return wrap;
  }

  function renderCard(item) {
    const card = el("div", { class: "card" });
    const head = el("div", { class: "card-head" });
    const left = el("div");
    left.appendChild(el("span", { class: "code", text: item.code }));
    if (item.note) {
      left.appendChild(el("span", { class: "note", text: ` · ${item.note}` }));
    }
    head.appendChild(left);
    const meta = el("div", { class: "meta", text: `加入 ${fmtDate(item.added_at || item.created_at)}` });
    head.appendChild(meta);
    card.appendChild(head);
    const acts = el("div", { class: "card-actions", style: { marginTop: "8px" } });
    acts.appendChild(el("button", { type: "button", "data-action": "advice", "data-code": item.code }, "拉取建议"));
    acts.appendChild(
      el("button", { type: "button", class: "danger", "data-action": "remove", "data-code": item.code }, "移除"),
    );
    card.appendChild(acts);
    const body = el("div", { "data-body": item.code });
    card.appendChild(body);
    return card;
  }

  function renderAdvice(code, payload, status) {
    const wrap = el("div");
    const httpStatus = status != null ? status : (payload && payload.status) || 200;
    if (httpStatus >= 400) {
      wrap.appendChild(
        el("div", { class: "error", text: payload && payload.error ? payload.error : `HTTP ${status}` }),
      );
      return wrap;
    }
    if (!payload || typeof payload !== "object") {
      wrap.appendChild(el("div", { class: "error", text: "建议接口返回为空" }));
      return wrap;
    }
    wrap.appendChild(
      el("div", { class: "hint", text: `${payload.code || code} · 时点 ${fmtDate(payload.as_of)}` }),
    );
    if (payload.current) {
      const fold = el("details", { class: "fold", open: "open" });
      fold.appendChild(el("summary", { text: "当前结构（current）" }));
      fold.appendChild(renderCurrent(payload.current));
      wrap.appendChild(fold);
    }
    if (payload.explain_algorithm) {
      const fold = el("details", { class: "fold" });
      fold.appendChild(el("summary", { text: "算法是怎么算的（algorithm）" }));
      fold.appendChild(renderAlgorithm(payload.explain_algorithm));
      wrap.appendChild(fold);
    }
    if (payload.suggested_points) {
      const fold = el("details", { class: "fold", open: "open" });
      fold.appendChild(el("summary", { text: "BUY / SELL 建议点（points）" }));
      fold.appendChild(renderPointsTable(payload.suggested_points));
      if (payload.suggested_points.reason) {
        fold.appendChild(el("div", { class: "hint", text: payload.suggested_points.reason }));
      }
      wrap.appendChild(fold);
    }
    const humanFold = el("details", { class: "fold" });
    humanFold.appendChild(el("summary", { text: "讲人话（human · LLM）" }));
    humanFold.appendChild(renderHuman(payload.human, code));
    wrap.appendChild(humanFold);
    if (payload.disclaimer) {
      wrap.appendChild(el("div", { class: "hint", text: payload.disclaimer }));
    }
    return wrap;
  }

  // ── 渲染主列表 ───────────────────────────────────────────────

  async function refresh() {
    const listEl = document.getElementById("list");
    const recEl = document.getElementById("recycle");
    const stamp = document.getElementById("last-update");
    clear(listEl);
    clear(recEl);
    listEl.appendChild(el("div", { class: "hint", text: "加载中…" }));
    const [payload, status] = await apiList();
    clear(listEl);
    clear(recEl);
    setStatus(stamp, `更新于 ${fmtDate(new Date().toISOString())}`, "ok");
    if (status !== 200) {
      listEl.appendChild(
        el("div", {
          class: "error",
          text: payload && payload.error ? payload.error : `HTTP ${status}`,
        }),
      );
      return;
    }
    if (payload.available === false) {
      listEl.appendChild(
        el("div", { class: "error", text: `服务降级：${payload.reason || "unavailable"}` }),
      );
      return;
    }
    const items = (payload && payload.items) || [];
    const recycle = (payload && payload.recycle) || [];
    if (!items.length) {
      listEl.appendChild(
        el("div", { class: "empty", text: "还没有追踪的股票。用上方表单加入。" }),
      );
    } else {
      for (const item of items) listEl.appendChild(renderCard(item));
    }
    if (!recycle.length) {
      recEl.appendChild(el("div", { class: "empty", text: "回收站为空。" }));
    } else {
      for (const item of recycle) {
        const card = el("div", { class: "card" });
        const head = el("div", { class: "card-head" });
        head.appendChild(el("span", { class: "code", text: item.code }));
        head.appendChild(
          el("span", { class: "meta", text: `移除于 ${fmtDate(item.removed_at)}` }),
        );
        card.appendChild(head);
        const acts = el("div", { class: "card-actions", style: { marginTop: "8px" } });
        acts.appendChild(
          el("button", { type: "button", "data-action": "restore", "data-code": item.code }, "复活"),
        );
        acts.appendChild(
          el("button", {
            type: "button",
            class: "danger",
            "data-action": "remove",
            "data-code": item.code,
          }, "永久丢弃（不可恢复）"),
        );
        card.appendChild(acts);
        recEl.appendChild(card);
      }
    }
  }

  async function handleAdd(form) {
    const statusEl = form.querySelector("[data-status]");
    const code = form.elements["code"].value.trim();
    const note = form.elements["note"].value.trim();
    if (!/^[0-9]{6}$/.test(code)) {
      setStatus(statusEl, "代码必须是 6 位数字", "bad");
      return;
    }
    setStatus(statusEl, "提交中…");
    const [payload, status] = await apiAdd(code, note);
    if (status === 200) {
      setStatus(statusEl, payload && payload.item && payload.item.note ? `已加入（${payload.item.note}）` : "已加入", "ok");
      form.elements["note"].value = "";
      refresh();
    } else {
      setStatus(statusEl, payload && payload.error ? payload.error : `HTTP ${status}`, "bad");
    }
  }

  async function handleAction(target) {
    const code = target.getAttribute("data-code");
    const action = target.getAttribute("data-action");
    if (!code || !action) return;
    if (action === "advice") {
      const card = document.querySelector(`[data-body="${code}"]`);
      if (!card) return;
      clear(card);
      card.appendChild(el("div", { class: "hint", text: "拉取中…" }));
      const [payload, status] = await apiAdvice(code);
      clear(card);
      card.appendChild(renderAdvice(code, payload, status));
      return;
    }
    if (action === "remove") {
      const [, status] = await apiRemove(code);
      if (status === 200 || status === 204) {
        refresh();
      }
      return;
    }
    if (action === "restore") {
      const [payload, status] = await apiRestore(code);
      if (status === 200) {
        refresh();
      } else if (payload && payload.error === "not_in_recycle") {
        refresh();
      }
      return;
    }
    if (action === "speak-again") {
      const speakStatus = document.querySelector(`[data-status="speak-${code}"]`);
      if (speakStatus) setStatus(speakStatus, "提交中…");
      const [payload, status] = await apiSpeak(code);
      if (status !== 200) {
        if (speakStatus) {
          setStatus(
            speakStatus,
            payload && payload.error ? `失败：${payload.error}` : `失败 HTTP ${status}`,
            "bad",
          );
        }
        return;
      }
      const callId = payload && payload.call_id;
      const phase = payload && payload.status;
      if (phase === "duplicate") {
        if (speakStatus) setStatus(speakStatus, "6h 内已生成过，正在重新拉取…", "ok");
        await refreshOne(code);
        return;
      }
      if (!callId) {
        if (speakStatus) setStatus(speakStatus, "提交成功但无 call_id，请刷新页面", "bad");
        return;
      }
      if (speakStatus) setStatus(speakStatus, `生成中（call_id=${callId.slice(0, 8)}）…`);
      const done = await pollSpeak(callId);
      if (done) {
        if (speakStatus) setStatus(speakStatus, "已生成，正在刷新…", "ok");
        await refreshOne(code);
      } else if (speakStatus) {
        setStatus(speakStatus, "等待超时（30s），可重试", "bad");
      }
      return;
    }
  }

  function init() {
    const userInput = document.getElementById("user");
    const saveBtn = document.getElementById("save-user");
    const userStatus = document.getElementById("user-status");
    userInput.value = getUser();
    saveBtn.addEventListener("click", () => {
      const ok = saveUser(userInput.value.trim());
      setStatus(
        userStatus,
        ok ? `已生效：${getUser()}` : "格式错误（字母/数字/._-，最多 32 字符）",
        ok ? "ok" : "bad",
      );
    });
    document.getElementById("add-form").addEventListener("submit", (e) => {
      e.preventDefault();
      handleAdd(e.target);
    });
    document.body.addEventListener("click", (e) => {
      const t = e.target;
      if (t && t.matches && t.matches("button[data-action]")) {
        e.preventDefault();
        handleAction(t);
      }
    });
    refresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();