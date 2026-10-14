// dash-chrome.js — CPT 看板 · 由 dashboard/dashboard.js 按功能拆分（R45）
//
// 顶栏与全局状态：renderChrome（342 行串行 setText）+ 顶栏控件。
// 
// ⚠️ renderChrome 是**每次轮询都跑**的热路径，但它是「顺序 DOM 写入」
// 而不是复杂逻辑 —— **拆分不会让它变快**（提速要加 dirty check，
// 那是行为改动，风险高得多）。
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
  // 实时报价源中文名（``dual_compare.realtime.source``）。R35 换源后上游是新浪，
  // 面板上写死指向东财的那个标签已经指错地方。
  const SOURCE_LABELS = { sina: "新浪", eastmoney: "东财", tencent: "腾讯" };
// >>>HEAD
// <<<FUNCS
  function renderChrome(snapshot) {
    const market = isObject(snapshot) && isObject(snapshot.market) ? snapshot.market : {};
    const quality = isObject(snapshot) && isObject(snapshot.data_quality) ? snapshot.data_quality : {};
    const runtime = isObject(snapshot) && isObject(snapshot.runtime) ? snapshot.runtime : {};
    const candles = normalizeCandles(snapshot && snapshot.candles);
    // A 股模式标记：这块面板里「24h / USDT / 东财」都是加密侧遗产，A 股要换口径。
    const isAShare = market.kind === "a_share";
    const lastBarDate = typeof market.last_bar_date === "string" ? market.last_bar_date : "";
    // 上游 24h 聚合（A 股恒为 available=false）。原来在涨跌幅与量额两处各声明一次，
    // 涨跌幅现在要参与回落链，提到函数顶部统一声明。
    const market24h = isObject(snapshot) && isObject(snapshot.market_24h) ? snapshot.market_24h : null;
    const market24hAvailable = market24h && market24h.available === true;

    root.querySelectorAll("[data-field]").forEach((node) => {
      node.textContent = formatFieldValue(node.dataset.field, resolvePath(snapshot, node.dataset.field));
    });

    // 板块：``market.board`` 是 ``security_master.board`` 的裸值（SH / SZ /
    // SZ_CHINEXT），而 SH 把主板与科创板混在一起（688981 中芯国际也是 SH）——
    // 光一个 "SH" 分不出科创板。后端补了带中文名的 ``market.board_label``，
    // 有就用它，原始板号放 title 里方便对库。
    const boardNode = q("[data-testid=market-board]");
    if (boardNode) {
      const boardCode = typeof market.board === "string" ? market.board.trim() : "";
      const boardLabel = typeof market.board_label === "string" ? market.board_label.trim() : "";
      if (boardLabel && boardLabel !== boardCode) {
        boardNode.textContent = boardLabel;
      }
      if (boardCode) boardNode.setAttribute("title", `security_master.board = ${boardCode}`);
    }

    // 证券名称（A 股有，加密没有）。**不放进 data-field 绑定**：那套把 undefined
    // 格式化成 "—"，加密模式下会在顶栏留一个孤零零的破折号。这里空则直接隐藏。
    //
    // 之所以要在顶栏显示名称：A 股只有六位数字，600519/600815 这种一眼看岔，
    // 而"看的是不是我想的那只票"是看盘第一件要确认的事。
    const securityName = typeof market.name === "string" ? market.name.trim() : "";
    const nameNode = setText("[data-testid=topbar-security-name]", securityName);
    if (nameNode) {
      nameNode.hidden = securityName === "";
      const board = typeof market.board === "string" ? market.board.trim() : "";
      nameNode.setAttribute("title", board ? `${securityName} · ${board}` : securityName);
    }

    // 页面标题也带上标的：同时开几个标签页时，"看岔"就发生在标签栏这一层。
    // 名称放最前面——标签被截断时先看到的是它。
    const titleSymbol = typeof market.symbol === "string" ? market.symbol.trim() : "";
    if (titleSymbol) {
      document.title = securityName ? `${securityName} ${titleSymbol} · CPT` : `${titleSymbol} · CPT`;
    }

    const firstOpen = num(market.first_open_time);
    const lastOpen = num(market.last_open_time);
    // 更新时间读 snapshot 的生成时刻（runtime.generated_at），不是 K 线窗口起点；
    // 这样 30s 轮询时显示会真实滚动，反映 snapshot 何时被生成。
    const generatedAt = num(runtime.generated_at) || num(snapshot && snapshot.reproducibility && snapshot.reproducibility.generated_at);
    const updatedNode = setText(
      "[data-testid=topbar-updated-at]",
      generatedAt !== null ? formatDateTime(generatedAt) : "—",
    );
    if (updatedNode) {
      updatedNode.setAttribute(
        "title",
        generatedAt !== null
          ? "snapshot 生成时刻（runtime.generated_at）"
          : "上游未注入 generated_at 时间戳",
      );
    }
    // 面板标题：A 股这块讲的是**单只股票**，「市场概览 / 24h」是加密侧的措辞。
    const marketTitleNode = q("#market-overview-title");
    if (marketTitleNode) marketTitleNode.textContent = isAShare ? "个股概览" : "市场概览";
    // 时间范围：日线及以上级别不再输出 ``:00:00``。``formatAxisTime`` 早就按这个
    // 口径渲染坐标轴，只有这里还在用 ``formatDateTime`` ——
    // ``2026-04-17 00:00:00 → 2026-10-13 00:00:00`` 里的时间部分全是噪音，
    // 而且全站 UTC 展示会让「00:00:00」被读成"当天零点"。
    const rangeIntervalMs = num(market.interval_ms) || 0;
    const rangeFormatter =
      rangeIntervalMs >= 86400000
        ? (ms) => formatAxisTime(ms, { showDate: true, showTime: false, showYear: true })
        : formatDateTime;
    setText("[data-testid=market-time-range]", firstOpen === null || lastOpen === null
      ? "—"
      : `${rangeFormatter(firstOpen)} → ${rangeFormatter(lastOpen)}`);
    setText("[data-testid=market-bar-count]", candles.length);
    setText("[data-testid=replay-schema-version]", (isObject(snapshot) && snapshot.schema_version) || SCHEMA_VERSION);

    const runtimeStale = runtime.stale === true;
    const stale = quality.stale === true || runtimeStale;
    const gap = quality.gap === true;
    setState(setText("[data-testid=data-quality-stale]", stale ? "true" : "false"), stale ? "true" : "false");
    setState(setText("[data-testid=data-quality-gap]", gap ? "true" : "false"), gap ? "true" : "false");
    // A 股数据质量口径：``closed_bar_count``/``unclosed_bar_count`` 在 A 股恒为
    // 119/0（日线批 16:00 跑完，库里不可能有未收盘 bar），顶着「已收盘/未收盘」
    // 两行是纯噪音，还让人以为盘中也是这套计数。改成窗口交易日/期望交易日，
    // 并把 stale 为什么恒 false 写进 title —— 否则「数据陈旧」永远显示 false，
    // 看着像这个指标坏了。
    const closedLabel = q("[data-testid=data-quality-closed-label]");
    const unclosedLabel = q("[data-testid=data-quality-unclosed-label]");
    if (closedLabel) closedLabel.textContent = isAShare ? "窗口交易日" : "已收盘";
    if (unclosedLabel) unclosedLabel.textContent = isAShare ? "期望交易日" : "未收盘";
    const staleNode = q("[data-testid=data-quality-stale]");
    if (staleNode && isAShare) {
      staleNode.setAttribute("title", "A 股日线快照批每次重写整段窗口，不存在「陈旧未刷新」态");
    }

    const reason = quality.reason;
    setText("[data-testid=data-quality-reason]", typeof reason === "string" && reason ? reason : "—");
    const factorFetch = isObject(quality.factor_fetch) ? quality.factor_fetch : null;
    if (factorFetch) {
      const fetched = factorFetch.fetched === true;
      const rows = factorFetch.rows != null ? `${factorFetch.rows} 行` : "";
      const reasonSuffix = typeof factorFetch.reason === "string" ? ` (${factorFetch.reason})` : "";
      setText("[data-testid=data-quality-factor-fetch]", fetched ? `已获取 ${rows}`.trim() : `未获取${reasonSuffix}`);
    } else {
      setText("[data-testid=data-quality-factor-fetch]", "—");
    }

    // C7：数据质量明细（severity / 缺口数 / 乱序数 + 明细列表）。
    // 后端这几个字段缺失（老快照）时整块隐藏，既有输出一个字不动。
    const qualityList = q("[data-testid=data-quality] .quality-list");
    if (qualityList) {
      const upsertQualityRow = (testid) => {
        let node = q(`[data-testid=${testid}]`);
        if (!node) {
          node = document.createElement("li");
          node.className = "quality-wide";
          node.dataset.testid = testid;
          qualityList.appendChild(node);
        }
        return node;
      };
      const severity = typeof quality.severity === "string" ? quality.severity : "";
      const gapCount = num(quality.gap_count);
      const outOfOrderCount = num(quality.out_of_order_count);
      const gaps = asArray(quality.gaps);
      const outOfOrder = asArray(quality.out_of_order);

      const detailRow = upsertQualityRow("data-quality-detail");
      if (!severity && gapCount === null && outOfOrderCount === null) {
        detailRow.hidden = true;
        detailRow.replaceChildren();
      } else {
        detailRow.hidden = false;
        // 严重度直接用后端 severity 标 data-state；缺省时按既有 gap/stale 口径兜底。
        detailRow.dataset.state = severity || (gap ? "gap" : stale ? "stale" : "ok");
        const label = document.createElement("span");
        label.textContent = "质量明细";
        const value = document.createElement("span");
        value.dataset.testid = "data-quality-detail-text";
        const parts = [];
        if (severity) parts.push(`severity ${severity}`);
        if (gapCount) parts.push(`缺口 ${gapCount} 处`);
        if (outOfOrderCount) parts.push(`乱序 ${outOfOrderCount} 处`);
        value.textContent = parts.length ? parts.join(" · ") : "无异常";
        detailRow.replaceChildren(label, value);
      }

      const listRow = upsertQualityRow("data-quality-detail-list");
      const describeQualityEntry = (kind, entry) => {
        if (isObject(entry)) {
          const start = num(entry.start_ms);
          const end = num(entry.end_ms);
          if (start !== null || end !== null) {
            return `${kind} · ${start === null ? "—" : formatDateTime(start)} → ${end === null ? "—" : formatDateTime(end)}`;
          }
          const at = num(entry.open_time);
          if (at !== null) return `${kind} · ${formatDateTime(at)}`;
          return `${kind} · ${JSON.stringify(entry)}`;
        }
        return `${kind} · ${String(entry)}`;
      };
      const entries = [
        ...gaps.map((entry) => describeQualityEntry("缺口", entry)),
        ...outOfOrder.map((entry) => describeQualityEntry("乱序", entry)),
      ];
      if (!entries.length) {
        listRow.hidden = true;
        listRow.replaceChildren();
      } else {
        listRow.hidden = false;
        listRow.dataset.state = "gap";
        const detailList = document.createElement("ul");
        detailList.dataset.testid = "data-quality-detail-entries";
        entries.slice(0, 20).forEach((text) => {
          const item = document.createElement("li");
          item.textContent = text;
          detailList.appendChild(item);
        });
        if (entries.length > 20) {
          const more = document.createElement("li");
          more.textContent = `… 仅显示前 20 条（共 ${entries.length} 条）`;
          detailList.appendChild(more);
        }
        listRow.replaceChildren(detailList);
      }
    }

    // T+1 交易日历（A 股专属）：快照未带上这一块时整行隐藏，加密模式不受影响。
    const tPlusOne = isObject(snapshot) && isObject(snapshot.t_plus_one) ? snapshot.t_plus_one : null;
    const tPlusOneNode = q("[data-testid=t-plus-one]");
    if (tPlusOneNode) {
      if (tPlusOne && tPlusOne.available !== undefined) {
        tPlusOneNode.hidden = false;
        const available = tPlusOne.available === true;
        const today = typeof tPlusOne.today === "string" ? tPlusOne.today : null;
        const nextDate = typeof tPlusOne.next_trade_date === "string" ? tPlusOne.next_trade_date : null;
        const reason = typeof tPlusOne.reason === "string" ? tPlusOne.reason : "";
        let statusText = available ? "今日可买" : "今日不可买";
        if (today) statusText += `（${today}）`;
        if (!available && nextDate) statusText += `，下一交易日 ${nextDate}`;
        setText("[data-testid=t-plus-one-status]", statusText);
        tPlusOneNode.dataset.state = available ? "ok" : "blocked";
        tPlusOneNode.setAttribute("title", reason || "");
      } else {
        tPlusOneNode.hidden = true;
      }
    }

    // 历史区间查询（replay/range 模式）：live 模式快照无 range 字段 → 整行隐藏。
    const rangeInfo = isObject(snapshot) && isObject(snapshot.range) ? snapshot.range : null;
    const rangeNode = q("[data-testid=range-query]");
    if (rangeNode) {
      const rangeStart = rangeInfo ? num(rangeInfo.start_ms) : null;
      const rangeEnd = rangeInfo ? num(rangeInfo.end_ms) : null;
      if (rangeInfo && rangeStart !== null && rangeEnd !== null) {
        rangeNode.hidden = false;
        const rangeAvailable = rangeInfo.available === true;
        const rangeCount = rangeInfo.bar_count != null ? `（${rangeInfo.bar_count} 根）` : "";
        const rangeReason =
          typeof rangeInfo.reason === "string" && rangeInfo.reason
            ? RANGE_REASON_LABELS[rangeInfo.reason] || rangeInfo.reason
            : "";
        let rangeText = `${formatDateTime(rangeStart)} → ${formatDateTime(rangeEnd)}${rangeCount}`;
        if (!rangeAvailable && rangeReason) rangeText += ` · ${rangeReason}`;
        setText("[data-testid=range-query-status]", rangeText);
        rangeNode.dataset.state = rangeAvailable ? "ok" : "blocked";
        rangeNode.setAttribute(
          "title",
          rangeAvailable ? "本次历史区间查询返回的 K 线区间" : `区间查询未成功：${rangeReason || "原因未知"}`,
        );
      } else {
        rangeNode.hidden = true;
      }
    }

    const lastPrice = num(market.last_price);
    setText("[data-testid=market-last-price]", lastPrice === null ? "—" : formatPrice(lastPrice));
    const last = candles.length ? candles[candles.length - 1] : null;
    const first = candles.length ? candles[0] : null;
    // R61：后复权价旁边**同屏**给出不复权价。面板上的 600519 是 8151.84，而不复权
    // 收盘 1274.89 —— 用户拿前者去行情软件里找不到、也挂不了单，第一反应是
    // 「数据错了」。两套尺子都得摆出来，口径说明放下面「价格口径」那一行。
    // 后端取不到当日因子时**不给**这两个字段（不伪造 ×1.0），此处整行隐藏。
    const priceRatio = num(market.price_ratio);
    const lastPriceRaw = num(market.last_price_raw);
    const rawPriceNode = setText(
      "[data-testid=market-last-price-raw]",
      lastPriceRaw === null ? "" : `（${formatPrice(lastPriceRaw)}）`,
    );
    if (rawPriceNode) {
      rawPriceNode.hidden = lastPriceRaw === null;
      rawPriceNode.setAttribute(
        "title",
        lastPriceRaw === null
          ? "缺当日复权因子，无法给出不复权价（不显示假倍率）"
          : `不复权价（后复权 ÷ 同一根 K 线的因子 ×${priceRatio}）`,
      );
    }
    const basisNode = setText(
      "[data-testid=market-price-basis]",
      isAShare && priceRatio !== null
        ? `后复权 ×${priceRatio}（括号内为不复权）`
        : isAShare
          ? "后复权（因子未知）"
          : "—",
    );
    const basisLabel = q("[data-testid=market-price-basis-label]");
    const basisRowShown = isAShare;
    if (basisNode) basisNode.hidden = !basisRowShown;
    if (basisLabel) basisLabel.hidden = !basisRowShown;
    // 数据截止日：A 股快照批 16:00（北京）跑，盘中运行时库里**没有今天**的 bar，
    // 而顶栏「更新时间」读的是 snapshot 生成时刻、一直滚动 —— 看着像实时数据。
    // 把数据本身的日期写出来，是这里唯一能防"看岔一天"的东西。
    const asofNode = setText("[data-testid=market-data-asof]", lastBarDate || "—");
    const asofLabel = q("[data-testid=market-data-asof-label]");
    if (asofNode) asofNode.hidden = !isAShare;
    if (asofLabel) asofLabel.hidden = !isAShare;
    if (asofNode && isAShare) {
      asofNode.setAttribute("title", "末根 K 线所属交易日（数据本身截止到这天，不是快照生成时刻）");
    }
    const dualCompare = isObject(snapshot) && isObject(snapshot.dual_compare) ? snapshot.dual_compare : null;
    const realtime = dualCompare && dualCompare.available === true && isObject(dualCompare.realtime)
      ? dualCompare.realtime
      : null;
    // 实时报价只有**比末根 bar 更新**（quote_date 更晚）时才算"今日涨跌"：
    // 盘中快照的末根 bar 还是昨天，此时上游现价才是今天的口径；反过来若上游
    // 给的还是昨天那根，用它就与 K 线重复且口径不一。
    const realtimeDate = realtime && typeof realtime.quote_date === "string" ? realtime.quote_date : "";
    const realtimeIsNewer = !!realtime && realtimeDate !== "" && (lastBarDate === "" || realtimeDate > lastBarDate);
    let changePct = market24hAvailable ? num(market24h.price_change_pct) : null;
    let changeSource = changePct === null ? null : "24h";
    let changeAmount = null;
    if (changePct === null && realtimeIsNewer) {
      const realtimePct = num(realtime.change_pct);
      if (realtimePct !== null) {
        changePct = realtimePct;
        changeSource = "realtime";
      }
    }
    const prevBar = candles.length > 1 ? candles[candles.length - 2] : null;
    if (changePct === null && last && prevBar) {
      const prevClose = num(prevBar.close);
      if (prevClose !== null && prevClose > 0) {
        changePct = ((last.close - prevClose) / prevClose) * 100;
        changeSource = "bars";
      }
    }
    // 涨跌额：一律按**不复权元**给（行情软件里就是这个数，挂单也用得上）。
    //
    // ⚠️ 上游 ``dual_compare.realtime`` 里 **``prev_close`` 是后复权域的**
    // （实测 600519/2026-10-14：``prev_close=8151.84`` = 后复权收盘），只有
    // ``raw_price`` 是不复权的。直接 ``raw_price - prev_close`` 会得到
    // ``1271.77 - 8151.84 = −6880.07`` —— 一个"跌了 6880 元"的茅台。
    // 昨收必须**先除回因子**：用上游随报价一起给的 ``hfq_factor``（它与
    // ``price``/``raw_price`` 同源，除权日也不会跟 K 线那根错位），
    // 次选 K 线口径的 ``price_ratio``。
    const realtimeRawPrice = realtime ? num(realtime.raw_price) : null;
    const realtimeRawPrev = realtime ? num(realtime.prev_close) : null;
    if (changeSource === "realtime" && realtimeRawPrice !== null && realtimeRawPrev !== null) {
      const quoteFactor = num(realtime.hfq_factor);
      const divisor =
        quoteFactor !== null && quoteFactor > 0 ? quoteFactor : priceRatio !== null && priceRatio > 0 ? priceRatio : null;
      if (divisor !== null) changeAmount = realtimeRawPrice - realtimeRawPrev / divisor;
    } else if (changeSource === "bars" && lastPriceRaw !== null && changePct !== null && changePct > -100) {
      // K 线分支只有后复权序列，用 raw 现价反推 raw 昨收（同一根 bar 的因子约掉）。
      changeAmount = lastPriceRaw - lastPriceRaw / (1 + changePct / 100);
    }
    const changeNode = q("[data-testid=market-change]");
    if (changeNode) {
      changeNode.textContent =
        changePct === null ? "—" : `${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`;
      changeNode.dataset.state = changePct === null ? "flat" : changePct >= 0 ? "up" : "down";
      changeNode.dataset.source = changeSource || "unavailable";
      const changeTitle =
        changeSource === "24h"
          ? "上游 24h 涨跌幅"
          : changeSource === "realtime"
            ? `实时涨跌幅（含当日盘中，来源 ${realtime.source || "upstream"} ${realtimeDate}）`
            : changeSource === "bars"
              ? `末两根 K 线涨跌幅（数据截止 ${lastBarDate || "—"}）`
              : "涨跌幅不可用";
      changeNode.setAttribute("title", changeTitle);
    }
    const changeAmountNode = setText(
      "[data-testid=market-change-amount]",
      // ⚠️ 符号必须自己拼：``formatNumber`` 内部是 ``Math.abs(value)``（千分位格式化
      // 用），``formatPrice(-3.12)`` 返回 ``"3.12"`` —— 跌了 3 块会显示成涨了 3 块。
      changeAmount === null
        ? ""
        : `${changeAmount >= 0 ? "+" : "-"}${formatPrice(Math.abs(changeAmount))}`,
    );
    if (changeAmountNode) {
      changeAmountNode.hidden = changeAmount === null;
      changeAmountNode.dataset.state =
        changeAmount === null ? "flat" : changeAmount >= 0 ? "up" : "down";
      changeAmountNode.setAttribute("title", "涨跌额（不复权，元）");
    }
    const countdownNode = q("[data-testid=close-countdown]");
    if (countdownNode) {
      // R34：这里是 `close_countdown`（snake_case），不是 `closeCountdown`。
      // 写错的代价特别隐蔽：取到 undefined → 三个分支全落空 → 掉进
      // `else if (last)` 显示「距下一根 K 线」，**一个看着挺合理的错标签**，
      // 而服务算好的 available/reason 永远没人读。
      const cc = snapshot.close_countdown;
      if (cc && cc.available && cc.is_open) {
        const mins = Math.floor(cc.seconds_to_close / 60);
        const secs = cc.seconds_to_close % 60;
        countdownNode.textContent = `${mins}m ${secs}s`;
        countdownNode.title = `距 A 股收盘 (${cc.close_time})`;
      } else if (cc && !cc.available) {
        countdownNode.textContent = "—";
        countdownNode.title = cc.reason === "not_a_trade_day" ? "今日非交易日" : "收盘倒计时不可用";
      } else if (last) {
        const interval = num(market.interval_ms) || 300000;
        const remaining = Math.max(0, last.openTime + interval - Date.now());
        countdownNode.textContent = `${Math.floor(remaining / 60000)}m ${Math.floor((remaining % 60000) / 1000)}s`;
        countdownNode.title = "距下一根 K 线";
      }
    }
    // 双数据集对比（R21 Phase 6 P2）
    const dualSection = q("[data-testid=dual-compare]");
    if (dualSection) {
      // R34：同上，服务发的是 `dual_compare`。写错的后果是
      // `dc.available` 恒为 undefined → 走 else → `dualSection.hidden = true`，
      // **整个双数据集对比面板永久隐藏**，而且没有任何报错。
      const dc = dualCompare;
      if (dc && dc.available) {
        dualSection.hidden = false;
        setText("[data-testid=dual-cpt-close]", dc.cpt_close ? formatPrice(dc.cpt_close) : "—");
        setText("[data-testid=dual-realtime-price]", dc.realtime_price ? formatPrice(dc.realtime_price) : "—");
        // R62：这里原本判的是 `div === null`。判 null 不判类型 —— 键**缺失**（undefined）
        // 会走到 `div.toFixed()` 抛 TypeError，异常被 render() 的上层 catch 吞掉，
        // 表现为「这一行还是旧的，页面毫无报错」（R61 bug ② 的同款失效模式）。
        // `Number.isFinite` 一并挡掉 NaN 与非数字（NaN 会渲染成 "NaN%"）。
        const div = dc.divergence_pct;
        setText(
          "[data-testid=dual-divergence]",
          Number.isFinite(div) ? `${div >= 0 ? "+" : ""}${div.toFixed(2)}%` : "—",
        );
        // R35 换源后上游是**新浪**（东财在生产的日本机器上实测 502）。标签必须跟着
        // 改：把源名写死会让人去一个根本不存在的源上排查问题。
        const rtInfo = isObject(dc.realtime) ? dc.realtime : null;
        const rtLabel = q("[data-testid=dual-realtime-label]");
        if (rtLabel) {
          const source = rtInfo && typeof rtInfo.source === "string" ? rtInfo.source : "";
          rtLabel.textContent = source ? `实时价（${SOURCE_LABELS[source] || source}）` : "实时价";
        }
        const rtPriceNode = q("[data-testid=dual-realtime-price]");
        if (rtPriceNode && rtInfo) {
          const stamp = [rtInfo.quote_date, rtInfo.quote_time]
            .filter((value) => typeof value === "string" && value)
            .join(" ");
          rtPriceNode.setAttribute("title", stamp ? `上游实时快照 ${stamp}` : "上游实时快照");
        }
      } else {
        dualSection.hidden = true;
      }
    }
    const previous = candles.length > 1 ? candles[candles.length - 2] : null;
    const trendDirection = last && previous ? (last.close > previous.close ? 1 : last.close < previous.close ? -1 : 0) : 0;
    setState(
      setText(
        "[data-testid=market-price-direction]",
        last && previous ? (trendDirection === 1 ? "上涨" : trendDirection === -1 ? "下跌" : "持平") : "持平",
      ),
      last && previous ? directionState(trendDirection) : "flat",
    );

    // 最高/最低：A 股改用**末根 K 线的当日高低**。``market_24h`` 在 A 股恒为
    // available=false，于是一整年都显示"不可用"——而当天的高低从来没缺过，
    // 就在快照的末根 bar 里。价格按「后复权（不复权）」两个口径一起给。
    const highLabel = q("[data-testid=market-high-label]");
    const lowLabel = q("[data-testid=market-low-label]");
    if (highLabel) highLabel.textContent = isAShare ? "当日最高" : "24h 最高";
    if (lowLabel) lowLabel.textContent = isAShare ? "当日最低" : "24h 最低";
    const formatBasisPrice = (hfq, raw) =>
      raw === null ? formatPrice(hfq) : `${formatPrice(hfq)}（${formatPrice(raw)}）`;
    const lastHigh = num(market.last_high);
    const lastHighRaw = num(market.last_high_raw);
    const lastLow = num(market.last_low);
    const lastLowRaw = num(market.last_low_raw);
    const high24 = setText(
      "[data-testid=market-high-24h]",
      isAShare
        ? lastHigh === null ? "—" : formatBasisPrice(lastHigh, lastHighRaw)
        : market24hAvailable ? formatPrice(market24h.high) : "不可用",
    );
    if (high24) {
      high24.setAttribute(
        "title",
        isAShare
          ? `末根 K 线（${lastBarDate || "—"}）当日最高；括号内为不复权`
          : market24hAvailable ? "上游 24h 聚合" : "上游未提供真实 24h 聚合",
      );
    }
    const low24 = setText(
      "[data-testid=market-low-24h]",
      isAShare
        ? lastLow === null ? "—" : formatBasisPrice(lastLow, lastLowRaw)
        : market24hAvailable ? formatPrice(market24h.low) : "不可用",
    );
    if (low24) {
      low24.setAttribute(
        "title",
        isAShare
          ? `末根 K 线（${lastBarDate || "—"}）当日最低；括号内为不复权`
          : market24hAvailable ? "上游 24h 聚合" : "上游未提供真实 24h 聚合",
      );
    }
    // 成交量：A 股原来把 118 根 bar 相加显示成 ``4.85M`` —— 既不是当日量、又不带
    // 单位（``daily_bar.volume`` 的单位是**手**），还和旁边的「K 线根数」打架。
    // 改成末根 bar 的当日量（手/万手）+ 当日成交额（亿元），两者都有现成字段。
    const quoteVolume = market24hAvailable ? Number(market24h.quote_volume) : NaN;
    const volumeLabel = q("[data-testid=market-volume-label]");
    if (volumeLabel) volumeLabel.textContent = isAShare ? "当日成交" : "成交量";
    const volumeNode = q("[data-testid=market-volume]");
    if (volumeNode) {
      const lots = num(market.last_volume_lots);
      const amount = num(market.last_amount);
      if (isAShare && (lots !== null || amount !== null)) {
        const parts = [];
        if (lots !== null) {
          parts.push(lots >= 1e4 ? `${(lots / 1e4).toFixed(2)} 万手` : `${formatNumber(lots, 0)} 手`);
        }
        if (amount !== null) parts.push(`${(amount / 1e8).toFixed(2)} 亿元`);
        volumeNode.textContent = parts.join(" / ");
        volumeNode.setAttribute(
          "title",
          `末根 K 线（${lastBarDate || "—"}）当日成交量 / 成交额；1 手 = 100 股`,
        );
        volumeNode.dataset.source = "last_bar";
      } else if (Number.isFinite(quoteVolume)) {
        volumeNode.textContent = `${formatVolume(quoteVolume)} USDT`;
        volumeNode.setAttribute("title", "上游 24h USDT 成交额");
        volumeNode.dataset.source = "24h";
      } else if (candles.length) {
        const totalVolume = candles.reduce(
          (sum, bar) => sum + (bar.volume === null ? 0 : bar.volume),
          0
        );
        volumeNode.textContent = formatVolume(totalVolume);
        volumeNode.setAttribute("title", "当前 snapshot 窗口累计成交量（非 24h）");
        volumeNode.dataset.source = "window";
      } else {
        volumeNode.textContent = "—";
        volumeNode.setAttribute("title", "上游未提供 24h 成交额");
        volumeNode.dataset.source = "none";
      }
    }

    // 上游降级：后端在 fetch 失败 / 数据守卫拒绝时返回带 degraded 标记的空快照。
    // 必须显式呈现，否则用户只看到「图表保持空占位」，无法区分「上游挂了」和
    // 「本来就没数据」。degraded 优先于 empty 态。
    const degraded = runtime.degraded === true;
    const degradedReason =
      typeof runtime.degraded_reason === "string" ? runtime.degraded_reason : "";

    let stateKey = "empty";
    if (candles.length) {
      if (gap) stateKey = "gap";
      else if (stale) stateKey = "stale";
      else stateKey = runtime.status === "alert" ? "alert" : "confirmed";
    }
    if (degraded) stateKey = "degraded";
    setState(setText("[data-testid=topbar-status]", stateKey), stateKey);
    // 降级时不显示「暂无数据」空态：其文案是「离线 demo」，会掩盖真实原因
    setHidden("[data-testid=state-empty]", candles.length > 0 || degraded);
    setHidden("[data-testid=state-stale]", !(candles.length && stale));
    setHidden("[data-testid=state-gap]", !(candles.length && gap));

    root.dataset.status = stateKey;
    const dataSource = typeof runtime.data_source === "string" ? runtime.data_source.toLowerCase() : "";
    const isRealtime = dataSource.includes("realtime") || dataSource.includes("binance");
    root.dataset.runtimeMode = isRealtime ? "realtime" : "offline";
    root.dataset.viewMode = state.mode;
    root.dataset.dataSource = typeof runtime.data_source === "string" ? runtime.data_source : "unknown";

    root.dataset.degraded = degraded ? "true" : "false";
    if (degraded) {
      root.dataset.degradedReason = degradedReason || "unknown";
      showDegraded(degradedReason);
    } else {
      root.removeAttribute("data-degraded-reason");
      setHidden("[data-testid=state-degraded]", true);
      if (!candles.length) {
        setConnection("offline", emptyMessage(snapshot));
      } else if (!isRealtime) {
        setConnection("offline", `离线 snapshot（${candles.length} 根 K 线，${runtime.data_source || "fixture"}）· 无网络请求`);
      } else {
        setConnection("live", `实时 snapshot（${candles.length} 根 K 线，binance_realtime）`);
      }
    }

    // 结构预警（A 股专属）：summary.structural_alert = 笔序列首次跌破前低。
    // 加密快照没有这个字段 → 恒为 false，横幅保持 hidden。
    const summary = isObject(snapshot) && isObject(snapshot.summary) ? snapshot.summary : {};
    const structuralAlert = summary.structural_alert === true;
    setHidden("[data-testid=structural-alert]", !structuralAlert);
  }

  /* ---------------------------------------------------------- 结构面板填充 */


  function updateZoomLabel() {
    const label = q("[data-testid=chart-zoom-level]");
    if (!label) return;
    const candles = state.snapshot ? normalizeCandles(state.snapshot.candles) : [];
    const total = candles.length;
    if (!total) {
      label.textContent = "全部";
    } else if (state.visibleWindow) {
      label.textContent = `${applyZoomWindow(candles).length} / ${total}`;
    } else if (total > DEFAULT_VISIBLE_BARS) {
      label.textContent = `最近 ${DEFAULT_VISIBLE_BARS} / ${total}`;
    } else {
      label.textContent = `${total} / ${total}`;
    }
  }

  /** 结构是否落在未收盘区间：snapshot 标记 alert 且结构覆盖了最后一根 K 线。 */
  const structureStateOf = (view, endTime) => {
    const runtime = isObject(state.snapshot) && isObject(state.snapshot.runtime) ? state.snapshot.runtime : {};
    if (runtime.status !== "alert") return "confirmed";
    return endTime !== null && endTime >= view.lastOpenTime ? "alert" : "confirmed";
  };


  function installSymbolSwitch() {
    const select = q("[data-testid=symbol-select]");
    if (!select) return;
    select.addEventListener("change", () => {
      const symbol = select.value;
      setText("[data-testid=topbar-symbol]", symbol);
      root.dataset.symbol = symbol;
      setConnection("connecting", `正在切换交易对：${symbol}`);
      root.dispatchEvent(new CustomEvent("cpt:symbol-changed", { detail: { symbol } }));
      refreshSelectedSnapshot();
    });
  }


  function installIntervalSwitch() {
    const select = q("[data-testid=interval-select]");
    if (!select) return;
    select.addEventListener("change", () => {
      const option = select.selectedOptions && select.selectedOptions[0];
      const label = option && typeof option.textContent === "string" && option.textContent.trim() !== ""
        ? option.textContent.trim()
        : String(select.value);
      setText("[data-testid=topbar-interval]", label);
      root.dataset.intervalMs = select.value;
      setConnection("connecting", `正在切换周期：${label}`);
      root.dispatchEvent(new CustomEvent("cpt:interval-changed", { detail: { intervalMs: Number(select.value), label } }));
      refreshSelectedSnapshot();
    });
  }

  /** 本地时区的 `datetime-local` 文本（YYYY-MM-DDTHH:mm），与手动输入的解析口径一致。 */
  const toLocalInputValue = (ms) => {
    const pad = (value) => String(value).padStart(2, "0");
    const date = new Date(ms);
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };

  /** 历史区间会话：固定区间后停止自动刷新，避免实时快照把固定窗口冲掉。 */

  function installTimeRange() {
    const startInput = q("[data-testid=range-start]");
    const endInput = q("[data-testid=range-end]");
    const applyButton = q("[data-testid=range-apply]");
    const liveButton = q("[data-testid=range-live]");
    if (!applyButton && !liveButton) return;

    const setRangeStatus = (text) => setText("[data-testid=range-status]", text);
    const setRangeState = (value) => {
      const node = q("[data-testid=range-status]");
      if (node) node.dataset.state = value;
    };

    if (applyButton) {
      applyButton.addEventListener("click", () => {
        const startValue = startInput ? startInput.value : "";
        const endValue = endInput ? endInput.value : "";
        if (!startValue || !endValue) {
          setRangeStatus("请选择开始与结束时间");
          return;
        }
        const startMs = new Date(startValue).getTime();
        const endMs = new Date(endValue).getTime();
        if (!Number.isInteger(startMs) || !Number.isInteger(endMs)) {
          setRangeStatus("时间格式无效");
          return;
        }
        if (startMs >= endMs) {
          setRangeStatus("结束时间必须晚于开始时间");
          return;
        }
        // 必须在发起请求**之前**置位：绘制发生在 loadSnapshot 内部，
        // 晚一步设置会让窗口被默认的「最近 N 根」截断（2026-09-23 实测）。
        const previousRange = state.pinnedRange;
        state.pinnedRange = { startMs, endMs };
        Promise.resolve(refreshSelectedSnapshot({ startMs, endMs })).then((snapshot) => {
          if (!snapshot) {
            state.pinnedRange = previousRange;
            setRangeStatus("区间快照加载失败，区间未固定");
            return;
          }
          setRangeStatus(`历史区间 ${new Date(startMs).toLocaleString()} ~ ${new Date(endMs).toLocaleString()}`);
          setRangeState("pinned");
          stopPolling();
          scheduleDraw();
        });
      });
    }

    if (applyButton) {
      root.querySelectorAll("[data-range-quick]").forEach((button) => {
        button.addEventListener("click", () => {
          const days = Number(String(button.dataset.rangeQuick || "").replace(/d$/, ""));
          if (!Number.isInteger(days) || days <= 0) return;
          const endMs = Math.floor(Date.now() / 60000) * 60000;
          const startMs = endMs - days * 24 * 3600 * 1000;
          if (startInput) startInput.value = toLocalInputValue(startMs);
          if (endInput) endInput.value = toLocalInputValue(endMs);
          applyButton.click();
        });
      });
    }

    if (liveButton) {
      liveButton.addEventListener("click", () => {
        const wasPinned = state.pinnedRange !== null;
        state.pinnedRange = null;
        if (startInput) startInput.value = "";
        if (endInput) endInput.value = "";
        setRangeStatus("实时");
        setRangeState("live");
        // 立即拉一次实时快照，否则画面会停留在历史区间直到下一次轮询（最长 30 秒）。
        if (wasPinned) {
          Promise.resolve(refreshSelectedSnapshot()).then((snapshot) => {
            if (snapshot) scheduleDraw();
          });
        }
        // 走 globalThis：dash-*.js 各自独立 IIFE，裸标识符跨文件不可达
        // （2026-10-06 实测此处一直是死代码，点「实时」也没启动轮询）。
        // 取不到就抛 —— 静默跳过只会让页面停在旧数据上却看不出原因。
        if (state.snapshotUrl) startPolling(state.snapshotUrl);
      });
    }
  }


  function renderReplayControls() {
    // 进度分母取权威全量快照（回放时 state.snapshot 已被切成前缀）
    const full = state.fullSnapshot || state.snapshot;
    const count = full ? asArray(full.candles).length : 0;
    const index = state.replayIndex == null ? 0 : state.replayIndex;
    setText("[data-testid=replay-progress]", `${index} / ${count}`);
    const runtime = full && isObject(full.runtime) ? full.runtime : {};
    setText("[data-testid=replay-window-size]", runtime.window_size == null ? count : runtime.window_size);
    setState(setText("[data-testid=replay-truncated]", runtime.truncated === true ? "true" : "false"), runtime.truncated === true ? "true" : "false");
    // D4 回放状态机已接通：play / pause / step / reset / seek 全部可用。
    // - 播放中：只留 pause 可点（step/reset/seek 跳帧会乱）
    // - 非播放中：全部可点；play 在末尾仍可用，点击自动从第 1 根重播
    const isPlaying = state.replayTimer !== null;
    root.querySelectorAll("[data-replay-action]").forEach((button) => {
      const action = button.dataset.replayAction;
      const disabled = isPlaying ? action !== "pause" : count === 0;
      button.disabled = disabled;
      button.setAttribute("aria-disabled", disabled ? "true" : "false");
      if (disabled) {
        button.setAttribute("title", isPlaying ? "回放进行中，先「暂停」" : "等待 snapshot 加载");
      } else {
        const atEnd = index >= count && count > 0;
        const titles = {
          play: atEnd ? "从头重播" : "开始回放",
          pause: "暂停回放",
          step: "前进 1 根 K 线",
          reset: "回到第 1 根 K 线",
          seek: "跳到末根 K 线",
        };
        button.setAttribute("title", titles[action] || "");
      }
    });
  }

// >>>FUNCS
})();
