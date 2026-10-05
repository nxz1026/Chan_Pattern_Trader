# CPT 软件结构设计

版本：v0.1 · 2026-09-22
状态：替代旧文档 `docs/design-review.md` 与 `docs/architecture-reference-audit.md`（已删除）；规则口径以 `docs/rules.md` 为准，实施节奏见 `docs/implementation-plan.md`。

## 1. 设计目标

1. **结构清晰**：单一职责分层，依赖方向在 CI 中强制，不靠自觉。
2. **虚拟/物理分层清晰**：缠论算法（虚拟）不知道交易所、数据库、绘图、LLM 的存在；物理层（交易所/存储/大模型）通过窄接口适配接入。
3. **各 API 干好自己的活儿**：每个模块一个窄接口，输入输出明确，不做"顺手"的事。
4. **避免代码屎山**：一套结构算法对 `BarLike` 泛化，跨级别复用；配置集中；领域对象不可变。
5. **高复用率**：基础口径（分型/新笔/力度度量）经反腐层委托给固定版 `czsc`；CPT 自研笔中枢、走势类型、递归映射、一买状态机这些 czsc 未覆盖的核心。

## 2. 分层总览

```text
┌─────────────────────────────────────────────────────────────┐
│ web/              HTTP 入口：handler / 路由 / CLI（--mode）    │
├─────────────────────────────────────────────────────────────┤
│ application/      用例编排：replay / inspect / export /       │
│                   dashboard / a_share_snapshot / llm_cases   │
├─────────────────────────────────────────────────────────────┤
│ llm/              独立 LLM 服务层：config / queue / provider / │
│                   prompts / structured（不碰 SQL）            │
├─────────────────────────────────────────────────────────────┤
│ storage/          持久化：**SQL 只许出现在这一层与 adapters/**  │
│                   signal_event / structure_event / llm_call / │
│                   dashboard_run                               │
├─────────────────────────────────────────────────────────────┤
│ adapters/         外部接入：Binance / ccxt / Wind / 腾讯 /     │
│                   本地 A 股库 / 三种缠论实现                    │
├─────────────────────────────────────────────────────────────┤
│ domain/           纯算法与领域模型（零第三方依赖，全部纯函数）    │
└─────────────────────────────────────────────────────────────┘
```

> **这张图在 2026-10-01（R24 / R25）又改过两次。** 2026-09-25 那次重画把
> `engine/` 与 `storage/` 标成「整层删除」，理由是它们只有四个文件且生产代码零导入。
> 半年后 R24 发现 **SQL 其实一直散落在 `domain` / `application` / `web` 里**，
> 于是**恢复了** `storage/` 作为唯一的持久化层，并加了 CI 门禁
> （`scripts/check_sql_layering.py`，7 条门禁之首）强制「SQL 只许出现在
> `storage/` 与 `adapters/`」。R25 又补上了 `llm/`。
>
> **教训值得留着**：那次「整层删除」是对的（四个孤儿文件该删），但它**顺手把一张
> 描述现状的图当成了描述设计的图** —— 于是文档画着不存在的层，读者与后来的审核
> 都以为现状就是设计。删层和改图是两件事，删层时必须同时改图。

### 2.1 各层复盘状态（R45 核实并更正，2026-10-03；行数刷新至 2026-10-04）

「复盘」指**以该层为单位**做过的勘察 + 重做（补边界、加门禁、真机验证、修 bug），
不是被单点改动顺手碰过。

⚠️ **这张表在 R45 之前是错的，而且错在两个方向**：
数字几乎每行都对不上；更要紧的是**把「建了/修了」当成了「复盘了」**。
详见下方 §2.1.1。

| 层 | 文件 | 行数 | 复盘状态 |
|---|---:|---:|---|
| `storage/` | 7 | 1,697 | ✅ **已做**（R45：全层勘察 + 3 个真 bug + 门禁②；R24 只是恢复层+划边界，**不算复盘**。详见 `docs/review-storage-layer-r45.md`） |
| `llm/` | 8 | 1,370 | ✅ **已做**（R45：全层勘察 + 3 个真 bug —— 清空进程环境 / 归类撒谎 / 单例丢弃审计回调） |
| `adapters/` | 20 | 6,015 | ✅ **已做**（R45：全层勘察 + 3 个真 bug + 外部契约真机验证脚本 `scripts/verify_public_contracts.py`；顺带实测更正了 handoff 里 K 线缺口的量级） |
| `application/` | 33 | 5,702 | ✅ **已做**（R45：全层勘察 + 3 个真 bug —— DB 故障被报成「缺因子」两处 + CDN 护栏后门） |
| `domain/` | 16 | 2,964 | ✅ **已做**（R30 语义契约门禁；R45 重扫 16 文件，**未发现需修问题** —— 递归/中枢/级别标签不变量实测通过） |
| `web/` | 5 | 3,035 | ✅ **已做**（R29 门禁仍成立；R45 重扫 5 文件，**未发现需修问题** —— 4 处候选经真机实证均为假阳性） |
| `dashboard/`（前端） | 7 | ~1.6 MB | ⚠️ 只被画布 D 与几个面板碰过 |
| `engine/` | — | — | 🚫 2026-09-25 整层删除（四个孤儿文件，R24 未恢复） |

