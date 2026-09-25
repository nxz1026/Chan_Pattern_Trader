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
│                   dashboard / a_share_snapshot               │
├─────────────────────────────────────────────────────────────┤
│ adapters/         外部接入：Binance / ccxt / Wind / 腾讯 /     │
│                   本地 A 股库 / 三种缠论实现                    │
├─────────────────────────────────────────────────────────────┤
│ domain/           纯算法与领域模型（零第三方依赖，全部纯函数）    │
└─────────────────────────────────────────────────────────────┘
```

> **这张图在 2026-09-25 重画过。** 原图多画了 `engine/`、`storage/`、`llm/` 三层：
> 前两层实际只落地过 `engine/realtime.py`、`engine/rebuild.py`、`storage/models.py`、
> `storage/repository.py` 四个文件且**生产代码零导入**（生产实时路径由
> `web/__main__.py` 的 `_RealtimeProvider` 自包含承担，历史回放走
> `application/replay.py`，全程无本地持久化），已按审核 P0-2 整层删除；`llm/`
> 则从未有过代码（空的 `cpt/llm/` 占位包更早按审计 A1 删除）。
> 当时的处置是**只在图下加"这是预留蓝图"的说明、没有重画图** —— 于是文档继续画着
> 不存在的层，读者（和后来的审核）会以为它们还在。历史记录见 §3.2 / §3.4。
>
> 若未来确实需要持久化（如信号落库），按 `domain/signal.py` 中 `signal_id` 作为
> **稳定 upsert 主键**的设计重新实现，不要照搬旧的 SQLite 三层模型。若未来落地
> LLM 用例（规则解释 / 差异摘要 / 标注辅助），在 `application/` 里编排，LLM 服务层
> 只放无业务规则的独立实现，且不得被 `domain` / `adapters` 导入。

依赖方向（`.importlinter` 的 `layers` 契约强制）：

```text
web         → application / adapters / domain
application → adapters / domain
adapters    → domain
domain      → （不导入任何上层）
domain 不导入 pandas / httpx / ccxt / fastapi / sqlalchemy / torch / openai / 绘图库
```

> 契约是**自上而下**写的（`layers` 第一条是**最高**层），高层可以导入**任意**低层
> （`web` 直接用 `domain` 是允许的），反向不行。实测依赖图：
> `domain → []`、`adapters → [domain]`、`application → [adapters, domain]`、
> `web → [adapters, application, domain]`。

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

### 3.4 storage/ — 持久化（**已整层删除，2026-09-25**）

原规划 SQLite 三层模型（**当前状态 + 不可变事件 + 信号**），实际只落地过 `models.py`
与 `repository.py` 两个文件，生产代码零导入，已按审核 P0-2 整层删除。原内容不再保留
（同上：描述的是没做完的蓝图，不是历史实现）。

未来若确实需要持久化（如信号落库），按 `domain/signal.py` 里 `signal_id` 作为**稳定
upsert 主键**的设计重新实现，不要照搬旧的 SQLite 三层模型。

### 3.5 application/ — 用例编排

只做参数解析、调用 domain / adapters、输出。不含业务规则。

- `replay.py`：历史回放（批量/单根推进）
- `inspect.py`：查询结构与信号（CLI）
- `export.py`：JSON 导出（schema 版本化）
- `llm_cases.py`：LLM 用例（规则解释、差异摘要、标注辅助）——编排 llm 层，不含提示词

## 4. 独立 LLM 服务层

> ⚠️ **本节是未实现的蓝图，不是现状。** `cpt/llm/` **从未有过代码**（空的占位包更早
> 按审计 A1 删除），下面写的模块与接口都还不存在。保留本节只是为了记录设计意图；
> 读到时请按"计划"而不是"已有"理解。当前 `cpt/` 里**没有任何** LLM 调用代码。

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

**按 2026-09-25 的仓库实况重写**（原树写的是 `src/cpt/`，而实际根目录直接是 `cpt/`；
且列着 `engine/`、`storage/`、`llm/` 三个不存在的包与 `tests/unit|oracle|e2e` 四个
不存在的子目录 —— 那是 v0.1 的规划树，不是实况）：

```text
cpt/
├── domain/         纯算法与领域模型（14 个模块，零第三方依赖）
│   types.py  models.py  config.py  chan_bar.py  contain.py
│   fractal.py  bi.py  zhongshu.py  trend_type.py  recursion.py
│   signal.py  ...
├── adapters/       外部接入（16 个模块）
│   binance_futures.py  ccxt_source.py  wind_source.py  a_share_public.py
│   a_share_local.py  a_share_pool.py  a_share_factor.py  strategy_signal.py
│   validators.py  reference_chanlun.py  native_chanlun.py  czsc_chanlun.py
│   _dbconfig.py  ...
├── application/    用例编排（29 个模块）
│   replay.py  inspect.py  export.py  dashboard.py  multi_level.py
│   a_share_snapshot.py  a_share_rules.py  canvas_*.py  ...
└── web/            HTTP 入口（5 个模块）
    __main__.py  app.py  a_share.py  a_share_routes.py  __init__.py

scripts/            运维入口（不在包内，但已在 CI 门禁覆盖范围内）
    factor_backfill.py
dashboard/          前端静态产物（Nginx 从 /var/www/cpt-dashboard 提供，非包内）
tests/              扁平布局：test_*.py 直接放 tests/ 下（71 个）
    fixtures/oracle/
docs/               rules.md  architecture.md  implementation-plan.md  progress-log.md
                    pending-wiring.md  duplication-triage.md  export-schema-v1.md  audit/
deploy/             nginx/  systemd/  env/  README.md
references/         czsc @ 701e480a（可选 extra `chan`）  wbt @ 39bb1e8a（仅可视化参考）
```

> 三个已删除的层（`engine/` / `storage/` / `llm/`）与它们各自的规划树**不再列出** ——
> 见 §2 的层说明与 §3.2 / §3.4。

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

```text
tests/fixtures/   人工构造案例（JSON，含预期结构序列与事件）
tests/unit/       domain 各 stage 纯函数 + engine 状态机
tests/oracle/     czsc 对照：同输入 diff 分型/新笔/力度度量/一买谓词
tests/e2e/        同输入必同输出的哈希校验（复现性）
```

测试金字塔：人工构造 fixture 先行（实施计划 M0 交付物直接 fixture 化）→ 引擎回放集成测 → oracle 对照测 → 端到端复现性校验。与 czsc 的差异必须可解释并记录。
