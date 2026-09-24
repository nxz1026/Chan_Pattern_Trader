/**
 * CPT 主看板 · A 股市场模式（R17-3）
 *
 * 目标：在主看板里直接看 A 股日线的缠论结构，**复用 R16 的四个画布**。
 *
 * 之所以能一行画布代码都不改：后端 A 股快照走的是与加密侧**完全同构**的
 * `dashboard.v2` schema（`cpt/application/a_share_snapshot.py`）。本模块只负责
 * 「换一个 snapshot URL」和「换一套顶栏控件」，渲染仍然全部交给 dashboard.js 的
 * 画布分发器（本模块**不碰**任何画布或渲染代码）。
 *
 * 与加密侧的三点差异（都必须在 UI 上体现，不能装作一样）：
 *   1. **不轮询**：A 股日线收盘后不再变，轮询纯属浪费；切到 A 股会停掉定时器；
 *   2. **周期固定 1d**：interval 选择器锁死并禁用；
 *   3. **只有 94/5225 只画得出来**：缺复权因子的代码后端返回 degraded +
 *      `reason=no_factor`，前端必须明说，不能画空图让人猜。
 *
 * URL 约定（与 `?canvas=` 同套路，可分享、可审计）：
 *   `?market=a_share&code=002614`
 */

(function () {
  "use strict";

  const MARKET_CRYPTO = "crypto";
  const MARKET_A_SHARE = "a_share";

  /** 后端降级原因 → 面向用户的中文说明（A 股专属，加密侧的在 dashboard.js）。 */
  const A_SHARE_REASONS = {
    no_factor: "该代码缺复权因子，画不出后复权序列（全库 5225 只里只有 94 只有因子）",
    no_data: "本地库 public.daily_bar 里没有该代码的行情",
    invalid_code: "代码格式不正确",
  };

  function q(selector, scope) {
    return (scope || document).querySelector(selector);
  }

  /**
   * 从现有 snapshot 地址推出 A 股路由。
   *
   * **必须从 data-snapshot-url 推导**，不能写死 `/cpt/api/...`：看板可以挂在任意
   * nginx 前缀下（本地是 `/cpt/`，独立 A 股服务是根路径），写死会在换前缀时静默 404。
   */
  function aShareBase(snapshotUrl) {
    const fallback = "/api/dashboard";
    const raw = String(snapshotUrl || "");
    const marker = "/api/dashboard";
    const index = raw.indexOf(marker);
    const base = index >= 0 ? raw.slice(0, index + marker.length) : fallback;
    return `${base}/a-share`;
  }

  function install(dashboard) {
    const root = q("[data-testid=dashboard-root]");
    if (!root) return null;

    const params = new URLSearchParams(window.location.search);
    const requestedMarket = params.get("market") === MARKET_A_SHARE ? MARKET_A_SHARE : MARKET_CRYPTO;
    const requestedCode = (params.get("code") || "").trim();

    const state = {
      market: requestedMarket,
      code: requestedCode,
      pool: null,
      base: aShareBase(root.dataset.snapshotUrl),
    };

    function snapshotUrl() {
      return `${state.base}/snapshot?code=${encodeURIComponent(state.code)}`;
    }

    function syncUrl() {
      const url = new URL(window.location.href);
      if (state.market === MARKET_A_SHARE) {
        url.searchParams.set("market", MARKET_A_SHARE);
        if (state.code) url.searchParams.set("code", state.code);
      } else {
        url.searchParams.delete("market");
        url.searchParams.delete("code");
      }
      window.history.replaceState({}, "", url);
    }

    function renderSwitch() {
      root.dataset.market = state.market;
      // 画布 D 是服务端取数的，需要从 DOM 读 market/code（见 canvas_d.js marketQuery）
      root.dataset.aShareCode = state.market === MARKET_A_SHARE ? state.code : "";
      root.querySelectorAll("[data-market-action]").forEach((button) => {
        button.setAttribute(
          "aria-pressed",
          button.dataset.marketAction === state.market ? "true" : "false",
        );
      });
      const picker = q("[data-testid=a-share-picker]");
      if (picker) picker.hidden = state.market !== MARKET_A_SHARE;
      const interval = q("[data-testid=interval-select]");
      if (interval) {
        interval.disabled = state.market === MARKET_A_SHARE;
        // option 的 value 是**毫秒**（86400000 = 1d），写 "1d" 匹配不到任何 option，
        // select 会显示成空白（实测踩到）。
        if (state.market === MARKET_A_SHARE) interval.value = "86400000";
      }
    }

    function renderPicker() {
      const select = q("[data-testid=a-share-code-select]");
      if (!select) return;
      const pool = state.pool;
      select.replaceChildren();
      const items = pool && Array.isArray(pool.items) ? pool.items : [];
      if (!items.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = state.code || "（热门池不可用）";
        select.appendChild(option);
        return;
      }
      // 画不出来的票**仍然列出但标注**：热门池 100 只里只有 94 只有因子，
      // 直接过滤掉会让人以为池子少了票。
      items.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.code;
        const rank = item.rank === null || item.rank === undefined ? "—" : item.rank;
        option.textContent = item.drawable
          ? `${item.code}（#${rank}）`
          : `${item.code}（#${rank}·缺因子）`;
        option.disabled = item.drawable !== true;
        if (item.code === state.code) option.selected = true;
        select.appendChild(option);
      });
      if (state.code && !items.some((item) => item.code === state.code)) {
        const option = document.createElement("option");
        option.value = state.code;
        option.textContent = `${state.code}（手输）`;
        option.selected = true;
        select.appendChild(option);
      }
    }

    function describeDegraded(snapshot) {
      const runtime = snapshot && typeof snapshot === "object" ? snapshot.runtime || {} : {};
      if (runtime.degraded !== true) return null;
      const reason = String(runtime.degraded_reason || "");
      const key = reason.split(":")[0];
      return A_SHARE_REASONS[key] || `A 股数据不可用（${reason || "unknown"}）`;
    }

    function applyMarket() {
      renderSwitch();
      if (state.market !== MARKET_A_SHARE) return;
      if (!state.code) {
        // 没有 code 就不发请求：后端会回 400，浏览器控制台会多一条红字错误。
        return;
      }
      root.dispatchEvent(
        new CustomEvent("cpt:market-changed", {
          detail: { market: state.market, code: state.code, snapshotUrl: snapshotUrl() },
        }),
      );
      dashboard.loadSnapshot(snapshotUrl());
    }

    function selectCode(code) {
      if (!code || code === state.code) return;
      state.code = code;
      syncUrl();
      setStatus(`正在加载 ${code} …`);
      applyMarket();
    }

    function setStatus(text) {
      const node = q("[data-testid=topbar-status]");
      if (node) node.textContent = text;
    }

    async function loadPool() {
      try {
        const response = await fetch(`${state.base}/pool`, { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        state.pool = await response.json();
      } catch (error) {
        state.pool = null;
        setStatus(`热门池加载失败：${error && error.message ? error.message : error}`);
      }
      renderPicker();
      return state.pool;
    }

    function installSwitch() {
      root.querySelectorAll("[data-market-action]").forEach((button) => {
        button.addEventListener("click", () => {
          const next = button.dataset.marketAction === MARKET_A_SHARE ? MARKET_A_SHARE : MARKET_CRYPTO;
          if (next === state.market) return;
          state.market = next;
          if (next === MARKET_A_SHARE) {
            // 默认给一只有因子的票：空 code 会 400，用户体验也差。
            // **必须在 syncUrl() 之前**赋值，否则 URL 里只有 market 没有 code，
            // 分享出去的链接打开是 400（实测踩到）。
            if (!state.code) state.code = "002614";
            // 先同步刷一遍控件（按钮高亮要立刻响应点击），再等热门池回来
            syncUrl();
            renderSwitch();
            loadPool().then(() => applyMarket());
          } else {
            syncUrl();
            renderSwitch();
            dashboard.loadSnapshot(root.dataset.snapshotUrl);
          }
        });
      });
      const select = q("[data-testid=a-share-code-select]");
      if (select) {
        select.addEventListener("change", () => selectCode(select.value.trim()));
      }
      const input = q("[data-testid=a-share-code-input]");
      if (input) {
        const submit = () => selectCode(input.value.trim());
        input.addEventListener("keydown", (event) => {
          if (event.key === "Enter") submit();
        });
        const button = q("[data-testid=a-share-code-apply]");
        if (button) button.addEventListener("click", submit);
      }
    }

    function init() {
      installSwitch();
      renderSwitch();
      if (state.market === MARKET_A_SHARE) {
        loadPool().then(() => applyMarket());
      }
    }

    return {
      init,
      getMarket: () => state.market,
      getCode: () => state.code,
      getPool: () => state.pool,
      snapshotUrl,
      describeDegraded,
      setCode: selectCode,
      aShareBase: () => state.base,
    };
  }

  window.CPTAShare = { install, aShareBase };
})();
