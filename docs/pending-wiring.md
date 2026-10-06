# 待接线模块清单（pending-wiring）

> 建立：2026-09-25（代码审核 P0-2 处置）
> 重写：2026-09-30（R19——按**功能簇 + 处境**重排，替换原「平铺 16 行」表）
> 收尾：2026-09-30（R22——簇二/簇三 **9 个模块 + 3 个函数全部接线**，本清单只剩 T+1 一项）
> 来源：`docs/audit/cpt-code-audit-20260925.md` §3.4、`docs/audit/verification-20260925.md` §4
> 维护：新增待接线模块必须登记在此；接线完成或决定删除时从此表移除，并在提交信息里说明。
> **R54 复核（2026-10-05）**：全篇 32 处 `file.py:NNN` 引用逐条实测，**17 处已失效**
> （`cpt/web/app.py` 整体右移约 280 行，R22 接线表十处全错）。均已用
> `grep -n 'def <符号>'` / `grep -n 'path.path == "<路由>"'` 重定位，改动处就地标注。
> **结论未变**：九个模块与「另有 3 个函数」仍全部接线，本期只改坐标。
> ⚠️ **行号还会继续漂** —— 引用本文档时以**符号名 / 路由路径**为准，行号只当快速定位。
> 顺带发现 `_format_multi_level` 已被删除（原文并列引用它），已在原处更正。

## R22 收尾：本清单现状（2026-09-30，R41 复核仍成立）

**唯一仍在册的待接线项**：`cpt/domain/a_share_rules.py::t_plus_one_purchase_allowed`
（T+1 读取点。标签能力 R19 已接，R22 未动，仍留 `whitelist.py` §4b 豁免）。

> **R41 复核（2026-10-02）**：R31~R41 又接进了一批（飞书告警通道、运行水位表、
> 每日巡检、结构事件归因、golden set、因子重算与对账报告），**没有新增待接线项**，
> 本条仍然成立。复核方式：全仓 grep `t_plus_one_purchase_allowed` —— 只在
> `a_share_rules.py` 自己的 `__all__`（第 49 行）与定义（第 112 行）出现，
> 确认**没有任何生产调用方**。
>
> ⚠️ 复核方法上的坑：只查 `from x import y` 会漏掉 `module.func()` 形式的属性
> 访问。按导入可达性统计会得到一批**假阳性**（实测 23 个，其中绝大多数是通过
> 属性访问在用的）。要判「某个具体符号是否接线」，**直接 grep 全仓比导入图可靠**。

簇一/二/三与「另有 3 个函数」的其余条目**全部已接线**，处置明细见下表。
R22 的 9 个接线点（路由与调用方均已 grep 复核）：

| D 类模块 | R22 接线点（**R54 重定位**） |
|---|---|
| `dashboard_export.py` | `cpt/web/app.py:798`（C1 `GET /export?start_ms=&end_ms=`） |
| `dashboard_levels.py` | `cpt/application/dashboard_snapshot_v2.py:32`（`def _level_tree_payload`）+ `cpt/web/app.py:840`（C2 `/levels`） |
| `dashboard_compare.py` | `cpt/web/app.py:862`（C3 `/compare?left=&right=`，序列值降级为 `{__summary__,count,hash}`） |
| `dashboard_multi_run.py` | `cpt/web/app.py:909`（C4 `/multi-run?run_ids=`） |
| `dashboard_stats.py` | `cpt/web/app.py:1035`（C5 `/signal-stats?days=&code=`，自报 `basis`） |
| `dashboard_watchlist.py` | `cpt/web/app.py:1046`（C6 `/watchlist`，投影自 A 股候选池） |
| `dashboard_quality.py` | `cpt/application/dashboard.py:120`（v1 `def _data_quality` 重写 ⇒ v1/v2 两条活路径都过） |
| `dashboard_watch.py` | `cpt/application/dashboard_snapshot_v2.py:21`（`def _watch_payload` → `v2["watch_metrics"]`） |
| `dashboard_realtime.py` | `cpt/web/__main__.py:594`（`_RealtimeProvider._cached_snapshot`，容量 8 的 LRU） |

