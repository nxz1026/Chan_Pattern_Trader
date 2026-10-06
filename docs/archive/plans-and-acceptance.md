# CPT 计划与验收文档合并件（历史记录，勿当现状）

> **归档件**：2026-10-06 由 7 份计划 / 验收文档合并而成，原件已 `git rm`（内容可从
> git 历史取回）。**判定优先级：本件低于全部现行文档** —— 凡与 `README.md`、
> `docs/architecture.md`、`docs/rules.md`、`docs/progress-log.md`、
> `docs/pending-wiring.md`、`docs/web-api-reference.md`、`docs/known-traps.md`
> 不一致，**一律以现行文档为准**。
>
> 本件只保留两类内容：**仍在生效的最终立场**（§1、§2）与**已被取代的结论清单**（§3）。
> 里程碑任务分解、当时被否决的方案、会议与提交考古一律不再保留。
>
> 放在 `docs/archive/` 下：不参与 `check_line_refs.py` 的现行行号校验。
> **本件不含行号**（原件里的行号多为当时现场，且多处已漂）。

## 0. 合并映射：7 份原件各自去了哪

| 原件（均已删除） | 现由哪份现行文档负责 |
|---|---|
| `docs/implementation-plan.md`（M0–M6 里程碑） | `docs/architecture.md`（层与依赖方向）、`docs/rules.md`（规则与冻结参数）、`docs/progress-log.md`（轮次与当前工作） |
| `docs/dashboard-product-roadmap.md`（产品 Phase 0–6） | `docs/web-api-reference.md`（真实接口清单）、`docs/pending-wiring.md`（每个 Phase 的接线状态） |
| `docs/dashboard-plan.md`（实施 D0–D6） | 同上，加 `docs/export-schema-v1.md`（数据集导出 schema v1） |
| `docs/dashboard-final-acceptance.md`（2026-09-25 验收快照） | `docs/web-api-reference.md`、`README.md`「质量门」 |
| `docs/m6-quality-report.md`（M6 质量报告） | `README.md`「质量门」、`docs/progress-log.md` |
| `docs/low-risk-hardening.md`（低危增强记录） | 大部分已落在 `docs/export-schema-v1.md` §4 与 `docs/architecture.md`；**其中一条至今无归处**，本件代为在册 → §2.5 |
| `docs/post-cutover-plan.md`（切表后工作计划） | `docs/progress-log.md`、`docs/db-inventory-and-cleanup.md`、`README.md`；仍生效的切表约束 → §2.1 |

---

## 1. 仍在生效的最终立场

### 1.1 参照仓库

- 唯一后端参照是 **czsc**，固定 `701e480a`，Apache-2.0，**可选依赖** extra `chan`，
  经反腐层复用分型 / 笔 / 力度度量 / 一买谓词。
- **wbt** 固定 `39bb1e8a`、MIT —— **R51 画布 D 下线后已无任何代码引用**。
- PyPI 上的 MIT 版 `chanlun` 包**不存在**；`chanlun-pro` / `chanlun.py` /
  `chanlun_pine` 与 Rust `chanlun==2606.73` 已于 2026-09-24 全部移除。
- 固化脚本 `scripts/fetch_references.sh`：clone 后 checkout 到固定 commit，校验
  HEAD hash 与 LICENSE，任一不符即非 0 退出。许可证与复用边界见
  `docs/reference-audit.md`。

### 1.2 里程碑 M0–M6 的最终状态

| 里程碑 | 最终状态 | 归口 |
|---|---|---|
| M0 基线与工具链 | 已完成（references 固化、许可证审计、ruff/mypy/pytest/import-linter 工具链） | `docs/reference-audit.md`、`.github/workflows/ci.yml` |
| M1 单级别全链路 | 已完成（包含→分型→新笔→笔中枢→JSON 导出；schema v1 已冻结） | `docs/export-schema-v1.md` |
| M2 oracle 对照 | **口径已变更**：不做算法等价证明；dashboard 侧的对照面板改走 **czsc 优先 / 回落腾讯**，经 `GET /api/dashboard/parity` 暴露 | `docs/pending-wiring.md` 簇二、`docs/web-api-reference.md` |
| M3 走势类型 + 递归 + 一买 | 已完成基础实现；一买背驰走 **czsc 笔力度口径**，力度度量未填充时**响亮报错不静默降级** | `docs/rules.md`、`docs/progress-log.md` |
| M4 数据与回放 | 已完成（Binance Futures 窄接口、缺口检测、批量 / 单根回放） | `docs/architecture.md` |
| M5 实时预警 | 已完成（未收盘只 `alert`、收盘升级）。**生产实时路径不在 `cpt/engine/`** —— 见 §1.3 | `docs/rules.md`、`docs/architecture.md` |
| M-LLM 独立 LLM 服务层 | 已落地（`cpt/llm/`）。**可关闭、可审计**；`llm.enabled=false`（默认）整层短路。用例与预算的最终口径见 §2.2 | `docs/architecture.md` §4 |
| M6 质量验收 | 已完成。**M3/M4/M5 未达生产交易系统级别**，不得对外宣称 | `README.md`「质量门」 |

