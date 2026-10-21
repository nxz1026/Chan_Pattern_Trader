// dash-alert.js — CPT 看板 · 由 dashboard/dashboard.js 按功能拆分（R45）
//
// 浏览器端提醒：Notification 授权、提示音、观察器与标签同步。
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
