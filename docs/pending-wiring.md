# 待接线模块清单（pending-wiring）

> 建立：2026-09-25（代码审核 P0-2 处置）
> 重写：2026-09-30（R19——按**功能簇 + 处境**重排，替换原「平铺 16 行」表）
> 来源：`docs/audit/cpt-code-audit-20260925.md` §3.4、`docs/audit/verification-20260925.md` §4
> 维护：新增待接线模块必须登记在此；接线完成或决定删除时从此表移除，并在提交信息里说明。

## 为什么有这份清单

2026-09-25 的独立核实发现仓库里有约 2,353 行「生产代码零导入」的模块。核实结论是
它们**不是同一类东西**，不能一刀切删除，故按「是否有明确的产品位置」分三类处置：

| 处置 | 判据 |
|---|---|
| 删除 | 既无生产导入方，也**不在任何现行计划文档**里 |
| **待接线（本清单）** | 无生产导入方，但**产品路线图或现行台账里有明确位置** |
| 保留 | 生产路径可达（活代码） |

判据文档（按效力排序）：

1. ~~`/home/ubuntu/work/cpt-audit/CPT-总计划-2026-09-24.md`——现行**唯一任务台账**（仓库外）~~
   → **已失效（2026-09-30）**：该文件与整个 `cpt-audit/` 目录均已不存在。
   台账职责收归 `docs/progress-log.md` 的「2. 轮次记录」。
   本清单的去留判定**改以 `docs/progress-log.md` + 下条 roadmap 为准**。
2. `docs/dashboard-product-roadmap.md`——`docs/dashboard-plan.md:151` 明确「**后续产品路线
   以本文件 Phase 0-6 为准**」
3. `docs/implementation-plan.md`——头部已声明自己是**历史记录**，**不作为**判据

> ⚠️ **判据踩坑记录**：本次执行第一版只按「模块名是否出现在台账里」判定，把 12 个
> `dashboard_*` 当无规划删掉了。装 P0-3 门禁时复查才发现 roadmap 的 Phase 3–6 用
> **功能描述**（不是模块名）覆盖了它们，遂全部回滚。教训：判据文档必须按**功能**而
> 非**文件名**比对。

> ⚠️ **复核踩坑记录（R19，2026-09-30）**：上一轮把 5 个模块判为「已有活替代实现」，
> 依据是**函数名概念相近**。逐行读函数体后只有 1 个成立（`dashboard_market_fetch`），
> 另 4 个的比对对象根本不同语义（配置 diff ≠ 快照 diff；计数投影 ≠ 结构树；
> 质量口径互补；轮询线程 ≠ 纯函数）。教训：**判「重复实现」必须比对函数体，
> 不能比对名字**——这与本仓 2026-09-30 报告里「只查关键词不读函数体」是同一个错。

## 先看结论：16 个模块是 3 个功能簇，不是 16 件散货

| 簇 | 模块 | 行数 | 性质 |
|---|---:|---:|---|
| **一、A 股信号链** | 2 | 463 | **唯一影响「信号对不对」**（R19 已接 1 个） |
| **二、研究者模式面板** | 10 | 396 | 只读展示/分析 |
| **三、盯盘模式面板** | 4 | 122 | 只读展示 |
| 合计 | 16 | **981** | |

**优先级判据**：簇一改错 = 信号本身是错的；簇二/三改错 = 面板难看。故顺序为
**簇一 → 簇二 → 簇三**。簇一里的两个模块是**真正有待建价值**的部分；簇二/三里
有相当一部分要先做「要不要这个功能」的决策，而不是埋头接线。

---

## 三种处境（不是所有「待接线」都该接）

同一份清单里的模块处境不同，处置动作也不同：

| 处境 | 含义 | 该做什么 |
|---|---|---|
| **半接线** | 路由已活、前端已在读，但数据源恒为占位 | 补数据源（改 `dashboard_snapshot_v2.py`，不是改路由） |
| **真重复** | 活路径已有等价实现，本模块是其空壳包装 | 二选一：删本模块，或删活路径那份 |
| **依赖消失** | 它依赖的上游实现已被删除，永远不可能有数据 | 先拍功能去留，**不能直接接** |
| **纯未接线** | 无活替代、无已删依赖，就是没接 | 写从生产入口可达的集成测试后接线 |

---

## 簇一：A 股信号链（463 行）——**唯一影响信号正确性**

| 模块 | 行数 | 处境 | 功能 | 接线目标 |
|---|---:|---|---|---|
| `cpt/domain/signal.py` | 311 | 纯未接线 | **一买状态机**：`assess_first_buy` / `transition_first_buy`，管 alert→confirmed→invalidated 转移 | 接进 A 股信号链；`signal_id` 已按**稳定 upsert 主键**设计 |
| `cpt/domain/a_share_rules.py` | 152 | ✅ **R19 已接线** | **A 股交易规则标签**：涨跌停 / 停牌 / T+1。`fetch_daily_tags` / `apply_ashare_tags_to_bis` / `t_plus_one_purchase_allowed` | 标签能力已进 A 股主看板（见下）；**只剩 T+1 未接** |