> ⚠️ **「文件 / 行数」是快照，会随改动腐坏** —— R45 第二轮扫文档时发现
> 6 层**全部**偏低，正是因为当天加的文件没回填。CI 已接
> `scripts/check_doc_drift.py`，A 类（计数）/ B 类（复盘状态）/
> C 类（冻结参数）/ D 类（接口路径）任一漂移即红，**别再手改这张表**。
>
> 数字来源：R45 用 AST + `wc -l` 实测（不是沿用旧表）。旧表声称的
> `storage` 5/1,070、`llm` 7/1,092、`adapters` 16/4,519、`application` 29/4,182
> **全部偏低** —— 旧表由 commit `7791a5b` 写下后，同一天又落地了
> `feishu.py` / `run_metric.py` / `dashboard_parity.py` / `parity_reference.py`
> 等文件，表没跟着更新，而标签仍写着「2026-10-02 实测」。

#### 2.1.1 一条被 R45 推翻的「已完成」判断

旧表给 `storage/` 和 `llm/` 都打了 ✅。翻 `progress-log.md` 的章节标题：

| 层 | 旧表标注 | 实际做过的 | 真复盘过吗 |
|---|---|---|---|
| `storage/` | ✅ 已做 | `R24 · **恢复** storage 层，SQL 只许出现在 adapters/storage` | ❌ **没有** —— 恢复+划边界 |
| `llm/` | ✅ 已做 | `R25 · **独立 LLM 服务层**（异步+429 退避重入）` | ❌ **没有** —— 建层 |
| `web/` | ✅ 已做 | `R29 · **web 层复盘**` | ✅ 有 |
| `domain/` | ✅ 已做 | `R30 · **domain 层复盘**` | ✅ 有 |

**把「建了/修了」记成「复盘了」，与那个被推翻的「14 个模块未接线」是同一类错误** ——
用一个 ✅ 掩盖了没做过的事。R45 重做 storage/ 之后，在两天内就撞出 **2 个真 bug**，
印证了它此前确实没被认真看过。

**下一层建议做 `adapters/`**（`llm/` 复盘完成后接着做）：它是唯一
「出去问别人要数据」的层，外部契约（Wind 字段、腾讯 K 线格式、PG schema）漂移了
不会让任何测试变红 —— 而这正是 R28 反复吃的那类亏。
外部触点密度实测（AST 扫网络/进程/DB）：`adapters` 6/19 文件（31.6%）、
`web` 1/5、`llm` 1/8、`application` 1/32、`domain`/`storage` 均 0。

> ⚠️ **一条已被推翻的旧结论**：`docs/audit/cpt-code-audit-20260930.md` 与 09-25 审计
> 都写着「14 个 `dashboard_*` 模块无生产导入方」。2026-10-02 用 AST 逐个核对
> `import` 后确认：**19 个全部有生产引用，孤儿数为 0**。它们在
> `cpt/application/`（18）与 `cpt/storage/`（1），不是 `adapters/`。R16-5 四画布、
> R20/R23 runs、R24~R26 陆续把它们接上了，审计结论早已过期。
> **别照抄审计里的「未接线」清单，先用 AST 复核。**

依赖方向（`.importlinter` 的 `layers` 契约强制，实测依赖图见下）：


```text
web         → application / llm / storage / adapters / domain
application → llm / storage / adapters / domain
llm         → （不导入任何上层；自己不碰 SQL）
storage     → domain
adapters    → domain
domain      → （不导入任何上层）
domain 不导入 pandas / httpx / ccxt / fastapi / sqlalchemy / torch / openai / 绘图库
```

> 契约是**自上而下**写的（`layers` 第一条是**最高**层），高层可以导入**任意**低层
> （`web` 直接用 `domain` 是允许的），反向不行。`.importlinter` 共 6 条，全部
> `KEPT`（`lint-imports` 是 7 条门禁之一）：
> 「Domain 无第三方依赖」「Adapters 不漏进 domain」「Storage 不漏进 domain」
> 「LLM 不漏进 domain」「LLM 不漏进 storage」「Layered architecture」。
>
> 后两条是 R25 专门为 `llm/` 加的 —— 它是**独立服务层**，只有 `application/llm_cases`
> 可以调它，`web` / `storage` / `adapters` / `domain` 都不得依赖。

## 3. 各层职责

### 3.1 domain/ — 纯算法与领域模型（虚拟层）

唯一允许存在缠论概念的地方。无状态、无副作用、同输入必同输出。

