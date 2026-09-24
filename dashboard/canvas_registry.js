/*
 * CPT 画布注册表（R16-5）。
 *
 * 为什么要单独一个文件、且**必须在 dashboard.js 之前加载**：
 * 画布 B/C/D 是独立文件，它们需要在加载时把自己注册进来；而 `defer` 脚本按
 * 文档顺序执行，所以注册表必须先于它们存在，`dashboard.js` 才能在 `boot()`
 * 里读到完整列表。
 *
 * 契约（四个画布必须一致）：
 *   module = {
 *     id:    "A" | "B" | "C" | "D",
 *     label: 人类可读名字（顶栏按钮用）,
 *     note:  一句话说明（审计 JSON 用）,
 *     draw(view) -> counts
 *   }
 *   counts = { canvas, candles, fractals, bis, zhongshus, trendTypes }
 *
 * `draw` 必须：
 *   1. 自己清空 `view.canvas`（以及它用不到的 volume/macd/axis 区域 —— 否则
 *      上一个画布的残留 SVG 会串台，见 `window.CPTDashboard.clearRegion`）；
 *   2. 返回**自己真正画出来的**元素个数（不是"快照里有几个"），这样审计才能
 *      用「四个画布计数两两相等」证明它们消费了同一份结构。
 */
(function () {
  "use strict";

  const registry = new Map();

  window.CPT_CANVASES = {
    register(id, module) {
      const key = String(id).toUpperCase();
      if (!module || typeof module.draw !== "function") {
        throw new Error(`画布 ${key} 缺少 draw(view)`);
      }
      registry.set(key, Object.assign({ id: key }, module));
      return key;
    },
    get(id) {
      return registry.get(String(id).toUpperCase()) || null;
    },
    has(id) {
      return registry.has(String(id).toUpperCase());
    },
    ids() {
      return Array.from(registry.keys()).sort();
    },
    list() {
      return Array.from(registry.values())
        .sort((a, b) => a.id.localeCompare(b.id))
        .map((m) => ({ id: m.id, label: m.label || m.id, note: m.note || "" }));
    },
  };
})();