### `a_share_rules` 的 R19 接线结果

数据流：`AShareLocalClient.fetch_daily_tags`
（`cpt/adapters/a_share_local.py`）→ `cpt/application/a_share_snapshot.py`
的 `_apply_daily_tags` → 合成 id 挂到 `Bi.source_ids`（如
`ashare:is_limit_up:2026-09-21`）+ 审计块写进 `data_quality.ashare_tags`。

**审计块刻意区分三种「没标签」**，否则面板上「没画虚线」分不清是今天真没有涨停
还是功能没接上：

| 情形 | `available` | `reason` |
|---|---|---|
| 注入的客户端没实现 `fetch_daily_tags`（测试替身） | `false` | `client_unsupported` |
| 查了但失败（DB 挂） | `false` | `tag_fetch_failed` |
| 查通了，区间内确实没有极端日 | `true` | ——（`tagged_bis: 0`） |

三条关键设计约束，都有守门用例：

1. **标签是纯展示增强，DB 挂了不能搞挂快照** —— 降级只影响标签，不影响出图。
2. **只注入 `source_ids`，不改 `Bi` 数值** —— 判据与 M4 fixture 迁移同源：
   接线类改动必须先证明「结构不变」，否则标签写进数值字段会让缠论结构静默变形。
3. **标签查询区间 = K 线查询区间** —— 区间错位会让笔的末日查不到标签，且**静默失效**
   （`end_ms`/`start_ms` 因此从 `try` 块里提到块外共用）。

前端 `dashboard/dashboard.js` 已在笔上输出 `data-source-ids`（`:1492`）并在详情面板
显示（`:619`），所以标签接上即自动可见；**但前端尚无 `ashare:` 前缀的专门渲染**
（虚线/降透明）—— 属下一增量。

### 剩下的唯一一块：`signal.py` 的一买状态机

`a_share_rules` 的 docstring 写明「C4 T+1 … 由 `cpt.domain.signal` 在评估一买/一卖
时读取」，即两者互锁。`signal.py` 没能一起接，根因是**缺输入的生产者**：
`assess_first_buy` 需要 `has_two_centers` / `has_divergence_leg` / `has_reversal_bi`，
而这三个值只出现在 `signal.py` 自己和 `cpt/domain/first_buy.py:12` 的 docstring 里；
`TrendType`（`cpt/domain/models.py:163`）没有中心数/背驰笔/反转笔字段。**这座桥
必须新写，不是接线**，故挂起待拍板。

---

## 簇二：研究者模式面板（396 行，roadmap Phase 1/2/5/6）

| 模块 | 行数 | 处境 | 功能 / roadmap 位置 | 前端消费 |
|---|---:|---|---|---:|
| `cpt/application/dashboard_parity.py` | 83 | **依赖消失** | Phase 2「Oracle 对比」 | 32 处 |
| `dashboard_signal_history.py` | 47 | 纯未接线 | Phase 4 P1 信号历史 + Phase 6 P2 转化率/失效原因分布 | 0 |
| `dashboard_event_audit.py` | 42 | 纯未接线 | Phase 5 P1 事件前后状态对比 | 0 |
| `dashboard_levels.py` | 40 | 纯未接线 | Phase 5 P1 级别递归树 | 0 |
| `dashboard_runs.py` | 37 | **半接线** | R1 数据集/运行浏览器 | 2 处 |
| `dashboard_quality.py` | 33 | 纯未接线 | Phase 5 P1 数据质量报告 | 0 |
| `dashboard_compare.py` | 31 | 纯未接线 | R5 两份 snapshot 字段级 diff + Phase 6 P2 | 0 |
| `dashboard_stats.py` | 30 | 纯未接线 | Phase 6 P2 一买统计 | 0 |
| `dashboard_multi_run.py` | 28 | 纯未接线 | Phase 6 P2 双数据集同步对比 | 0 |
| `dashboard_export.py` | 25 | 纯未接线 | Phase 6 P2 时间范围切片导出 | 0 |

### 簇二的两条特殊条目

**① `dashboard_parity` —— 依赖已消失，接不了。**

oracle 参照实现 **R13 已整体删除**，`dashboard_snapshot_v2.py:61` 的
`reason: "oracle_reference_unavailable"` 是**永久 false**，不是暂时缺数据。
`build_parity_view` 在没有参照物时是无米之炊。

> 待拍板：**还要不要 oracle 对比这个功能**？
> - 要 → 先恢复一份 oracle 参照实现，再接线（成本大）
> - 不要 → 删 `dashboard_parity.py` + 摘掉前端 `parity` 面板（`dashboard.js:975
>   renderParityCharts` 等 6 处消费点）+ 移除 `app.py:208` 路由

**② `dashboard_runs` —— 半接线：路由活、前端读、数据恒空。**