### 1.3 分层与依赖方向

- 原计划的 `cpt/engine/`（`historical.py` / `realtime.py` / `rebuild.py` /
  `events.py` / `store.py`）**只落地过 2 个文件且生产零导入，整层已于 2026-09-25 移除**。
  生产实时路径由 `cpt/web/__main__.py` 的 `_RealtimeProvider` 自包含承担（poll-driven）。
- `cpt/storage/` 曾整层删除，**2026-10-01（R24）恢复**，位置在 application 与 domain 之间。
- `.importlinter` 共 **6 条**契约（5 条 `forbidden` + 1 条 `layers`），全部 KEPT。
  **不是 5 条、也不是 3 条** —— 原件里这两个数都过期了。
- `layers` 是**自上而下**写的：第一条是最高层，高层可导入任意低层，反向不行。
  写反会立刻 `BROKEN`，补契约时必须做反向验证。

### 1.4 Dashboard 的只读边界

原件多处写「首版只读 / 全程只读」，**已不成立**。当前有 **3 条写路径**：

```
POST   /api/dashboard/a-share/llm/explain
POST   /api/dashboard/a-share/llm/summarize
POST   /api/dashboard/a-share/watchlist
DELETE /api/dashboard/a-share/watchlist
```

- 写接口**无鉴权**（单用户看板经 nginx `auth_basic` 暴露，应用层不做认证）。
  唯一的客户端侧守卫是**必须带 `Content-Type: application/json`**，否则 415。
  它挡的是 CSRF，**挡不住**任何能主动设 CT 的客户端 —— 不要把这层当认证用。
- 行情与结构数据仍**只读**：不下单、不撤单、不给账户 / 持仓 / 盘口，
  不提供任何修改结构数据的写操作，不让 LLM 决定分型 / 笔 / 中枢 / 买卖点。
- 本地研究注释只写 localStorage，**不进入数据集、不影响 dataset hash、不改结构数据**。
- 真实接口清单（26 条）见 `docs/web-api-reference.md`。原件那份 D1 接口清单里
  的 `/candles` 与 `/events` **从未作为独立接口存在**，数据都在
  `GET /api/dashboard/snapshot` 的顶层；`/api/dashboard/stats` 代码里没有。

### 1.5 `dashboard.v1` 契约（`cpt/application/dashboard.py`）

顶层**固定 8 个键**：`schema_version` / `market` / `candles` / `overlays` /
`signal` / `events` / `data_quality` / `runtime`（`DASHBOARD_SCHEMA_VERSION`
= `dashboard.v1`）。约束：

- 时间统一 **Unix 毫秒**；价格 / 量沿用 `CanonicalBar` 原始单位，不做换单位；
- 结构对象保留 `level` / `source_ids`（`asdict` 原样透出），走势类型额外保留
  `kind` / `direction`；
- **未收盘必须标 `alert`，不得伪装 `confirmed`**；
- 缺口、乱序、stale 必须显式返回，不静默。

`dashboard.v2` 是另一层组合（`cpt/application/dashboard_snapshot_v2.py`），
两者不是同一份契约。

### 1.6 前端真实结构（原件的目录树已作废）

原件写的 `dashboard/js/*.js` 拆分方案与 `dashboard/dashboard.js` 兼容入口
**均不存在**。当前是：

- 源文件 **7 份** `dashboard/dash-*.js`（`dash-core` / `dash-chart` /
  `dash-alert` / `dash-signal` / `dash-ops` / `dash-structure` / `dash-chrome`），
  各自成模块、互不认识谁在谁前面；
- 由 `scripts/build_dashboard_bundle.py` **拼成单个** `dashboard/dashboard.bundle.js`
  —— 维护成本按模块拆，浏览器仍只下载一个文件；
- `python scripts/build_dashboard_bundle.py --check` 是 CI 门禁，bundle 必须与
  `dash-*.js` 同步；
- 静态资源由 nginx 从 `/var/www/cpt-dashboard` 提供、**不经 Python**。
  改前端要同步那个目录，用 `deploy/dashboard-sync.sh`（仓库→部署目录→线上 HTTP
  三层校验）。

