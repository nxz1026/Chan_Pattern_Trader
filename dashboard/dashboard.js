(() => {
  "use strict";

  const root = document.querySelector("[data-testid=dashboard-root]");
  if (!root) return;

  const fieldNodes = [...root.querySelectorAll("[data-field]")];
  const setText = (selector, value) => {
    const node = root.querySelector(selector);
    if (node) node.textContent = value == null ? "—" : String(value);
  };
  const formatTime = (value) =>
    value == null ? "—" : new Date(Number(value)).toISOString().replace("T", " ").slice(0, 19);

  function render(snapshot) {
    const market = snapshot.market || {};
    const quality = snapshot.data_quality || {};
    const runtime = snapshot.runtime || {};
    fieldNodes.forEach((node) => {
      const path = node.dataset.field.split(".");
      let value = snapshot;
      path.forEach((key) => {
        value = value == null ? undefined : value[key];
      });
      node.textContent = value == null ? "—" : String(value);
    });
    setText("[data-testid=topbar-updated-at]", formatTime(market.last_open_time));
    setText("[data-testid=market-time-range]", `${formatTime(market.first_open_time)} → ${formatTime(market.last_open_time)}`);
    setText("[data-testid=market-bar-count]", market.bar_count || 0);
    setText("[data-testid=data-quality-stale]", quality.stale ? "true" : "false");
    setText("[data-testid=data-quality-gap]", quality.gap ? "true" : "false");
    setText("[data-testid=replay-schema-version]", snapshot.schema_version || "—");
    root.dataset.status = runtime.status || "empty";
    root.dataset.mode = runtime.mode || "offline";
    root.dataset.dataSource = runtime.data_source || "unknown";
  }

  async function loadSnapshot(url) {
    const loading = root.querySelector("[data-testid=state-loading]");
    const empty = root.querySelector("[data-testid=state-empty]");
    if (loading) loading.hidden = false;
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const snapshot = await response.json();
      render(snapshot);
      if (empty) empty.hidden = Boolean(snapshot.candles && snapshot.candles.length);
      root.dataset.connection = "live";
    } catch (error) {
      root.dataset.connection = "error";
      const message = root.querySelector("[data-testid=state-error-message]");
      if (message) message.textContent = `无法加载 snapshot：${error.message}`;
    } finally {
      if (loading) loading.hidden = true;
    }
  }

  window.CPTDashboard = { render, loadSnapshot };
  root.dispatchEvent(new CustomEvent("cpt:dashboard-ready"));
})();