| 模块 | 职责 | 输入 → 输出 |
|---|---|---|
| `types.py` | `BarLike` 协议（`high/low/open_time/close_time/direction`），K 线与结构元素的公共抽象 | — |
| `models.py` | 全部领域对象（见 §6），`@dataclass(frozen=True)` | — |
| `config.py` | `RulesConfig`：全部规则口径枚举 + 结构元数据，可序列化 | — |
| `chan_bar.py` | 包含合并，产出缠论K线 | `BarLike[] → ChanBar[]` |
| `fractal.py` | 分型识别（`fx_qy_middle`/`fx_qj_ck`） | `ChanBar[] → Fractal[]` |
| `bi.py` | 新笔（`bi_type_new`） | `Fractal[] → Bi[]` |
| `zhongshu.py` | 笔中枢、`level`、`zs_wzgx` | `Bi[] → ZhongShu[]` |
| `trend_type.py` | 走势类型构成与完成分类（CPT 自研） | `Bi[] + ZhongShu[] → TrendType[]` |
| `recursion.py` | 走势类型 → 结构元素（适配为 `BarLike` 喂回同一条管线） | `TrendType[] → StructureElement[]` |
| `signal.py` | 一买状态机（CPT 自研） | 结构序列 → `Signal[]` |

**核心设计：一套算法，两层输入。** 分型/笔/中枢算法全部写成对 `BarLike` 序列的纯变换。K 线层和高级别结构元素层共用同一条管线，递归只负责适配输入——这是避免"为每个级别复制一套算法"的关键。

### 3.2 engine/ — 有状态编排（**已整层删除，2026-09-25**）

原规划 5 个模块（`historical.py` / `realtime.py` / `rebuild.py` / `events.py` /
`store.py`），**实际只落地过 `realtime.py` 与 `rebuild.py` 两个文件，且生产代码零导入**
—— 生产实时路径由 `web/__main__.py` 的 `_RealtimeProvider` 自包含承担（poll-driven），
历史回放走 `application/replay.py`。已按审核 P0-2 整层删除。

原模块表**不再保留**：它描述的是从未存在的模块，留着只会让人以为"曾经有过又删了"，
而不是"从没做过"。处置依据见 §2 与 `docs/audit/cpt-code-audit-20260925.md` §3.4。

### 3.3 adapters/ — 外部数据接入（物理层）

窄接口，一个适配器一件事：

```python
def fetch_klines(symbol, interval, start, end) -> list[CanonicalBar]
```

- `binance_futures.py`：Binance USDⓈ-M 永续 K 线，产出 `CanonicalBar`（字段见 rules.md §8.5）。
- `validators.py`：去重、缺口检测、连续性检查（独立于适配器，可单测）。发现缺口不自动填充，并阻止跨缺口生成正式结构。
- `reference_chanlun.py`：**后端契约**（见 §7.3）+ `native_chanlun.py`（自研后端）/ `czsc_chanlun.py`（czsc 后端）。不用 ccxt——抽象泄漏且重，httpx + 窄接口足够。

### 3.4 storage/ — 持久化（**2026-09-25 删除 → 2026-10-01 R24 恢复**）

> ⚠️ **R45 更正**：本节原本只写了「2026-09-25 整层删除」并就此结束，
> **漏掉了 6 天后 R24 的恢复** —— 于是文档在层已经回来 3 天的情况下，
> 仍然告诉读者「这一层不存在」。§5 目录结构、§2.1 复盘表都写的是恢复后的状态，
> 只有本节没跟上，**三处自相矛盾**。

原规划 SQLite 三层模型（**当前状态 + 不可变事件 + 信号**）只落地过 `models.py`
与 `repository.py`，2026-09-25 按审核 P0-2 整层删除。
**R24（2026-10-01）恢复**为 PostgreSQL 实现（不是复活那套 SQLite 三层模型）：

- 7 个 store，每个只做**一种**实体的持久化；
- `signal_id` 作为**稳定 upsert 主键**的设计保留（来自 `domain/signal.py`）；
- R45 复盘查出 3 个真 bug（DB 故障被当成「空数据」返回 3 处），
  并加门禁② `scripts/check_storage_failure_semantics.py` 锁住失败语义。

**为什么删除的层要恢复**：信号/结构事件落库是产品硬需求，
而「层被删了」不等于「需求消失了」。这与 §3.1 domain 的"虚拟层"不同 ——
storage 有真实的状态，不能靠进程内对象替代。

### 3.5 application/ — 用例编排

只做参数解析、调用 domain / adapters、输出。不含业务规则。

- `replay.py`：历史回放（批量/单根推进）
- `inspect.py`：查询结构与信号（CLI）
- `export.py`：JSON 导出（schema 版本化）
- `llm_cases.py`：LLM 用例（规则解释、差异摘要、标注辅助）——编排 llm 层，不含提示词

## 4. 独立 LLM 服务层