### 1.7 回放 CLI 的真实签名

```
python -m cpt.application.replay --input <fixture.json> --output <out.json>
                                 [--backend fixture|native] [--validate-only]
```

- 原件写的 `--export` **是错的**，实际是 `--output`。
- `--backend` 只接受 `fixture`（占位）/ `native`（自研 domain 管线）。
- `--validate-only` 只校验 bar（去重 / 顺序 / 连续性 / OHLC），打印行数与时间范围，
  **不写结构文件**。
- 低层兼容入口 `run_replay()` 保留（不校验），批量 / 前缀入口 `replay_bars()` /
  `replay_incremental()` 走严格校验。校验与回放导出分离是刻意设计。

### 1.8 人工 fixture 的时间契约（硬约束）

`tests/fixtures/` 下 8 份人工 fixture（case1–case8）全部已迁移到**真实 5m 契约**：

| 项 | 值 |
|---|---|
| 起点 `open_time` | `1706745600000`（= 2024-02-01T00:00:00Z，与 oracle 冻结快照同源） |
| `interval_ms` | `300000` |
| `close_time` | `open_time + interval_ms - 1`（= `open_time + 299999`） |
| OHLC | `open` / `close` 必须落在 `[low, high]` 闭区间内 |

守门用例：`tests/test_replay_integration.py::test_fixture_passes_m4_entry`
（对旧数据实测 FAIL，确认是真守门而非同义反复）。
**新增 fixture 必须过这个用例**，否则等于退回早期 600ms 演示时间边界。

---

## 2. 仍在册的约束与开放项

### 2.1 复权因子切表：仍生效的约束

最终态 **5206 票 / 3,339,427 行 / placeholder 0**（切前 5222 票 / 106 占位）；
切换点登记在 `cpt_factor_epoch` 单行表（`switched_at` 被最后一次切表覆盖）。
数字与口径见 `docs/progress-log.md` 与 `README.md`。以下四条**至今生效**：

1. **两表列结构不同，生产表没有 `basis` 列。** 照暂存表的列写切表 SQL 会在真库
   上直接报错：

   | 生产表 `asel.ref_adjust_factor` | 暂存表 `asel.ref_adjust_factor_v2` |
   |---|---|
   | code, trade_date, hfq_factor, source | code, trade_date, hfq_factor, **basis**, source |
   | **source_url, source_ref, as_of, available_at, fetched_at** | **computed_at** |

   生产侧独有列（腾讯链路的元数据）留 NULL —— 它们描述「从哪儿抓的」，
   重算值没有这个来源。**实测无任何代码读它们。**
2. **脚本绝不自动切。** 重算只写暂存表；切不切是人工决定（看
   `scripts/factor_report.py` 的报告再定）。暂存表是再切表时的数据来源与回滚能力，
   **不能删**。
3. **不用停服务，也不用清缓存。** Postgres 是 MVCC，读者要么看到全旧、要么看到全新；
   因子读取路径上**没有任何缓存**。
4. **切表改变历史 K 线形状，不改变今天显示的价格。** `anchor_scale` 把最新一根
   锚定到生产现值，所以最新价天然不变；历史那段必须被修（跨除权日要连续）。
   缠论只认**相对比值**（分型与背驰都是比值判定），所以结构会变、绝对量级不变。
   ⇒ **不需要通知用户「价格变了」**；但中位偏差大的票（如 000002）**会改变结构判定**，
   这是修 bug 的目的，不是回归。

**不受切表影响**：`cpt_structure_event` 的 payload 由
`cpt/domain/structure_events.py::state_to_payload` 生成，12 个键
（id / level / kind / direction / start_time / end_time / status / revision /
first_seen_at / confirmed_at / invalidated_at / source_ids）
**没有任何价格字段**，故历史事件流不必处置。

**口径断点的处置已定**：`cpt_signal_event` 里的旧口径信号按
`cpt_factor_epoch.switched_at` 分「旧 / 新」呈现（`/recommendation` 响应的
`history` 带 `legacy_count` / `current_count`），不是删除。见
`docs/web-api-reference.md` 与 `docs/db-inventory-and-cleanup.md` §8。

### 2.2 LLM 层的最终口径（与原件写的不一样）

原件描述的形状有 4 处与代码不符，以代码为准：

