// dash-alert.js — CPT 看板 · 由 dashboard/dashboard.js 按功能拆分（R45）
//
// 浏览器端提醒：Notification 授权、提示音、观察器与标签同步。
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
  function installAlertObserver() {
    const alert = q("[data-testid=realtime-alert]");
    const refresh = q("[data-testid=realtime-refresh]");
    const syncOffline = () => {
      const realtime = root.dataset.runtimeMode === "realtime";
      if (alert) alert.hidden = !realtime;
      if (refresh) refresh.hidden = !realtime;
    };
    installBrowserAlerts(alert);
    root.addEventListener("cpt:realtime-updated", (event) => {
      const detail = event.detail || {};
      if (!alert) return;
      if (detail.triggered) state.lastAlertDetail = detail;
      // 保留最近一次提醒：后端只在状态切换那一轮带上 alerts，
      // 后续轮询 alerts 为空，不能因此把已显示的提醒刷回 idle。
      const shown = state.lastAlertDetail;
      if (shown) {
        alert.textContent = `信号提醒：${shown.currentStatus || "alert"}`;
        alert.dataset.state = "alert";
        if (shown.at != null) alert.setAttribute("data-alert-at", String(shown.at));
      } else {
        alert.textContent = "无新信号提醒";
        alert.dataset.state = "idle";
        alert.removeAttribute("data-alert-at");
      }
    });
    new MutationObserver(syncOffline).observe(root, { attributes: true, attributeFilter: ["data-runtime-mode"] });
    syncOffline();
  }

  // 消费 snapshot.alerts（后端 _RealtimeProvider 仅在信号状态切换那一轮填充）
  // 并转成 ``cpt:realtime-updated`` 事件——这是通知/蜂鸣/文案的唯一触发源。

  function syncAlerts(snapshot) {
    const alerts = snapshot && Array.isArray(snapshot.alerts) ? snapshot.alerts : [];
    const first = alerts.find((item) => isObject(item)) || null;
    root.dispatchEvent(
      new CustomEvent("cpt:realtime-updated", {
        detail: first
          ? {
              triggered: true,
              kind: first.kind || "signal_transition",
              currentStatus: first.status || null,
              previousStatus: first.previous_status || null,
              reason: first.reason || null,
              at: first.at == null ? null : first.at,
            }
          : { triggered: false },
      }),
    );
  }


  function isNotificationSupported() {
    return typeof window !== "undefined" && "Notification" in window;
  }


  function playAlertBeep() {
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (!AudioCtx) return;
      const ctx = new AudioCtx();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.45);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.5);
    } catch (error) {
      // 浏览器策略或权限阻止时静默失败；不应阻塞主流程。
      console.warn("playAlertBeep failed", error);
    }
  }


  function installBrowserAlerts(alertNode) {
    if (!alertNode) return;
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.testid = "enable-browser-alerts";
    button.textContent = "启用浏览器通知 + 蜂鸣";
    button.addEventListener("click", async () => {
      // denied 后 JS 再 requestPermission 也无效，避免死循环，直接引导用户去设置
      const live = isNotificationSupported() && typeof window.Notification.permission === "string"
        ? window.Notification.permission
        : "default";
      if (live === "denied") {
        state.notificationPermission = "denied";
        updateBrowserAlertsLabel();
        return;
      }
      if (live === "unsupported") {
        state.notificationPermission = "unsupported";
        updateBrowserAlertsLabel();
        return;
      }
      try {
        const permission = await window.Notification.requestPermission();
        state.notificationPermission = permission;
      } catch (error) {
        state.notificationPermission = "denied";
      }
      updateBrowserAlertsLabel();
    });
    alertNode.appendChild(button);
    if (isNotificationSupported() && typeof window.Notification.permission === "string") {
      state.notificationPermission = window.Notification.permission;
    }
    updateBrowserAlertsLabel();
    root.addEventListener("cpt:realtime-updated", (event) => {
      const detail = event.detail || {};
      if (!detail.triggered) return;
      const title = "CPT 信号变化";
      const body = `${detail.previousStatus || "—"} → ${detail.currentStatus || "alert"}`;
      const signature = `${detail.previousStatus || ""}->${detail.currentStatus || ""}@${detail.at || ""}`;
      if (signature === state.lastAlertSignature) return;
      state.lastAlertSignature = signature;
      playAlertBeep();
      if (isNotificationSupported() && window.Notification.permission === "granted") {
        try {
          new window.Notification(title, { body, tag: "cpt-realtime-alert" });
        } catch (error) {
          console.warn("Notification failed", error);
        }
      }
    });
  }


  function updateBrowserAlertsLabel() {
    const button = q("[data-testid=enable-browser-alerts]");
    if (!button) return;
    const permission = state.notificationPermission;
    if (permission === "granted") {
      button.textContent = "浏览器通知已启用（点击重试蜂鸣）";
      button.setAttribute("title", "点击只重试蜂鸣，不再重复弹授权框");
    } else if (permission === "denied") {
      button.textContent = "浏览器通知已被拒绝 · 请到站点设置开启";
      button.setAttribute("title", "浏览器 Notification.permission 已是 denied，无法用 JS 再弹授权；请到地址栏左侧锁形/站点设置中放行通知权限后刷新页面");
    } else if (permission === "unsupported") {
      button.textContent = "当前环境不支持浏览器通知（蜂鸣仍可用）";
      button.setAttribute("title", "Notification API 不存在；蜂鸣仍可触发");
    } else {
      button.textContent = "启用浏览器通知 + 蜂鸣";
      button.setAttribute("title", "首次点击会弹浏览器授权框；选择「允许」即可收到信号变化通知");
    }
  }

// >>>FUNCS
})();