`dashboard_runs.py` 顺带补齐**运行本体**（此前只存索引行）：
`cpt/application/dashboard_runs.py:188 def run_body()` / `:203 def find_run()`，
被 `cpt/web/app.py:679` 的 `/api/dashboard/runs` 分支调用。

> **R54 重定位说明**：上表九行坐标原为 `app.py` 的 `513`/`541`/`562`/`602`/`160`/`234`、
> `dashboard_snapshot_v2.py` 的 `48`/`29`、`dashboard.py:140`、`__main__.py:572`，
> **十处全部过期**（`cpt/web/app.py` 整体右移约 280 行）。判据：新坐标均已
> `grep -n 'def <符号>' / 'path.path == "<路由>"'` 实测。**结论未变** ——
> 九个模块仍全部接在活路径上，只是坐标漂了。


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
2. `docs/archive/plans-and-acceptance.md` §1.1（参照仓库与产品路线）——原
   `dashboard-product-roadmap.md` / `dashboard-plan.md`，R56 收敛时合并至此。
3. 该合并件 §3「已被取代的结论」——原 `implementation-plan.md` 已明确声明自己是
   历史记录，**不作为**判据

> ⚠️ **判据踩坑记录**：本次执行第一版只按「模块名是否出现在台账里」判定，把 12 个
> `dashboard_*` 当无规划删掉了。装 P0-3 门禁时复查才发现 roadmap 的 Phase 3–6 用
> **功能描述**（不是模块名）覆盖了它们，遂全部回滚。教训：判据文档必须按**功能**而
> 非**文件名**比对。

> ⚠️ **复核踩坑记录（R19，2026-09-30）**：上一轮把 5 个模块判为「已有活替代实现」，
> 依据是**函数名概念相近**。逐行读函数体后只有 1 个成立（`dashboard_market_fetch`，R20 已删），
> 另 4 个的比对对象根本不同语义（配置 diff ≠ 快照 diff；计数投影 ≠ 结构树；
> 质量口径互补；轮询线程 ≠ 纯函数）。教训：**判「重复实现」必须比对函数体，
> 不能比对名字**——这与本仓 2026-09-30 报告里「只查关键词不读函数体」是同一个错。

## 先看结论：16 个模块是 3 个功能簇，不是 16 件散货

| 簇 | 模块 | 行数 | 性质 | R22 后 |
|---|---:|---:|---|---|
| **一、A 股信号链** | 2 | 463 | **唯一影响「信号对不对」**（R19 已接 1 个，R20 已接 1 个） | 只剩 `t_plus_one_purchase_allowed` 一个符号 |
| **二、研究者模式面板** | 10 | 396 | 只读展示/分析 | 7 个全部接线（另 3 个 R20/R21 已删） |
| **三、盯盘模式面板** | 4 | 122 | 只读展示 | 3 个全部接线（另 1 个 R20 已删） |
| 合计 | 16 | **981** | | **15 个模块已接线/已删，1 个符号待接（T+1）** |

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
| ~~`cpt/domain/signal.py`~~ | ~~311~~ | **已接线（R20）** | **一买状态机**：`assess_first_buy` / `transition_first_buy`，管 alert→confirmed→invalidated 转移 | 已进 A 股信号链（`a_share_snapshot.py:316`） |
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

### `signal.py` 的一买状态机 —— **已接线（R20）**

`a_share_rules` 的 docstring 写明「C4 T+1 … 由 `cpt.domain.signal` 在评估一买/一卖
时读取」，即两者互锁。R20 前它接不上的根因不是「缺输入的生产者」，而是**缺翻译层**：
数据其实齐了，只是没人把结构对象翻译成状态机的入参。
`assess_first_buy` 需要 `has_two_centers` / `has_divergence_leg` / `has_reversal_bi`，
这三个值只出现在 `signal.py` 自己、`cpt/domain/first_buy.py:12` 的 docstring 和本文件里；
`TrendType`（`cpt/domain/models.py:193`）确实没有中心数/背驰笔/反转笔字段。