| 项 | 原件写法 | 实际 |
|---|---|---|
| 用例 | 三个（规则解释 / 差异摘要 / 标注辅助） | **两个**：`cpt/application/llm_cases.py` 的 `explain_structure`（规则解释）与 `summarize_recommendation`（给结构判断配人话）。差异摘要 / 标注辅助未实现 |
| 预算 | `cpt/llm/budget.py`（每日 / 每次预算与速率限制） | **该模块不存在**。只有 `cpt/llm/base.py` 的 `LLMRateLimited` 异常类型（429 退避重入） |
| 缓存 | `cpt/llm/cache.py`（请求 hash 缓存） | **该模块不存在**。等价能力由唯一索引 `idx_cpt_llm_call_request_hash`（`(purpose, request_hash)`，限 `queued/running/ok`）承担幂等去重 |
| 审计表 | `llm_call` | **`public.cpt_llm_call`**（`scripts/migrations/2026-10-03_r25_llm_call.sql`） |

`public.cpt_llm_call` 的列：`call_id` / `purpose` / `subject_id` / `status` /
`request_hash` / `result_text` / `error_text` / `model` / `prompt_tokens` /
`completion_tokens` / `created_at` / `finished_at`。
**没有费用列** —— 实测上游免费档的成本响应头恒为 `0.0`，故不伪造成本估算。
`status` 取值受 CHECK 约束：`queued` / `running` / `ok` / `error` /
`rate_limited` / `interrupted`。

其余约束（`docs/architecture.md` §4）仍成立：**永不阻塞核心**（domain / storage /
adapters / web 都不 import `cpt.llm`，只有 `application/` 的用例调它）、
**永不改结构**（只消费已产出的结构 JSON，产出文字建议）、
**可关闭**（`LLMConfig.enabled` 默认 `False`，环境变量 `CPT_LLM_ENABLED`）、
**入队即返回**（HTTP 不等模型，落库拿 `call_id` 在前、入队在后）。

### 2.3 Dashboard 仍在册的限制

- **`/api/dashboard/export` 是纯 API，前端没有面板**，只能手工 / 脚本调用
  （真机 grep 部署目录无任何文件引用它）。它**只切 `candles` 与 `market`**，
  其余块（`level_tree` / `overlays` / `indicators` / `data_quality` /
  `reproducibility` / `engine_state` …）**原样保留完整窗口内容**，内部
  `bar_index` 仍按完整窗口计数 ⇒ **拿切片响应按下标取结构会越界**。
  它与 `docs/export-schema-v1.md` 的**数据集导出 schema v1 是两件事**，后者未动。
- **24h 数据分市场**：加密 realtime 路径有真实 24h（`fetch_24h_ticker` →
  `normalize_24h`，上游不可达时降级为 `{"available": false, "reason":
  "upstream_ticker_unavailable"}`）；**A 股没有 24h 行情源**，前端按
  `available: false` 显示「—」。盯盘投影里的窗口统计来自 bars，**不是 24h 聚合**。
  判据见 `docs/pending-wiring.md` 簇三。
- **两个「对比」不是同一件事**：`dual_compare` 是 CPT 本地后复权收盘价 vs 上游
  **同口径**现价；`GET /api/dashboard/multi-run?run_ids=a,b,c`（2–5 个）比的是
  **本进程内多次 run 的快照**，按 `open_time` 对齐，输出
  `{run_count, timestamps, points[{open_time, run_0, run_1, …}]}`。互不替代。
- **本地注释的 localStorage key** 是 `cpt-note:{symbol}:{level}:{kind}:{start}`
  （`dashboard/dash-structure.js`）。注释不进数据集、不影响 hash。
- **信号统计必须自报口径**：`/api/dashboard/signal-stats` 的转化率带
  `"basis": "signal_event_transitions"` —— 是**事件流里的跃迁率**，不是
  「当前若干只票的状态」；无库时返回
  `{"available": false, "reason": "signal_history_unavailable"}`，**不假装是 0**。
- **画布计数 ≠ API 计数**：画布侧经可视窗口过滤（`applyZoomWindow`）只画与当前
  窗口相交的结构，两边本就不该相等。见 `docs/web-api-reference.md` 附录。

### 2.4 仍在册的开放项

原件里的 P0 / P1 / P2 清单**多数已闭合**，不要照着推进：

