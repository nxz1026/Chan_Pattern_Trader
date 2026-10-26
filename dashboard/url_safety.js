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
 * R51：画布 D 下线，漏网名单从 4 个减到 3 个（巡检面板 / 热门池 / 自选增删），
 * 统一出口本身不变。
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
 *
 * `window.CPT_URL.base(pathname?) -> string`
 *   **看板根前缀**（``/cpt/`` 形状，必带尾斜杠），运行时从 URL 推导 ——
 *   看板可挂在任意 nginx 前缀下，写死前缀会在换前缀时静默 404。
 *
 * `window.CPT_URL.path(endpoint, pathname?) -> string`
 *   把**可能是相对**的地址解析成以看板根基址为根的绝对路径；本来就是绝对的
 *   （根绝对路径 / 带 scheme 的整 URL）原样返回。
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

  /* ── 看板根基址：运行时从 URL 推导 ────────────────────────────
   *
   * ## 为什么必须推导，不能写死
   *
   * 看板可以挂在**任意** nginx 前缀下（现役 ``/cpt/``，将来的 ``/ecpt/cpt/``）。
   * 把前缀写死在 HTML/JS 里 ⇒ 换前缀时**静默 404**：页面照常渲染、数据全空，
   * 没有任何报错提示 —— 这是本仓最贵的一类 bug。
   *
   * ## 算法（一句话）
   *
   * 取 ``window.location.pathname`` 的**目录部分**；末段若是挂在看板根**下面**
   * 的子页面目录（``track/``）就再往上退一级 ⇒ 得到「看板根前缀」（必带尾斜杠）：
   *
   *     /cpt/             → /cpt/
   *     /cpt/track/       → /cpt/
   *     /ecpt/cpt/        → /ecpt/cpt/
   *     /ecpt/cpt/track/  → /ecpt/cpt/
   *
   * ⚠️ 这里列的是**页面目录名**（哪些页面挂在看板根下面），**不是** nginx 前缀 ——
   * 前缀永远从 URL 推导。写死前缀就是本节要消灭的那个坑本身。
   */
  const SUB_PAGES = ["track", "index.html"];

  function currentPathname() {
    const loc = typeof window !== "undefined" ? window.location : null;
    return (loc && loc.pathname) || "/";
  }

  /**
   * 看板根前缀（``/cpt/`` 形状，必带尾斜杠）。
   *
   * @param {string} [pathname] 传入 pathname 时按它推导（测试用）；
   *                            省略则用 ``window.location.pathname``。
   */
  function base(pathname) {
    const raw = String(pathname == null ? currentPathname() : pathname);
    const cut = raw.search(/[?#]/);
    const path = (cut >= 0 ? raw.slice(0, cut) : raw) || "/";
    const segments = path.split("/").filter(Boolean);
    if (segments.length && SUB_PAGES.indexOf(segments[segments.length - 1]) >= 0) {
      segments.pop();
    }
    return segments.length ? "/" + segments.join("/") + "/" : "/";
  }

  /**
   * 把一个**可能是相对**的地址解析成以看板根基址为根的绝对路径。
   *
   * 已经是绝对地址的（根绝对路径 ``/x``、带 scheme 的整 URL）**原样返回** ——
   * 独立部署在根路径上的服务（``/api/dashboard/...``）不能被看板前缀污染。
   */
  function path(endpoint, pathname) {
    const raw = String(endpoint == null ? "" : endpoint);
    if (!raw) return raw;
    if (
      raw.charAt(0) === "/" ||
      raw.slice(0, 2) === "//" ||
      /^[a-z][a-z0-9+.-]*:/i.test(raw)
    ) {
      return raw;
    }
    return base(pathname) + raw.replace(/^\.\//, "").replace(/^\/+/, "");
  }

  window.CPT_URL = Object.assign(window.CPT_URL || {}, {
    safe: safe,
    urlObject: urlObject,
    base: base,
    path: path,
  });
})();