> **R20 处置：新增 `cpt/application/first_buy_bridge.py`（翻译层），接进 A 股主看板。**
> - 桥输出 `FirstBuyFacts`（三事实 + `center_ids` + `structure_id` + `divergence_status`），
>   再由 `a_share_snapshot.py:456 _derive_first_buy_signal` 调 `assess_first_buy` 出
>   `Signal`，经 `build_dashboard_snapshot_v2(..., signal=signal)` 落到
>   `v2["signal"]`。接线点干净：v2 早有该形参，只是没人传。
> - **保守口径四条**（全选「宁可判否」一侧）：①取本级别**最后两个**中枢，不是任意两个；
>   ②背驰段 = 中枢二 `end_time` 之后、方向相同的**第一笔**（不含中枢连接笔）；
>   ③反向笔**出现即算**，收盘确认交给 `transition_first_buy` 的 `reversal_closed`；
>   ④**背驰不是硬门槛**（沿用 `_structure_ready` 现状）。
> - **趋势方向从数据推导**：取本级别最后一笔方向，**不硬编码 `-1`**。最后一笔向上
>   时一买无意义 → 桥返回 `None` → 不产信号。若写死 `-1`，信号栏将永远非空，
>   失去「当下是否处于一买结构」的信息量。
> - **两套口径并存**：`divergence_status` 用 `first_buy.check_first_buy`
>   （czsc 笔段力度背驰），三事实用缠论 §8.2 结构条件。力度未填充时前者
>   **响亮报错**（`first_buy.py:72`），桥捕获并降级为 `not_checked` ——
>   **不与 `not_detected` 混用**（前者是「数据缺」，后者是「算过了，不背驰」）。
> - **守门用 spy 而非断言 `signal is not None`**：断言成败取决于测试数据能否造出
>   两个中枢（实测 60 组参数都造不出，`_extend` 会一路吞并），会「因为数据没结构」
>   变红。`test_production_entry_calls_bridge` 用 monkeypatch 盯住「生产入口有没有
>   调这个函数」，未接线时实测红（`assert []`），且不挑数据。
> - **仍未接**：`transition_first_buy` 的状态推进（需持久化信号历史）、
>   `alert` 状态（需盘中反向 K 线）、一卖 `check_first_sell`、T+1 读取点。

---

## 簇二：研究者模式面板（396 行，roadmap Phase 1/2/5/6）

| 模块 | 行数 | 处境 | 功能 / roadmap 位置 | 前端消费 |
|---|---:|---|---|---:|
| `cpt/application/dashboard_parity.py` | 222 | **已复活并重写（R35b）** ⚠️ R45 更正 | Phase 2「Oracle 对比」—— 原判「oracle 参照已删，永久未启用」**这个前提已经不成立**：参照侧改成 **czsc 优先 / 回落腾讯**（`parity_reference.py:61` 惰性 import `build_parity_snapshot`），本模块从 83 行重写成 246 行 | 28 处（空面板保留） |
| ~~`dashboard_signal_history.py`~~ | ~~47~~ | **已删除（R21）** | Phase 4/6 — 依赖 `Signal` 域对象，生产线不产出，留了也是假希望 | — |
| ~~`dashboard_event_audit.py`~~ | ~~42~~ | **已删除（R21）** | Phase 5 — 依赖 `StructureEvent` 域对象，生产线不产出，留了也是假希望 | — |
| ~~`dashboard_levels.py`~~ | ~~40~~ | **已接线（R22）** | Phase 5 P1 级别递归树 | v2 `level_tree` + C2 `/levels` |
| ~~`dashboard_runs.py`~~ | ~~37~~ | **已接线（R20/R22）** | R1 数据集/运行浏览器 | 2 处 |
| ~~`dashboard_quality.py`~~ | ~~33~~ | **已接线（R22）** | Phase 5 P1 数据质量报告 | v1/v2 两条路径 |
| ~~`dashboard_compare.py`~~ | ~~31~~ | **已接线（R22）** | R5 两份 snapshot 字段级 diff + Phase 6 P2 | C3 `/compare` |
| ~~`dashboard_stats.py`~~ | ~~30~~ | **已接线（R22）** | Phase 6 P2 一买统计 | C5 `/signal-stats` |
| ~~`dashboard_multi_run.py`~~ | ~~28~~ | **已接线（R22）** | Phase 6 P2 双数据集同步对比 | C4 `/multi-run` |
| ~~`dashboard_export.py`~~ | ~~25~~ | **已接线（R22）** | Phase 6 P2 时间范围切片导出 | C1 `/export` |