`cpt/web/app.py:210` 路由活着，`dashboard.js:827` 真的在读 `snapshot.runs`，
但 `dashboard_snapshot_v2.py:62` 硬编码 `v2["runs"] = []`。**通的是一条死管道。**
补数据源即可（有 `build_run_index` 可用）。

---

## 簇三：盯盘模式面板（122 行，Phase 3/4）

| 模块 | 行数 | 处境 | 功能 / roadmap 位置 | 前端消费 |
|---|---:|---|---|---:|
| `cpt/application/dashboard_realtime.py` | 40 | 纯未接线 | Phase 4 P1 SSE/高效实时更新 | 0 |
| `dashboard_watch.py` | 29 | 纯未接线 | Phase 4 P1 reconnect/stale 指标 | 0 |
| `dashboard_watchlist.py` | 28 | 纯未接线 | Phase 4 P1 多交易对 | 0 |
| `dashboard_market_fetch.py` | 25 | **真重复** | Phase 3 P0 真实 24h 高低点/成交量 | 0 |

### 簇三里唯一确认的重复：`dashboard_market_fetch`

内层逻辑 `normalize_24h`（`cpt/application/dashboard_market.py:42`）**已经被活路径
直接调用**——`cpt/web/__main__.py:790` 的 `_safe_24h_for`。本模块的
`market_snapshot` 只是在它外面包了 `symbol` / `interval_ms` / `source` 三个字段，
且**自己 0 个导入方**。

> 处置二选一：
> - **删** `dashboard_market_fetch.py`（25 行，包装层无独立价值）← 倾向此项
> - 或把 `_safe_24h_for` 改成调 `market_snapshot`（多一层，收益不明）

---

## 占位集中地（半接线的统一病灶）

`cpt/application/dashboard_snapshot_v2.py:56-70` 是**五个占位兜底的唯一出处**：

| 行 | 字段 | 占位值 |
|---:|---|---|
| 56 | `market_24h` | `{"available": False, "reason": "upstream_aggregate_unavailable"}` |
| 61 | `parity` | `{"available": False, "reason": "oracle_reference_unavailable"}` |
| 62 | `runs` | `[]`（硬编码，无 `available` 包装） |
| 63 | `multi_level` | `{"available": False, "reason": "multi_level_unavailable"}` |
| 67 | `config_compare` | `{"available": False, "reason": "config_compare_unavailable"}` |

**接线的正确改法是给这些键传入真值**——五个都已是 `build_dashboard_snapshot_v2`
的**关键字形参**（`:32-35`），调用方传进去即可，不必改函数体。

> 但注意：`multi_level` / `config_compare` 的**活路径并不经这里**——
> `cpt/web/__main__.py:174-175`（`_format_multi_level` / `_compare_with_default`）
> 直接构造，不调 v2。所以这两个键的占位只在「谁调 v2」时才有意义。

---

## 另有 3 个函数（非独立模块）同样待接线

| 函数 | 产品位置 | 处境 | 说明 |
|---|---|---|---|
| `dashboard_reproducibility.py::snapshot_diff` | roadmap R5 | 纯未接线 | 唯一调用方是 `dashboard_compare.py:30`——**两个待接线模块互调**，都不在生产链路上 |
| `adapters/a_share_local.py::_to_wind_code` | 台账决策 C1 | 纯未接线 | 仅被**注释**提及（`a_share_public.py:115`、`scripts/factor_backfill.py:130`），无代码调用 |
| `adapters/wind_source.py::fetch_adjust_factors` | 台账决策 C1 | 纯未接线 | 全仓 0 引用 |

---

## 约束

1. **改动本清单内模块前先读本文**——它们没有生产调用方，改错了不会有测试变红。
2. 这些模块的现有测试是**自证式**的（测一段不运行的代码），**不能**当作「已在生产验证」。
   R19 的接线守门用例补了一条新规矩：**入口必须是生产构造函数**
   （`build_ashare_snapshot`），只改模块函数体而忘接线的改动**不会**被原有用例发现。
3. CI 的 vulture 门禁（`.github/workflows/ci.yml`，阈值已从 80 降到 60）对这批名字走
   `whitelist.py` 白名单。白名单是「已知未接线」的登记，不是「忽略告警」。
4. 接线时**先写从生产入口可达的集成测试**，再把模块从本清单和 `whitelist.py` 里移除。
5. **删模块时三处同步**：本文件 + `whitelist.py` + `tests/test_<module>.py`。
6. **白名单是逐个符号的，不是逐个模块的**：一个模块接了一半，剩下的符号仍要留豁免
   （例：`a_share_rules` 的标签函数已摘，`t_plus_one_purchase_allowed` 仍留）。

## 相关

- `docs/audit/cpt-code-audit-20260925.md` §3.4（孤儿层与死模块）、§5.1（dbconfig 三胞胎）
- `docs/audit/verification-20260925.md` §4（P0 清单）
- `docs/architecture.md` §2（`engine/` 与 `storage/` 两层的删除说明）
- `docs/dashboard-product-roadmap.md`（Phase 0-6 功能描述，判据 2）
