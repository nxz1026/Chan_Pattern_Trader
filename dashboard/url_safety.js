/*
 * URL 凭据消毒（R45）——**全站唯一实现**。
 *
 * ## 为什么单独一个文件
 *
 * 相对路径在**带凭据的页面**（``https://user:pwd@host/cpt/``）上解析时，
 * 会把 base 的凭据继承进结果（URL 规范如此），而 ``fetch``/``Request``
 * **按规范拒绝**带凭据的 URL：
 *
 *     Failed to execute 'fetch' on 'Window': Request cannot be constructed
 *     from a URL that includes credentials
 *
 * 后果不是报错页，而是**静默降级**：整块面板变成"暂无数据"。
 *
 * 必须在 `dashboard.js` **之前**加载 —— 与 `canvas_registry.js` 同理
 * （那个文件存在的原因就是加载顺序：本仓多个 `<script defer>` 之间
 * 有真实的先后依赖，见 `index.html` 的注释）。
 *
 * ## 为什么不直接把逻辑写进每个文件
 *
 * R45 第一次修复只覆盖了 `dashboard.js` 里的 5 个 fetch 出口，**漏了 4 个**
 * （画布 D / 巡检面板 / 热门池 / 自选增删）。若当时把同样的逻辑复制进
 * 那三个文件，就等于埋下「改了主副本、三个副本静默漂移」的地雷 ——
 * 而「多份实现漂移」正是本仓反复吃的那类亏（三个 chanlun 后端、
 * 三份 `_dbconfig`、`_try_on_demand_factors` 的两处同源 bug）。
 *
 * ## 契约
 *
 * `window.CPT_URL.safe(endpoint) -> string`
 *   解析并**清空 username / password**；解析不了（file:// 下的畸形输入等）
 *   原样返回，**绝不抛** —— 调用方都在 try/catch 里，抛出去只会变成 500。
 *
 * `window.CPT_URL.urlObject(endpoint) -> URL | null`
 *   同上但返回 ``URL`` **对象**（``dashboard.js`` 要用 ``searchParams.set``）。
 *   解析不了返回 ``null``，由调用方决定降级。
 */
(function () {
  "use strict";

  function urlObject(endpoint) {
    try {
      const url = new URL(endpoint, window.location.href);
      if (url.username || url.password) {
        url.username = "";
        url.password = "";
      }
      return url;
    } catch (error) {
      return null;
    }
  }

  function safe(endpoint) {
    const url = urlObject(endpoint);
    return url === null ? endpoint : url.toString();
  }

  window.CPT_URL = Object.assign(window.CPT_URL || {}, {
    safe: safe,
    urlObject: urlObject,
  });
})();