### 簇二的两条特殊条目

**① `dashboard_parity` —— 依赖已消失，R20 已处置。**

oracle 参照实现 **R13 已整体删除**，`dashboard_snapshot_v2.py:98` 的
`reason: "oracle_reference_unavailable"` 是**永久 false**，不是暂时缺数据。

> **R20 处置：删后端模块，保留前端空面板。**
> - 已删 `cpt/application/dashboard_parity.py`（83 行）、摘掉
>   `whitelist.py` 的 `build_parity_snapshot` 豁免、移除
>   `tests/test_dashboard_research_services.py` 与 `tests/test_review_m7_fixes.py`
>   里的两处自证用例。
> - **保留** `dashboard_snapshot_v2.py:98` 的 `{"available": False, "reason":
>   "oracle_reference_unavailable"}` 键位、`app.py:677` 路由与前端
>   `renderParityCharts`：前端 28 处消费点依赖这个形状，摘面板要改 6 个渲染函数
>   + 契约测试，收益仅为少显示一个恒空的 `<section>`。**代价大于收益。**
> - 结论：Oracle 对比功能**永久未启用**。若将来要恢复，正确顺序是
>   先恢复一份 oracle 参照实现，再谈接线；不要在没有参照物时保留投影函数。
> - 前端契约测试 `tests/test_dashboard_parity_navigation.py` **保留**（它守的是
>   仍然活着的空面板交互，不是被删的投影函数）。

**② `dashboard_runs` —— 已接线（R20 路由 + R22 本体 + R23 落库）。**

`cpt/web/app.py:679` 路由活着，`dashboard/dash-ops.js:388` 与 `dashboard/dash-signal.js:439`
真的在读 `snapshot.runs`，R20 补了索引行数据源，但 `dashboard_snapshot_v2.py:99` 的硬编码
`v2["runs"] = []` 让「通的是一条死管道」。**R22 补上运行本体**：`dashboard_runs.py` 新增
`_RUN_BODIES: deque[dict | None]`（与 `_RUN_RING` 严格同步 append/clear，否则错位）、
`RUN_BODY_MAX_BYTES = 4_000_000`（超限**仍收索引行、只是不存本体**）、
`run_body(run_id)`(:188，返回深拷) 与 `find_run(run_id)`(:203)。C3/C4 由此才有入参。

> **R54 行号更正**：本段六处引用原为 `app.py:210` / `dashboard.js:827` /
> `dashboard_snapshot_v2.py:62` / `dashboard_runs.py` 的 `:158`、`:173`，**四处全过期**
> （`:210` 指着一条注释；`dashboard.js` 已随 R51 删除）。结论未变，只改坐标。
> `dashboard.js` 的前端读取职责由 `dash-ops.js` / `dash-signal.js` 承接。

**R23 把「只对本进程活过的 run 可比」这个遗留口径消掉了**：新增
`cpt/application/dashboard_run_store.py`
> ⚠️ **R45 更正**：该文件后来随 storage 层恢复**搬到了 `cpt/storage/dashboard_run_store.py`** ——
> 本文记录的是 R23 当时的位置，跟着路径找会扑空。
+ 表 `public.cpt_dashboard_run`（5 列：
`run_id` PK / `dataset_hash` / `generated_at` / `body_recorded` / `snapshot` jsonb），
`record_run(..., on_recorded=...)` 在**真正 append 之后**同步双写（best-effort，
写失败不反噬 HTTP），`/compare`、`/multi-run`、`/runs` 三条路由改成
**表优先 → 进程内 deque 兜底**。

> R20 当初的结论是「不建表」——理由是「落库即 2,880 行/天的低价值流水」。
> **这个推理有个错误**：realtime 30s 一轮里十几次 HTTP 请求命中的是**同一份
> 缓存 snapshot**，`record_run` 的去重会让它们**全部走快路径、连 DB 都不碰**，
> 实际只落 1 行/轮（≈2,880 行/天，且每行是独立的一次运行，删了就没了）。
> 重复请求的放大问题从来就不存在，所以「不建表」的理由不成立。
> 真正让这个决策被推翻的是用户诉求：**跨重启可比**（2026-09-30）。
>
> 表是 append-only 且不自动 GC，运维按需
> `DELETE WHERE generated_at < now() - interval '7 days'`。
> 迁移与部署见 `deploy/README.md`「数据库迁移」小节。