| 原件里的项 | 最终状态 |
|---|---|
| 轮换 `emotion_core` 口令（曾明文进 git 历史，仓库 PUBLIC） | **未闭合**；另 owner 已明确表示**不轮换** `deploy/dashboard-sync.sh` 的默认 Basic Auth 口令（可被 `CPT_BASIC_AUTH` 覆盖），该项只记录。见 `docs/archive/reviews-r45.md` §5.2 与 `README.md` |
| 78 只「分过红但算不出」 | **未闭合**。根因是除权日早于 bar 起点（2024-01-02），取不到除权前收盘。要治得往前拉 K 线起点，同属 ingest 侧工程 |
| 28 只保留旧真值 | **已闭合**：最后一次切表改用东财重算补回（它们与上条同根因） |
| 看板静态资源与仓库各改各的 | **已闭合**：`deploy/dashboard-sync.sh`（三层校验）是门禁配套流程 |
| A 股 K 线缺口 | **未闭合**，成因在 ingest 侧（另一个项目）。原件的「1~2%」**已过时** —— R45 实测真实量级是 **11.9%**（623 只票中间缺口），且缺口主要在 `derived_bar`、不是 `daily_bar`。见 `docs/known-traps.md` #23 |
| M-LLM 独立线 | **已落地**（`cpt/llm/`），不再是开放项；但**差异摘要 / 标注辅助两个用例与预算模块从未实现** → §2.2 |
| oracle parity | **已复活**（czsc 优先 / 回落腾讯），不再是「永久未启用」 |
| 实时引擎增量优化 | **作废**：它描述的 `cpt/engine/realtime.py` 已随整层删除；现行实时路径见 §1.3 |
| `t_plus_one_purchase_allowed` 未接线 | **仍在册**，是 `docs/pending-wiring.md` 唯一的待接线项 |

### 2.5 一条至今无归处的规则

原件 `docs/low-risk-hardening.md` 的「保留边界」段是**长期有效的设计约束**，
但**现行文档里没有等价表述**，故本件代为在册：

> `CanonicalBar` 继续承担**输入层的完整数值校验**；其他结构 dataclass 的构造校验
> 逐步增强，但**数据库 / 外部 JSON 入口仍必须经过 application / adapters 的验证层**
> —— **不把裸 dataclass 构造当作不可信输入边界**。

**建议去处**：`docs/rules.md`（属规则口径）或 `docs/architecture.md`（属分层职责）。
本件不替现行文档决定这件事。

---

## 3. 已被取代 / 作废的结论（不要再当判据）

| 原结论 | 取代它的现行立场 |
|---|---|
| 「M6 一买模块只实现状态机、背驰计算未接入」 | 已实现，走 czsc 笔力度口径（`cpt/domain/first_buy.py::_is_divergent`），力度未填充时 `_require_power_metrics` 显式报错 |
| 「历史人工 fixture 仍是 600ms 演示时间边界」 | 8 个 fixture 全部迁到真实 5m 契约并加守门用例 → §1.8 |
| 「czsc 对照已取消、永久未启用」 | parity 已复活：czsc 优先 / 回落腾讯，`GET /api/dashboard/parity` |
| 「Dashboard 始终只读」 | 有 3 条写路径（§1.4） |
| 「首版只读、不承诺交易生产能力」 | 同上；对外承诺仍只写「结构状态翻译，非投资建议」 |
| 「`/api/dashboard/candles`、`/events`、`/stats` 是接口」 | 前两个从未作为独立接口存在，第三个代码里没有 → §1.4 |
| 「M0–M2 用 Rust `chanlun==2606.73` 对照」 | 参照已于 2026-09-24 换成 czsc → §1.1 |
| 「切表后生产表仍未动」 | 已切 4 次，最终态见 §2.1 |
| 「孤儿行 134 / 非单调 code 26 ⇒ 因子会向下跳 2544 只」 | **该结论是错的**（判据用错，实测 0 只）。见 `docs/known-traps.md` #25 / #26 的自纠错记录 |
| 「import-linter 5 条契约」（或「3 条」） | 6 条（5 `forbidden` + 1 `layers`）→ §1.3 |
| 「`scan_stale_dependents()` 只能识别版本化引用」 | 该函数**已不在代码里**（全仓零命中），此限制随之失效 |
| `scripts/compare_oracle.py` 相关门禁 | 该脚本已于 2026-09-24 `git rm`；CI 里的 oracle 固定版本 job 同批删除 |
| 「M6 验收数字 511 passed / 107 passed」 | 全部过时，勿引用。现行门禁见 `README.md`「质量门」 |
| 「画布 D（`wbt.report.HtmlReportBuilder`）在跑」 | R51 下线，`/api/canvas/wbt` 回 404；现为**三画布**（A/B/C） |
| 「M3（canvas iframe 信任边界）至今仍开放」 | **已闭合**。该风险全部来自 `dashboard/canvas_d.js` 的 `doc.body.innerHTML = payload.body_html`（配 `sandbox="allow-same-origin allow-scripts"`），而画布 D 已随 R51 删除。实测现存 `dashboard/*.js`（含 A/B/C 与 `canvas_registry.js`）**零 `innerHTML`、零 `sandbox`** ⇒ 该风险类别不复存在 |