> ⚠️ **R45 更正：本节已从「蓝图」变成「现状」。** 原文写着
> 「`cpt/llm/` **从未有过代码**」「下面的模块与接口都还不存在」——
> 这在 **R25（2026-10-02）落地后就不成立了**：该层现有 8 个文件 / 1,300 行，
> R45 复盘又修掉 3 个真 bug（清空进程环境变量 / 合法 JSON 标量被误判为
> `not_json` / 单例丢弃审计回调）。
>
> 下面的设计**确实实现了**，但**不完全等于**实现：
> §4.2 的模块清单与 §4.3 的三个用例请以 `docs/rules.md` §9 与代码为准，
> 本节保留作**设计意图**记录。
> ⇒ 同一个错误在 R45 一天里出现了两次（本节 + `README.md`），
> 都是「写文档时是真的，后来变成假的，但没人回来改」。

`llm/` 是一个**独立、可插拔**的服务层：项目里任何需要大模型的地方，都只通过它访问；关闭它不影响任何核心功能。

### 4.1 设计约束

1. **永不阻塞核心**：缠论计算、存储、回放、导出不 import `llm/`；只有 `application/` 的 LLM 用例会调用它。
2. **永不改结构**：LLM 只消费已产出的结构/事件 JSON，产出文字解释或标注**建议**，绝不回写结构状态。标注落盘前必须经确定性校验 + 人工确认。
3. **可关闭**：`llm.enabled=false` 时整个层短路返回，用例优雅降级。
4. **可审计**：每次调用的 prompt 摘要、模型、token、费用、响应 hash 落盘到独立的 `llm_call` 表。

### 4.2 模块与接口

```text
llm/
├── config.py      # LLMConfig：enabled / provider / model / api_key / 预算 / 温度
├── base.py        # LLMClient Protocol + LLMResult（text/usage/cost）
├── registry.py    # 按 provider 名称构建 client（工厂，唯一知道具体 SDK 的地方）
├── prompts.py     # 提示词模板（集中管理，可版本化）
├── cache.py       # 请求规范化 → hash 缓存（可选，省钱）
├── budget.py      # 每日/每次预算与速率限制
└── providers/
    └── openai_compatible.py  # 首版唯一实现：OpenAI 兼容 HTTP 接口
```

```python
class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResult: ...

# application 层只依赖这个协议，不依赖任何 SDK
```

### 4.3 首版三个用例

| 用例 | 输入 | 输出 | 写回结构？ |
|---|---|---|---|
| 规则解释 | 结构/信号 JSON + 规则片段 | 自然语言解释 | 否 |
| 差异摘要 | CPT 与 czsc 的对照 diff | 差异原因说明 | 否 |
| 标注辅助 | 结构序列片段 | 标注建议 | 建议，须校验+人工确认 |

### 4.4 不做的事

不让 LLM 判断分型/笔/中枢/买卖点；不把 LLM 输出当作信号；不在 engine/domain 里调用 LLM；不引入 LangChain 等重型框架（一个 Protocol + 一个 OpenAI 兼容实现足够，学习价值更高）。

## 5. 目录结构

**按 2026-10-04 的仓库实况重写**（R45 全量扫描后）（原树写的是 `src/cpt/`，而实际根目录直接是 `cpt/`；
且列着 `engine/`、`storage/`、`llm/` 三个不存在的包与 `tests/unit|oracle|e2e` 四个
不存在的子目录 —— 那是 v0.1 的规划树，不是实况）：

