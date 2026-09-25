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
 *   3. **本地因子不全**：缺复权因子的代码后端返回 degraded + `reason=no_factor`，
 *      前端必须明说，不能画空图让人猜。R17-3b 起缺因子会自动按需拉取，所以
 *      这里不再写死"94/5225"这种会过期的数字。
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
    no_factor: "该代码缺复权因子，画不出后复权序列（本地因子表未覆盖该标的，且按需拉取没成功）",
    no_data: "本地库 public.daily_bar 里没有该代码的行情（日线入库是另一条链路，不在按需拉取范围）",
    invalid_code: "代码格式不正确",
    // 下面三条来自「按需补因子」。区分它们是有意义的：重试对 unsupported 没用，
    // 对 fetch_failed 有用；混成一条会让用户白点。
    no_factor_unsupported: "腾讯不提供该标的的后复权数据，补不了因子（是否提供是逐标的的，无法用板块预测）",
    no_factor_cooldown: "刚为该代码拉取过因子，请稍候再试（冷却中）",
    no_factor_fetch_failed: "拉取因子失败（网络或落库），可以再试一次",
  };

  /** 候选池分组标题（后端 `group` 字段 → 下拉 optgroup 文案）。 */
  const GROUP_LABELS = {
    manual: "手输（已保存）",
    hot: "热门池 Top5",
    strategy: "策略综合 Top5",
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
      //: 手输落盘失败的原因。**单独存**而不是只写状态栏：状态栏会被随后的快照
      //: 请求覆盖，用户就再也看不到"这次没保存"了 —— 而这正是要修的坑。
      saveError: null,
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

      // 顶栏「标的」栏在两市场显示不同东西：
      //   · 加密：BTCUSDT/ETHUSDT 下拉（可切）；
      //   · A股：纯文本代码（代码由 A 股 picker 选，不再放一个加密下拉）。
      //
      // 这里原来有个真实的坑：A 股模式下加密下拉**没有隐藏**，于是顶栏一直写着
      // "BTCUSDT" 而画布画的是 A 股 —— 用户没法确认自己在看什么，正是"看岔"的来源。
      const symbolSelect = q("[data-testid=symbol-select]");
      if (symbolSelect) symbolSelect.hidden = state.market === MARKET_A_SHARE;
      const symbolText = q("[data-testid=topbar-symbol]");
      if (symbolText) {
        symbolText.hidden = state.market !== MARKET_A_SHARE;
        if (state.market === MARKET_A_SHARE && state.code) symbolText.textContent = state.code;
      }
      if (state.market !== MARKET_A_SHARE) {
        const nameNode = q("[data-testid=topbar-security-name]");
        if (nameNode) nameNode.hidden = true;
      }

      const interval = q("[data-testid=interval-select]");
      if (interval) {
        interval.disabled = state.market === MARKET_A_SHARE;
        // option 的 value 是**毫秒**（86400000 = 1d），写 "1d" 匹配不到任何 option，
        // select 会显示成空白（实测踩到）。
        if (state.market === MARKET_A_SHARE) interval.value = "86400000";
      }
      syncSourceLabel();
      syncRemoveButton();
    }

    function poolItems() {
      const pool = state.pool;
      return pool && Array.isArray(pool.items) ? pool.items : [];
    }

    function currentItem() {
      return poolItems().find((item) => item.code === state.code) || null;
    }

    /**
     * 一条候选的**来源标注**。这是本次需求「并注名来源」的落点：
     * 用户必须能一眼看出这只票是热门池来的、自己手输的、还是策略推荐的。
     *
     * 一只票可以同时来自多处（如既在热门池 #2 又是策略 42 分），全部列出 ——
     * 后端已经去重成一条并给了 `sources`，这里只负责拼成人话。
     */
    function sourceLabel(item) {
      const parts = [];
      if (item.manual) parts.push("手输");
      if (item.hot) {
        const rank = item.hot.rank === null || item.hot.rank === undefined ? "—" : item.hot.rank;
        parts.push(
          item.hot.source === "ladder_day"
            ? `连板${item.hot.cont_days}天`
            : `热门#${rank}`,
        );
      }
      if (item.strategy) {
        const pct = Math.round(Number(item.strategy.confidence) * 100);
        parts.push(`策略${item.strategy.score}分/置信${pct}%·${item.strategy.action}`);
      }
      return parts.join("｜");
    }

    function buildOption(item) {
      const option = document.createElement("option");
      option.value = item.code;
      const name = typeof item.name === "string" && item.name ? ` ${item.name}` : "";
      // 画不出来的票**仍然列出但标注**：直接过滤掉会让人以为池子少了票。
      //
      // R17-3 起**不再禁用**它们：本地没因子不等于画不出来 —— 点一下会触发按需
      // 拉取（腾讯有该标的后复权就能救回来）。禁用会把这条路堵死。
      // 下拉里必须带**名称**：只有六位数字时 002119/002219/002110 这种一眼看岔，
      // 而这里正是"选错票"最容易发生的地方。
      const suffix = item.drawable ? "" : "·本地无因子，可尝试拉取";
      option.textContent = `${item.code}${name}（${sourceLabel(item)}${suffix}）`;
      if (item.code === state.code) option.selected = true;
      return option;
    }

    function renderPicker() {
      const select = q("[data-testid=a-share-code-select]");
      if (!select) return;
      select.replaceChildren();
      const items = poolItems();
      if (!items.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = state.code || "（候选池不可用）";
        select.appendChild(option);
        syncSourceLabel();
        return;
      }
      // 按来源分组（后端已按 手输 → 热门池 → 策略 排好序，这里只归类不重排）。
      // 用 optgroup 而不是把分组写进文案：原生下拉里 optgroup 是唯一有视觉分组的
      // 手段，而文案那点空间要留给来源明细。
      const groups = new Map();
      items.forEach((item) => {
        const key = item.group || "hot";
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(item);
      });
      groups.forEach((list, key) => {
        const box = document.createElement("optgroup");
        box.label = GROUP_LABELS[key] || key;
        list.forEach((item) => box.appendChild(buildOption(item)));
        select.appendChild(box);
      });
      // 当前 code 不在池子里：可能是刚输入还没落盘成功、刚被移除、或池子取数失败。
      // 必须仍然显示出来，否则顶栏写着 002614、下拉却是别的票（"看岔"的来源）。
      if (state.code && !items.some((item) => item.code === state.code)) {
        const option = document.createElement("option");
        option.value = state.code;
        option.textContent = `${state.code}（不在候选池）`;
        option.selected = true;
        select.appendChild(option);
      }
      syncSourceLabel();
    }

    function syncSourceLabel() {
      const node = q("[data-testid=a-share-source]");
      if (!node) return;
      if (state.market !== MARKET_A_SHARE) {
        node.textContent = "";
        node.hidden = true;
        return;
      }
      node.hidden = false;
      if (state.saveError) {
        node.textContent = state.saveError;
        return;
      }
      const item = currentItem();
      node.textContent = item ? sourceLabel(item) : "不在候选池";
    }

    /** 「移除手输」只在当前票确实是手输项时才出现（否则点了没意义）。 */
    function syncRemoveButton() {
      const button = q("[data-testid=a-share-manual-remove]");
      if (!button) return;
      const item = currentItem();
      button.hidden = !(state.market === MARKET_A_SHARE && item && item.manual);
    }

    function describeDegraded(snapshot) {
      const runtime = snapshot && typeof snapshot === "object" ? snapshot.runtime || {} : {};
      if (runtime.degraded !== true) return null;
      const reason = String(runtime.degraded_reason || "");
      const key = reason.split(":")[0];
      const detail = reason.includes(":") ? `（${reason}）` : "";
      return (A_SHARE_REASONS[key] || `A 股数据不可用（${reason || "unknown"}）`) + detail;
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
        setStatus(`候选池加载失败：${error && error.message ? error.message : error}`);
      }
      renderPicker();
      syncRemoveButton();
      return state.pool;
    }

    /**
     * 手输落盘（``POST``）与移除（``DELETE``）。
     *
     * 这是本次需求的核心：此前手输的代码**只写进 URL 查询串**，第二次登录就没了。
     * 现在落到服务端 JSON（``~/.cache/cpt/watchlist.json``），跨会话/跨浏览器都在。
     *
     * 接口约定（``cpt/web/app.py`` 的 ``_handle_a_share_write``）：code 走 **query
     * 参数**，不是 JSON body；返回完整自选列表。
     */
    async function writeWatchlist(code, method) {
      const verb = method === "POST" ? "保存" : "移除";
      const url = `${state.base}/watchlist?code=${encodeURIComponent(code)}`;
      try {
        const response = await fetch(url, { method, headers: { Accept: "application/json" } });
        const body = await response.json().catch(() => null);
        if (!response.ok) {
          const detail = body && body.error ? body.error.message : `HTTP ${response.status}`;
          state.saveError = `${verb}失败：${detail}`;
          return false;
        }
        state.saveError = null;
        return true;
      } catch (error) {
        state.saveError = `${verb}失败：${error && error.message ? error.message : error}`;
        return false;
      }
    }

    async function submitManual(raw) {
      const code = String(raw || "").trim();
      if (!code) return;
      await writeWatchlist(code, "POST");
      // 落盘失败也要能看图（只是这次不跨登录保留）—— 原因留在 state.saveError，
      // 由来源标注显示，不依赖会被快照请求覆盖的状态栏。
      selectCode(code);
      await loadPool();
    }

    async function removeManual() {
      if (!state.code) return;
      await writeWatchlist(state.code, "DELETE");
      await loadPool();
      // 当前票仍在图上（它可能还在热门池/策略里），只是不再是手输项
      applyMarket();
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
        // 手输现在会**先落盘再切图**（submitManual），不再只写 URL
        const submit = () => submitManual(input.value);
        input.addEventListener("keydown", (event) => {
          if (event.key === "Enter") submit();
        });
        const button = q("[data-testid=a-share-code-apply]");
        if (button) button.addEventListener("click", submit);
      }
      const removeButton = q("[data-testid=a-share-manual-remove]");
      if (removeButton) removeButton.addEventListener("click", () => removeManual());
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
