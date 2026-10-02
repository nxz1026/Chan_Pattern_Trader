// 画布之外的"运行巡检"面板（R38）—— 挂在事件面板下面，自成一块。
//
// 为什么这么写而不是改 index.html：`index.html` 是部署产物、四个画布共用，
// 往里加固定 DOM 会让"这个面板在 A 股页 / 加密页到底显不显示"变成新的疑问。
// 动态建 section（与 renderParity 同款）则只跟 data-testid 走。
//
// 面板要回答三件事，各自对应讨论里的两轨：
//   ① 现在健康吗        → inspection.health + problems/degraded 列表（第一轨）
//   ② 数据水位          → 每个标的的 bar 数 / 缺口 / 因子覆盖率 / 最新 bar 时间
//   ③ 算法有没有变      → 结构计数（笔/中枢）相对上一轮的变化 + backend/指纹
(function () {
  "use strict";

  // API 基址**必须**从页面自身推导，不能写死 "/api/..." ——
  // 页面挂在 /cpt/ 下，写死就会取到 nginx 的 404 HTML（实测报
  // "Unexpected token '<'" —— 拿到的是 index.html 而不是 JSON）。
  // 规则与 dashboard.js 的 DASHBOARD_BASE() 一致：从 body 上的 snapshot-url
  // 截到 /dashboard 之前；没有就退回按当前路径推断。
  function apiBase() {
    const url = (document.body && document.body.dataset
      && document.body.dataset.snapshotUrl) || "";
    if (url) {
      const m = String(url).match(/^(.*)\/api\/dashboard\//);
      if (m && m[1]) return `${m[1]}/api/dashboard`;
    }
    const path = window.location.pathname || "/";
    const m2 = path.match(/^(.*)\/(?:cpt\/)?(?:index\.html)?$/);
    return `${(m2 && m2[1]) || ""}/api/dashboard`;
  }

  const API = `${apiBase()}/inspection?limit=60`;
  let lastKey = null;

  const fmtTime = (ms) => {
    if (!ms) return "—";
    const d = new Date(Number(ms));
    if (Number.isNaN(d.getTime())) return String(ms);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}Z`;
  };
  const ago = (ms) => {
    if (!ms) return "—";
    const m = Math.max(0, Math.round((Date.now() - Number(ms)) / 60000));
    return m < 60 ? `${m} 分钟前` : `${Math.round(m / 60)} 小时前`;
  };
  const q = (sel, root) => (root || document).querySelector(sel);
  const txt = (sel, root) => {
    const n = q(sel, root);
    return n ? (n.textContent || "").trim() : "—";
  };
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };

  function healthBadge(health) {
    const map = { ok: "正常", degraded: "降级", failing: "故障" };
    const b = el("span", `cpt-health cpt-health-${health || "ok"}`, map[health] || "未知");
    return b;
  }

  function renderInspection(data) {
    const host = q("[data-testid=event-panel]");
    if (!host) return;
    let section = q("[data-testid=inspection-panel]");
    if (!section) {
      section = el("section", "cpt-inspection-panel");
      section.dataset.testid = "inspection-panel";
      const h = el("h3", null, "运行巡检与水位");
      section.appendChild(h);
      section.appendChild(el("p", "cpt-inspection-summary", "加载中…"));
      section.appendChild(el("div", "cpt-inspection-body"));
      host.appendChild(section);
    }
    // 保留 3 个子节点：h3 / summary / body。第一版写的是 `> 2`，
    // 于是**首次创建**时（正好 3 个子节点）把刚建的 body 删掉了，
    // 紧接着 `section.children[2].replaceChildren()` 就在 undefined 上炸 ——
    // 实测 "Cannot read properties of undefined (reading 'replaceChildren')"。
    while (section.children.length > 3) section.removeChild(section.lastChild);
    if (!data || data.available === false) {
      section.children[1].textContent =
        `巡检数据不可用：${(data && data.reason) || "unknown"}`;
      return;
    }

    const latest = data.latest_inspection || null;
    const detail = latest && latest.detail && typeof latest.detail === "object"
      ? latest.detail
      : null;
    const problems = (detail && detail.problems) || [];
    const degraded = (detail && detail.degraded) || [];
    const lines = [];
    lines.push("检查时间：" + fmtTime(latest && latest.observed_at));
    lines.push("水位行 " + ((detail && detail.metric_rows) || 0) + " 条");
    if (latest) {
      lines[lines.length - 1] += "（最新一行 " + ago(detail && detail.newest_row_at) + "）";
    }
    q("[data-testid=inspection-last]", section);
    const summary = section.children[1];
    summary.textContent = "";
    summary.appendChild(healthBadge(latest && latest.health));
    summary.appendChild(document.createTextNode(" " + lines.join(" · ")));

    const body = section.children[2];
    body.replaceChildren();
    if (problems.length) {
      const p = el("p", "cpt-inspection-problems", "");
      p.appendChild(el("b", null, "问题 "));
      problems.slice(0, 5).forEach((x) => p.appendChild(el("div", null, "· " + x)));
      body.appendChild(p);
    }
    if (degraded.length) {
      const p = el("p", "cpt-inspection-degraded", "");
      p.appendChild(el("b", null, "降级 " + degraded.length + "："));
      p.appendChild(document.createTextNode(degraded.slice(0, 3).join("；")));
      body.appendChild(p);
    }

    // 每个标的一行：水位 + 结构计数 + backend
    const rows = data.waterlines || [];
    const seen = new Set();
    const table = el("table", "cpt-inspection-table");
    const thead = el("tr");
    ["标的", "health", "bar 数", "缺口", "因子覆盖", "最新 bar", "笔", "中枢", "后端"].forEach((h) =>
      thead.appendChild(el("th", null, h))
    );
    table.appendChild(thead);
    rows.forEach((r) => {
      const key = `${r.market}/${r.symbol}`;
      if (seen.has(key)) return;
      seen.add(key);
      const tr = el("tr");
      tr.dataset.testid = "inspection-row";
      tr.appendChild(el("td", null, key));
      const h = el("td");
      h.appendChild(healthBadge(r.health));
      tr.appendChild(h);
      [
        r.bar_count, r.gap_count,
        (Number(r.factor_coverage || 0) * 100).toFixed(1) + "%",
        fmtTime(r.last_bar_time), r.bi_count, r.zhongshu_count, r.backend || "—",
      ].forEach((v) => tr.appendChild(el("td", null, String(v ?? "—"))));
      table.appendChild(tr);
    });
    if (seen.size) {
      body.appendChild(table);
    } else {
      body.appendChild(el("p", "cpt-inspection-empty", "还没有水位行（轮询跑起来后就会出现）"));
    }

    // 只在内容真的变了才重绘，避免每轮 poll 都闪
    const key = JSON.stringify([latest && latest.health, problems, degraded, [...seen]]);
    if (key !== lastKey) {
      lastKey = key;
      section.dataset.inspectionStale = "false";
    } else {
      section.dataset.inspectionStale = "true";
    }
  }

  async function loadInspection() {
    try {
      const resp = await fetch(API, { headers: { Accept: "application/json" } });
      const body = await resp.json();
      renderInspection(body);
    } catch (err) {
      renderInspection({ available: false, reason: String(err).slice(0, 120) });
    }
  }

  window.cptLoadInspection = loadInspection;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadInspection);
  } else {
    loadInspection();
  }
  // 与主面板同一个轮询节奏
  if (typeof window.cptBindPoll === "function") {
    window.cptBindPoll(loadInspection);
  }
})();