```text
cpt/
├── domain/         纯算法与领域模型（16 个模块，零第三方依赖）
│   types.py  models.py  config.py  bi.py  fractal.py  contain.py
│   zhongshu.py  trend_type.py  recursion.py  signal.py  first_buy.py
│   a_share_rules.py  containment_trace.py  structure_events.py  levels.py
├── adapters/       外部接入（20 个模块）
│   binance_futures.py  ccxt_source.py  wind_source.py  a_share_public.py
│   a_share_local.py  a_share_pool.py  a_share_factor.py  strategy_signal.py
│   validators.py  reference_chanlun.py  native_chanlun.py  czsc_chanlun.py
│   reference_backend.py ← R45：**参照侧一等后端**（czsc→腾讯两级回落）
│   corporate_actions.py  eastmoney_actions.py  feishu.py  source_registry.py
│   backend_factory.py  _dbconfig.py
├── application/    用例编排（33 个模块；**不出现 SQL**）
│   replay.py  export.py  dashboard.py  dashboard_snapshot_v2.py
│   multi_level.py  a_share_snapshot.py  first_buy_bridge.py  canvas_wbt.py
│   recommendation.py  ← R45：结构判断摘要（**买卖与价格纯确定性，
│                          刻意不经 LLM**）；dashboard_runs.py
│   llm_cases.py  parity_reference.py
│   dashboard_*.py（另有 19 个面板投影，全部有生产引用）
├── storage/        CPT 自有持久化（R24 恢复；SQL 只许在这里和 adapters）
│   signal_event_store.py   → public.cpt_signal_event（R21）
│   dashboard_run_store.py  → public.cpt_dashboard_run（R23）
│   factor_epoch_store.py   → public.cpt_factor_epoch（R45：复权因子口径切换点）
│   llm_call_store.py  run_metric_store.py  structure_event_store.py
├── llm/            LLM 服务层（R25 落地，8 个文件含 providers/ 子目录）
│   base.py  config.py  prompts.py  queue.py  registry.py  structured.py
│   providers/openai_compatible.py
└── web/            HTTP 入口（5 个模块 + __init__）
    __main__.py  app.py  a_share.py  a_share_routes.py

scripts/            运维入口（不在包内，但已在 CI 门禁覆盖范围内）
    factor_backfill.py  factor_recompute.py  factor_report.py
    snapshot_a_share_batch.py  golden_set.py  fetch_references.sh
    compare_chanlun_backends.py   ← native vs czsc 真机对比
    verify_public_contracts.py    ← 腾讯/新浪契约真机验证
    check_sql_layering.py              ← 门禁①（分层内容）
    check_storage_failure_semantics.py  ← 门禁②（store 失败语义）
    check_doc_drift.py                  ← 门禁③（文档↔代码漂移）
    check_all_claims.py                ← 门禁④（全量文档断言，1890 条）
    check_doc_counts.py                ← 门禁⑤（文档里的计数断言）
    check_enqueue_skeleton_unique.py   ← 门禁⑥（LLM 入队骨架唯一）
    check_job_poll_unique.py           ← 门禁⑦（前端轮询唯一）
    selftest_gates.py                  ← **门禁自检**，必须排在 ①~⑦ 之前
    scan_doc_claims.py                 ← 「未兑现承诺」候选抽取（只报不判）
    report_coverage_gaps.py  run_coverage.sh  ← 覆盖率 + 「生产路径未测透」清单
    migrations/（8 份幂等 SQL，R20 → R27）

dashboard/          前端静态产物（Nginx 从 /var/www/cpt-dashboard 提供，非包内）
    url_safety.js  ← ⚠️ **必须第一个 defer 加载**：凭据消毒的**唯一实现**
    cpt_job.js     ← ⚠️ **必须第二个加载**：异步轮询的**唯一实现**
    dash-core.js   ← ⚠️ R45 拆分：核心 + boot()，**7 个 dash-*.js 里第一个**
    dash-chrome.js  dash-structure.js  dash-signal.js
    dash-chart.js   dash-alert.js  dash-ops.js
                      ※ 上面 7 个是**源**文件（按功能分开，维护用）；
                        页面实际只加载 **dashboard.bundle.js** 一个
                        （构建时拼接，见 scripts/build_dashboard_bundle.py）。
                        为什么不发 7 个请求：跨模块调用有 **88 处**，
                        拆成 7 个独立 IIFE 会**全部断掉**（实测
                        `startPolling is not defined`）。
tests/              扁平布局：117 个 test_*.py 直接放 tests/ 下，仅一个 fixtures/
                    人工构造案例；**没有** unit/ oracle/ e2e 子目录（见 §11）
docs/               38 份（rules.md / architecture.md / progress-log.md /
                    known-traps.md / web-api-reference.md / todo-r45-followups.md /
                    review-*-r45.md 各一份 / audit/ / archive/ …）
deploy/             nginx/  systemd/  cron/  env/  golden/  README.md
references/         czsc @ 701e480a（可选 extra `chan`）  wbt @ 39bb1e8a（仅可视化参考）
                    ⚠️ 「参照侧」= **czsc**，不是 references/ 里的某个独立实现
```