---

## 簇三：盯盘模式面板（122 行，Phase 3/4）

| 模块 | 行数 | 处境 | 功能 / roadmap 位置 | 前端消费 |
|---|---:|---|---|---:|
| ~~`cpt/application/dashboard_realtime.py`~~ | ~~40~~ | **已接线（R22）** | Phase 4 P1 SSE/高效实时更新 | `_cached_snapshot` LRU |
| ~~`dashboard_watch.py`~~ | ~~29~~ | **已接线（R22）** | Phase 4 P1 reconnect/stale 指标 | v2 `watch_metrics` |
| ~~`dashboard_watchlist.py`~~ | ~~28~~ | **已接线（R22）** | Phase 4 P1 多交易对 | C6 `/watchlist` |

### 簇三里唯一确认的重复：`dashboard_market_fetch` —— **已删除（R20）**

内层逻辑 `normalize_24h`（`cpt/application/dashboard_market.py:42`）**已经被活路径
直接调用**——`cpt/web/__main__.py:920`（`def _safe_24h_for`；调用点 `:831`/`:918`）。本模块的
`market_snapshot` 只是在它外面包了 `symbol` / `interval_ms` / `source` 三个字段，
且自己 0 个导入方。

> **处置：已删除**（R20）。删 `cpt/application/dashboard_market_fetch.py`（25 行）
> 与 `tests/test_dashboard_market_fetch.py`（14 行），并摘掉 `whitelist.py` 的
> `market_snapshot` 豁免。Phase 3 P0「真实 24h 高低点/成交量」的需求**不受影响**——
> 活路径本就直接调 `normalize_24h`，包装层删掉后行为完全一致。

---

## 占位集中地（半接线的统一病灶）

`cpt/application/dashboard_snapshot_v2.py:93-111` 是**五个占位兜底的唯一出处**：

| 行 | 字段 | 占位值 |
|---:|---|---|
| 94 | `market_24h` | `{"available": False, "reason": "upstream_aggregate_unavailable"}` |
| 98 | `parity` | `{"available": False, "reason": "oracle_reference_unavailable"}` |
| ~~62~~→`99` | `runs` | v2 体内的 `[]` **仍在**，但活路径不经它：`cpt/web/app.py:253 _with_run_index()` 在**HTTP 响应层**注入真值（R20），`:1054`/`:1114` 调用 |
| 101 | `multi_level` | `{"available": False, "reason": "multi_level_unavailable"}` |
| 109 | `config_compare` | `{"available": False, "reason": "config_compare_unavailable"}` |

**接线的正确改法是给这些键传入真值**——五个都已是 `build_dashboard_snapshot_v2`
的**关键字形参**（`:69-72`），调用方传进去即可，不必改函数体。

> 但注意：`multi_level` / `config_compare` 的**活路径并不经这里**——
> `cpt/web/__main__.py` 的 `_compare_with_default`（`:457 def`，调用点 `:225`/`:355`/`:402`）
> 直接构造，不调 v2。所以这两个键的占位只在「谁调 v2」时才有意义。
>
> **R54 更正**：原文此处还并列了一个 `_format_multi_level`（记作 `:174-175`），
> 该函数**现今全仓已不存在**（`grep -rn _format_multi_level --include=*.py cpt/` 零命中），
> 已从本句删去；`_compare_with_default` 本身仍在，只是坐标从 174 漂到 457。

---

## 另有 3 个函数（非独立模块）——**R22 全部接线**

