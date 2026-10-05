/**
 * 异步任务轮询的**唯一实现**（R45 P1-1）。
 *
 * ## 为什么要有这个文件
 *
 * R45 加「结构判断的 LLM 摘要」时，我**自己发明了一套**轮询：
 * 间隔、上限、失败处理、幂等读回…… 而同一个仓库里已经有
 * ``dashboard.js`` 的 3 个 ``setTimeout`` / ``setInterval`` 轮询，
 * ``market_a_share.js`` 1 个，四份代码各写各的。
 *
 * 这和当初凭据消毒的处境**一模一样**：
 * R44 发现 5 个文件里有 9 处重复实现，收敛成 ``url_safety.js`` 唯一实现，
 * 并加了门禁锁住（"新出口必须走它"）。
 *
 * ⇒ 同样的坑不踩第二遍。轮询也收敛成一处，加同样的门禁。
 *
 * ## 纪律（照抄 url_safety 的那几条）
 *
 * 1. **必须先于所有使用者加载** —— index.html 把它放在第 2 个
 *    （紧随 url_safety.js，早于 market_a_share / dashboard；
 *    R51 起不再早于 canvas_d，该文件已随画布 D 下线）。
 * 2. **缺失时要响亮失败**，不静默回退一个残缺实现 ——
 *    否则"统一"只是看起来统一。
 * 3. **一切交给调用方的判定**：本模块只管"等"和"停"，
 *    不对状态做解释（done/error 各算什么，调用方说了算）。
 *
 * 全站无外部依赖，纯浏览器 API。
 */
(function (global) {
  "use strict";

  var DEFAULTS = {
    intervalMs: 1500,
    maxTries: 40,
  };

  /**
   * 轮询一个异步任务直到调用方说停。
   *
   * @param {() => Promise<any>} fetchOnce 每轮拉一次；返回任意值，交给 onTick
   * @param {(value:any, api:{stop:Function, tries:number}) => (boolean|void)} onTick
   *        返回 `false` 停止；返回 `undefined` 继续；抛异常视为停止并交给 onFail
   * @param {{intervalMs?:number, maxTries?:number, onFail?:Function}} [opts]
   * @returns {{stop: Function, done: boolean}} 句柄
   */
  function poll(fetchOnce, onTick, opts) {
    var o = opts || {};
    var intervalMs = o.intervalMs || DEFAULTS.intervalMs;
    var maxTries = typeof o.maxTries === "number" ? o.maxTries : DEFAULTS.maxTries;
    var handle = { stopped: false, tries: 0, stop: function () { handle.stopped = true; } };

    function tick() {
      if (handle.stopped || handle.tries >= maxTries) return;
      handle.tries += 1;
      Promise.resolve()
        .then(fetchOnce)
        .then(function (value) {
          if (handle.stopped) return;
          if (onTick(value, handle) === false) handle.stop();
        })
        .catch(function (err) {
          if (o.onFail) o.onFail(err, handle);
          handle.stop();
        })
        .then(function () {
          if (!handle.stopped && handle.tries < maxTries) {
            global.setTimeout(tick, intervalMs);
          }
        });
    }

    tick();
    return handle;
  }

  global.CPTJob = { poll: poll, DEFAULTS: DEFAULTS };
})(window);