> `engine/` 已于 2026-09-25 整层删除（生产零导入），见 §2。
>
> ⚠️ **R45 更正（2026-10-04）**：
> - `llm/` **已落地**（R25，2026-10-02），8 文件 / 1,300 行，**计入上表**（§4）；
> - `engine/` 的删除说明仍然成立；
> - `dashboard/url_safety.js` **必须第一个 `defer` 加载** ——
>   它是凭据消毒的**唯一实现**，缺它应当响亮失败而不是静默回退裸 URL
>   （R45 把 5 个文件里的 9 处重复实现收敛到这一个文件，门禁②⓷ 锁住）。
>
> ### storage/ 的边界（R24，2026-10-01）
>
> 这一层**只装 CPT 自有表**（`public.cpt_*`）的读写。`emotion_core` 共 28 张表，
> **只有 2 张是 CPT 的**（`cpt_signal_event` / `cpt_dashboard_run`），其余 26 张
> （`daily_bar` 470MB、`derived_bar` 381MB、`asel.ref_adjust_factor` 515MB、
> `trade_calendar` …）是**跨项目共享的 A 股数据枢纽** —— CPT 是它的读者不是主人，
> 所以那些查询属于「接外部数据源」，仍归 `adapters/`，不搬进 storage。
>
> **store 层不 commit**：事务边界归调用方（与 R21 一致，便于多个写入共享一个事务）。
> 这个约定有代价但必须写下来 —— R23 就因为 `app.py::_persist_run` 漏了
> `conn.commit()` 导致数据被 `close()` 静默回滚：HTTP 全 200、日志零告警、表 0 行。
>
> ### 两条硬规矩
>
> 1. **SQL 只许出现在 `adapters/` 与 `storage/`。** import-linter 查依赖方向，
>    查不了职责归属（`conn` 只是 `Any` 形参，没有 `import psycopg`）。R24 实测
>    SQL 曾铺在 domain/application/adapters/web **四层**里而 3 条契约全绿。
>    由 `scripts/check_sql_layering.py` 做内容级门禁。
> 2. **`domain` 零 IO。** 2026-10-01 之前 `a_share_rules.py` 里有 3 处 SQL
>    （`derived_bar` / `trade_calendar`），已下沉到 `adapters.a_share_local`。
>
> **关于 ccxt**：`adapters/ccxt_source.py` 存在并由 `source_registry.py` 注册为兜底
> 行情源，但它**顶层不 import ccxt**（ccsc 自带的 `ccxt_connector.py` 才是顶层硬 import）。
> 所以「不用 ccxt」与「有 ccxt 兜底源」两句话都成立 —— 准确说法是：**ccxt 是可选
> 依赖，CI 环境不装，测试走 `importorskip`**。§10 把「多交易所 / ccxt」列为非目标，
> 指的是不做多交易所聚合与选股，不是否掉这个兜底源。

## 6. 数据模型（不可变）

领域对象全部 `@dataclass(frozen=True)`，一切变更走"事件追加 + `revision` 递增"。

**BarLike（协议）**：`open_time close_time high low direction`——K 线与结构元素的公共抽象。

**CanonicalBar**：`open_time open high low close volume close_time quote_volume trade_count taker_buy_base_volume taker_buy_quote_volume is_closed`（rules.md §8.5）。

**StructureState**（当前状态，rules.md §8.6）：
`id level kind direction start_time end_time status revision first_seen_at confirmed_at invalidated_at source_ids`
`kind ∈ {fractal, bi, zhongshu, trend_type, signal}`；`id` 用确定性生成（`level+kind+start_time+source` 哈希）。

**StructureEvent**：`event_type structure_id revision payload occurred_at`
`event_type ∈ {created, updated, confirmed, reclassified, invalidated, closed}`，只追加。

**StructureElement（递归单元，rules.md §8.3）**：
`direction start_time end_time start_price end_price high low center_ids source_structure_id source_revision status revision`——实现 `BarLike`，可直接喂回结构管线。

**Signal（一买，rules.md §8.6）**：
`signal_id level signal_type status structure_id center_ids divergence_status alert_time candidate_time confirmed_time invalidated_time price source_revision`
`status ∈ {structure_ready, alert, candidate, confirmed, invalidated}`

**RulesConfig（config.py，可序列化的规则口径子集）**：
包含关系方向口径、`fx_qy_middle`、`fx_qj_ck`、`bi_type_new`、`zs_wzgx` 档位、底层笔最少跨度 `min_bi_len`、高级别笔≥5元素、MACD(12,26,9)、级别链等，全部可序列化，写入每次计算结果的元数据。

> 注意两个「笔门槛」量纲不同、不可混用：`min_bi_len` 是**去包含后K线根数**（§9.8），`min_elements_for_higher_bi` 是**低级别结构元素数**（§9.7）。

## 7. 复用映射与反腐层

### 7.1 许可证与复用边界

| 仓库 | 固定版本 | 许可证 | 复用方式 |
|---|---|---|---|
| czsc | `701e480a` | Apache-2.0 | 可选依赖 extra（`chan`），经反腐层复用分型/笔/力度度量/一买谓词 |
| wbt | `39bb1e8a` | MIT | 仅作可视化与回测参考（R16 画布 D），不整体依赖 |

**已移除（2026-09-24，G1 决议）**：`chanlun-pro`、`chanlun.py`、`chanlun_pine`。前者的分型/笔实现与缠论定义冲突（笔端点中位跨度仅 2 根原始K线，66–74% 的笔跨度不足 4 根），后两者只提供借鉴价值且引入了不可核验的溯源负担。移除依据与实测数据见 `rules.md` §7.6 与 `progress-log.md`。

### 7.2 复用地图

```text
依赖级复用（czsc，经反腐层，可选 extra `chan`）
    缠论K线包含 / 三根分型 / 新笔判定 / 笔的力度度量(power_price, power_volume, length)
    一买一卖结构谓词（移植为 cpt/domain/first_buy.py，与上游逐笔数交叉验证一致）

借鉴逻辑（wbt）
    可视化报告与回测口径（R16 画布 D 参考）

CPT 自研（czsc 未覆盖）
    笔中枢（严格三笔重叠 + ≥3 笔 + 延伸不收缩）
    走势类型构成与分类 / 走势类型→高级别笔的递归映射 / 一买状态机
```