| 函数 | 产品位置 | 处境 | R22 处置 |
|---|---|---|---|
| `dashboard_reproducibility.py::snapshot_diff` | roadmap R5 | **已接线（R22）** | 调用方 `dashboard_compare.py` 已接进 C3 `/compare`（`cpt/web/app.py:862`）；序列型值由 `cpt/web/app.py:287 _summarize_diff_value` 递归降级，避免原始 candles 灌进响应 |
| `adapters/a_share_local.py::_to_wind_code` | 台账决策 C1 | **已接线（R22）** | 调用方 `scripts/factor_backfill.py:199`（`def fetch_wind_factor_rows`）→ 循环体 `:594` 的 `_wind_fallback`，由 `--wind-fallback`（`:506`，**默认关闭**）开启；同时把非法输入从静默兜底改成抛 `ValueError` |
| `adapters/wind_source.py::fetch_adjust_factors` | 台账决策 C1 | **已接线（R22）** | 同上调用链 `scripts/factor_backfill.py:210`（引用）与 `:221`（`fetch_adjust_factors` 调用）；取数失败只转文案、绝不向上抛 |

> ⚠️ **前两版本文里的 `scripts/factor_backfill.py:130` 是错行号**，且"有 bug/两份重复实现"的描述
> 已过期（`_to_wind_code` 的顺序 bug 在 R17 已修，那份重复的 `_code_to_tx` 也已删除）。
> R22 复核后按上表更正。

> ⚠️ **这两个 Wind 符号在 `whitelist.py` §4c 里保留，但性质已变**：它们**不是待接线**，
> 而是**门禁盲区**——CI 的 vulture 只扫 `cpt/`（`.github/workflows/ci.yml`），调用方在
> `scripts/`，于是已接线的符号照样被报"未使用"。要摘这两条需把 vulture 范围扩到
> `scripts/`，实测那样会另带出 `scripts/factor_backfill.py` 里 2 条真死代码
> （`REPO_ROOT` 未使用变量、`latest_factor_date` 未使用函数），属本次范围外。

---

## 约束

1. **改动本清单内模块前先读本文**——它们没有生产调用方，改错了不会有测试变红。
2. 这些模块的现有测试是**自证式**的（测一段不运行的代码），**不能**当作「已在生产验证」。
   R19 的接线守门用例补了一条新规矩：**入口必须是生产构造函数**
   （`build_ashare_snapshot`），只改模块函数体而忘接线的改动**不会**被原有用例发现。
   R22 沿用这条：`tests/test_dashboard_wiring_d.py` 的守门用例把 spy 挂在**使用点**
   （`monkeypatch.setattr(dashboard_mod, "quality_report", spy(...))`）——实测删掉
   `v2["watch_metrics"] = ...` 一行立刻 `assert 0 == 1`。
3. CI 的 vulture 门禁（`.github/workflows/ci.yml`，阈值已从 80 降到 60）对这批名字走
   `whitelist.py` 白名单。白名单是「已知未接线」的登记，不是「忽略告警」。
   **注意白名单的覆盖范围只到 `cpt/`**：调用方在 `scripts/` 的符号，即使已接线也会被报
   未使用（R22 实测，见「另有 3 个函数」）。
4. 接线时**先写从生产入口可达的集成测试**，再把模块从本清单和 `whitelist.py` 里移除。
5. **删模块时三处同步**：本文件 + `whitelist.py` + `tests/test_<module>.py`。
6. **白名单是逐个符号的，不是逐个模块的**：一个模块接了一半，剩下的符号仍要留豁免
   （例：`a_share_rules` 的标签函数已摘，`t_plus_one_purchase_allowed` 仍留）。
7. **接线的三层要一起补**：R22 勘察发现 D 类 9 个功能是「后端函数 + HTTP 路由 + 生产
   数据源」**三层全缺**，只补其中一层等于没接。销账前必须对每条路由做真实 curl，
   而不是只看单测——单测里的替身会掩盖"路由不存在"。
8. **别把「同名/相近概念」当「已接线」**：R19 因此误判过 4 个模块（见上方复核踩坑记录）。
   判据是**函数体**，不是函数名。

## 相关

- `docs/archive/audits-2026-09.md`（2026-09 审计合并件）§3.4（孤儿层与死模块）、§5.1（dbconfig 三胞胎）、§4（P0 清单）
- `docs/architecture.md` §2（`engine/` 与 `storage/` 两层的删除说明）
- `docs/archive/plans-and-acceptance.md`（Phase 0-6 功能描述，判据 2）