### 7.3 反腐层 `adapters/czsc_chanlun.py`

czsc 的对象**绝不泄漏进 domain**。反腐层做四件事：

1. 把 `CanonicalBar` 转成 czsc 的 `RawBar`（注意：**传原始K线**，czsc 内部自己做包含处理，先跑 CPT 的 `merge_contained_bars` 会让包含关系被处理两次）；
2. 由相邻 `open_time` 的**中位**间隔反推周期标签（用中位而非均值，避免停牌/断线缺口带偏）；
3. 调用 czsc 取回分型/新笔与力度度量，并把**时间反查回原始K线下标**（反查失败即响亮报错，不静默用错时间）；
4. 把结果映射回 CPT 的不可变领域对象。

中枢**不走** czsc：其 `zs_list` 会产出 <3 笔的假中枢且无笔级溯源，CPT 用自己的 `build_zhongshus`（§9.9）。

这样即便未来替换或移除 czsc，domain/engine 零改动。`reference_chanlun.py` 保留为**后端契约**（`ChanlunResult` / `FxRaw` / `BiRaw` / `ZsRaw` / `ReferenceChanlunConfig`）与 `InMemoryChanlunBackend`，是三个后端（native / czsc / 测试内存）共同的接口定义。

### 7.4 配置覆盖点

背驰口径由 CPT 自定义：MACD 柱面积法，参数固定 `(12,26,9)`（`rules.md` §9.6），不依赖外部实现的可覆盖钩子。

## 8. 已冻结约定（v0）

以下原"未冻结缝隙"已在本文档冻结，并在 `rules.md` §9 同步记录：

1. **高级别分型包含**：不做包含合并，只做过滤。结构元素由完整走势类型构成、时间相邻不重叠，包含合并既无原文依据、又会破坏 `source_structure_id` 追溯。
2. **级联重构**：确认即冻结，重构走新事件。候选结构就地更新（`revision+1`）；已确认结构绝不原地修改，失效就发 `invalidated` 事件。高层结构记录 `source_structure_id + source_revision`，低级别 `revision` 变化时扫描依赖滞后的高层结构——这是"无未来函数"的执行机制。
3. **开放终态**：增加显式终态 `open_end`（区别于 `closed_by_reversal`）。历史模式数据边界截断时标记，`open_end` 的走势类型不参与一买完成统计；实时模式无需特殊处理。
4. **术语**：走势类型状态 `candidate` 改名 `forming`，避免与一买信号状态 `candidate` 冲突。事件统一 `created/updated/confirmed/reclassified/invalidated/closed`。
5. **中枢位置关系档位**：首版固定 `zs_wzgx = zgd`（高点比 zg、低点比 zd），写入 `RulesConfig`，不单币配置。
6. **背驰力度口径**：复用 MACD 柱面积法（对应 `query_macd_ld`），参数固定 `(12,26,9)`，`divergence_status ∈ {not_checked, not_detected, detected}`。
7. **"≥5 结构元素"语义**：是 CPT 递归工程参数，量纲为**低级别结构元素数**，与 `min_bi_len`（去包含后K线根数，§9.8）不同，必须用人工构造案例验证。

## 9. 架构原则（CI 与工程约束）

1. 依赖方向用 import-linter 在 CI 卡死，不靠自觉。
2. domain 零第三方依赖、零 I/O、零随机、零系统时钟；时间一律由 engine 注入。
3. 领域对象不可变；结构 ID 确定性生成，测试断言与幂等重放都受益。
4. 事件只追加，状态更新递增 `revision`；历史最终结果不覆盖实时预警，反之亦然。
5. 每个 LLM 调用可审计、可关闭、有预算；LLM 输出永不回写结构。
6. 先离线回放 + JSON 导出，再接实时订阅与 Web 展示；UI 后置。
7. 每个阶段有可重复命令、测试与验收结果。

## 10. 非目标（首版不做）

```text
自动下单 / 实盘接入        线段、走势段、扩展线段
多交易所 / ccxt           复杂策略组合 / 选股
Web UI / 前端图表组件      LLM 参与结构判断或信号生成
```

## 11. 测试策略

**实际布局（R45 复核，2026-10-04）**：`tests/` 是**扁平**的，**104** 个
`test_*.py` 直接放在 `tests/` 下，唯一子目录是 `fixtures/`（**12** 个文件，
人工构造案例）。

> ⚠️ 此处原写「2026-10-01 实况：72 个 test_*.py / 11 个 fixtures」——
> **R45 新增了 32 个测试文件**（四轮复盘 + 门禁），而**只改了 §5 目录树里那处
> 计数，漏了本句散文**。这类「同一个数字在文档里出现两次、改了一处漏一处」
> 是计数漂移最常见的形态 —— 门禁只钉住了 §2.1 那张表。

```text
tests/
├── fixtures/            人工构造案例（JSON，含预期结构序列与事件）
└── test_*.py            72 个，按被测对象命名（test_signal.py / test_dashboard_*.py …）
```

> **本节早期版本列的 `tests/unit` / `tests/oracle` / `tests/e2e` 四个子目录从未建立**，
> 那是 v0.1 的规划树。查模块归属请直接看文件名，不要按那四个目录去找。

测试金字塔：人工构造 fixture 先行 → 领域层纯函数 → 应用层快照契约 → HTTP 路由
（`tests/conftest.py::served()` 提供共用的真 server 样板）→ 死代码与层级门禁。

与 czsc 的差异必须可解释并记录；czsc 不在 CI 环境，相关测试走 `importorskip`，
**不允许**用 mock 假装它在位。

**门禁**见 `README.md`「质量门」—— 6 条全部在 CI（3.12 + 3.14 双版本）执行。

---

## 附录 A：§2.1 状态表的**核实结果**（R45，2026-10-03）

那张表标着「2026-10-02 实测」，但**实际已过期**，且 §2.1 下方自己就写着
「别照抄审计里的『未接线』清单，先用 AST 复核」—— 这条纪律同样适用于它自己。

### A.1 文件数/行数：几乎每行都对不上（AST + `wc -l` 实测）

| 层 | 表里声称 | 实测 | 差 |
|---|---|---|---|
| `storage/` | 5 文件 / 1,070 行 | 6 / 1,408 | +1 / +338 |
| `llm/` | 7 / 1,092 | **8** / 1,240 | +1 / +148 |
| `adapters/` | 16 / 4,519 | **19** / 5,524 | +3 / +1,005 |
| `application/` | 29 / 4,182 | **32** / 5,170 | +3 / +988 |
| `domain/` | 16 / 2,894 | 16 / 2,938 | 0 / +44 |
| `web/` | 5 / 2,680 | 5 / 2,834 | 0 / +154 |

**原因不是 R44/R45 改的**（那两轮合计约 +300 行）。是表由 commit `7791a5b` 写下后，
**同一天**又落地了 `feishu.py`(10-02)、`run_metric.py` / `dashboard_parity.py` /
`parity_reference.py`(10-02) 等文件，表没跟着更新。「2026-10-02 实测」实际只对到
当天某个时间点 —— **这是个会误导人的标签**。

### A.2 「下一层做 adapters/」这个**建议本身仍然成立**

用 AST 扫外部系统触点（网络 / 进程 / DB）：

| 层 | 外部触点 | 占比 | 行数 |
|---|---|---|---|
| **`adapters/`** | **6 / 19 文件** | **31.6%** | 5,524 |
| `web/` | 1 / 5 | 20.0% | 2,834 |
| `llm/` | 1 / 8 | 12.5% | 1,240 |
| `application/` | 1 / 32 | 3.1% | 5,170 |
| `domain/` `storage/` | 0 | 0% | — |

`adapters/` 的外部触点密度**是第二名的 1.6 倍**，且触点数最多。§2.1 给的理由
（外部契约漂移不会让任何测试变红）经核实成立。**所以结论不变，只是依据要换成实测数。**

### A.3 分层依赖：AST 手写扫描**不可信**，以 import-linter 为准

手写 AST 扫出 `domain → adapters`、`domain → application`、`storage → application`
等**违反契约**的边。一开始以为真有架构腐化，**其实是假阳性** ——
匹配时把**注释和 docstring 里的 `cpt.domain.` 字样**也算成了 import
（与 `tests/test_dashboard_url_credentials.py` 踩的同一个坑）。

项目自带的权威判定：

```
$ .venv/bin/lint-imports
Analyzed 123 files, 559 dependencies.
Domain has no third-party deps          KEPT
Adapters do not leak into domain        KEPT
Storage does not leak into domain       KEPT
LLM does not leak into domain           KEPT
LLM does not leak into storage          KEPT
Layered architecture                    KEPT
Contracts: 6 kept, 0 broken.
```

⇒ **分层依赖是干净的。** 手写 AST 扫描只适合做「量规模」这种粗活，
**判断依赖是否合法一律用 `lint-imports`**。

### A.4 R44/R45 对状态表的影响

本轮改过的层内文件：`cpt/adapters/a_share_local.py`（仅模块 docstring 去快照化）、
`dashboard/{dashboard,market_a_share}.js`（凭据 URL bug）。

按 §2.1 自己的定义（「**不是被单点改动顺手碰过**」），这两个层的 ⚠️ 状态**不变**。
`scripts/` 与 `deploy/` 不属于任何一层。

### A.5 建议的修法（未执行）

§2.1 的表要么按实测数更新，要么把「2026-10-02 实测」改成「截至 `7791a5b`」，
**并加一行说明它需要复核** —— 否则下一个接手的人会像这次一样先信它、再被它坑一次。
