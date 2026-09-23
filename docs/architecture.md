# CPT 软件结构设计

版本：v0.1 · 2026-09-22
状态：替代旧文档 `docs/design-review.md` 与 `docs/architecture-reference-audit.md`（已删除）；规则口径以 `docs/rules.md` 为准，实施节奏见 `docs/implementation-plan.md`。

## 1. 设计目标

1. **结构清晰**：单一职责分层，依赖方向在 CI 中强制，不靠自觉。
2. **虚拟/物理分层清晰**：缠论算法（虚拟）不知道交易所、数据库、绘图、LLM 的存在；物理层（交易所/存储/大模型）通过窄接口适配接入。
3. **各 API 干好自己的活儿**：每个模块一个窄接口，输入输出明确，不做"顺手"的事。
4. **避免代码屎山**：一套结构算法对 `BarLike` 泛化，跨级别复用；配置集中；领域对象不可变。
5. **高复用率**：基础口径（分型/新笔/笔中枢/`level`/`zs_wzgx`）经反腐层委托给固定版 `chanlun-pro`；CPT 只自研走势类型、递归映射、一买状态机这三个参考仓库都未覆盖的核心。

## 2. 分层总览

```text
┌─────────────────────────────────────────────────────────────┐
│ application/      用例编排：replay / inspect / export / llm 用例 │
├─────────────────────────────────────────────────────────────┤
│ engine/           有状态编排：历史模式 / 实时模式 / 重构 / 事件  │
├─────────────────────────────────────────────────────────────┤
│ domain/           纯算法与领域模型（零第三方依赖，全部纯函数）    │
├──────────────────┬──────────────────┬───────────────────────┤
│ adapters/        │ storage/         │ llm/                  │
│ 外部数据接入      │ 状态+事件持久化    │ 独立 LLM 服务层        │
└──────────────────┴──────────────────┴───────────────────────┘
```

> **LLM 层说明（死代码审计 A1 处置，2026-09）**：上图的 `llm/` 层是**预留蓝图**，
> 当前**没有对应代码**——空的 `cpt/llm/` 占位包已按审计建议删除（见
> `cpt-feature-review-and-deadcode-audit.md` A1）。若未来落地 LLM 用例
> （规则解释 / 差异摘要 / 标注辅助），按本图第 3.1 节规划在 `application/llm_cases.py`
> 编排、`llm/` 只放无业务规则的独立服务层，且遵守下方依赖方向（llm 不导入
> engine / adapters / storage）。

依赖方向（import-linter 强制）：

```text
application → engine → domain
application → adapters / storage / llm
adapters → domain          # 产出领域模型
llm 不导入 engine / adapters / storage
storage 不导入 engine / llm
domain 不导入 pandas / httpx / ccxt / fastapi / sqlalchemy / torch / openai / 绘图库
```

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

### 3.2 engine/ — 有状态编排

| 模块 | 职责 |
|---|---|
| `historical.py` | 历史模式：批量输入、事后分类、补 `closed` 事件、`open_end` 边界处理 |
| `realtime.py` | 实时模式：增量输入、候选维护、收盘升级、收盘后小窗口全量重建 |
| `rebuild.py` | 尾部失效结构弹出 + 回溯重算 + 级联事件（借鉴 chanlun.py，见 §8） |
| `events.py` | 事件追加、时间戳来源（历史用 bar 时间、实时用 wall clock）、配置快照 |
| `store.py` | 应用事件维护"当前状态"投影，是 storage 的唯一写入方 |

历史与实时**共享同一套 domain 算法**，区别只在输入边界（批量 vs 逐根）与事件策略（事后回填 vs 实时追加）。

### 3.3 adapters/ — 外部数据接入（物理层）

窄接口，一个适配器一件事：

```python
def fetch_klines(symbol, interval, start, end) -> list[CanonicalBar]
```

- `binance_futures.py`：Binance USDⓈ-M 永续 K 线，产出 `CanonicalBar`（字段见 rules.md §8.5）。
- `validators.py`：去重、缺口检测、连续性检查（独立于适配器，可单测）。发现缺口不自动填充，并阻止跨缺口生成正式结构。
- `reference_chanlun.py`：**反腐层**（见 §7）。不用 ccxt——抽象泄漏且重，httpx + 窄接口足够。

### 3.4 storage/ — 持久化（物理层）

- `models.py`：SQLite 表结构（原始K线 / 标准化K线 / 结构当前状态 / 结构事件 / 信号 / LLM 调用记录）。
- `repository.py`：读写接口，不导入 engine。首版 SQLite + JSON 导出，不引入 ORM。
- 三层模型：**当前状态 + 不可变事件 + 信号**。事件只追加；状态更新递增 `revision`。

### 3.5 application/ — 用例编排

只做参数解析、调用 engine/adapters/llm、输出。不含业务规则。

- `replay.py`：历史回放（批量/单根推进）
- `inspect.py`：查询结构与信号（CLI）
- `export.py`：JSON 导出（schema 版本化）
- `llm_cases.py`：LLM 用例（规则解释、差异摘要、标注辅助）——编排 llm 层，不含提示词

## 4. 独立 LLM 服务层

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
| 差异摘要 | CPT 与 chanlun-pro/Pine 的对照 diff | 差异原因说明 | 否 |
| 标注辅助 | 结构序列片段 | 标注建议 | 建议，须校验+人工确认 |

### 4.4 不做的事

不让 LLM 判断分型/笔/中枢/买卖点；不把 LLM 输出当作信号；不在 engine/domain 里调用 LLM；不引入 LangChain 等重型框架（一个 Protocol + 一个 OpenAI 兼容实现足够，学习价值更高）。

## 5. 目录结构

```text
src/cpt/
├── domain/
│   ├── types.py  models.py  config.py
│   ├── chan_bar.py  fractal.py  bi.py  zhongshu.py
│   ├── trend_type.py  recursion.py  signal.py
├── engine/
│   ├── historical.py  realtime.py  rebuild.py  events.py  store.py
├── adapters/
│   ├── binance_futures.py  validators.py  reference_chanlun.py
├── storage/
│   ├── models.py  repository.py
├── llm/
│   ├── config.py  base.py  registry.py  prompts.py  cache.py  budget.py
│   └── providers/openai_compatible.py
└── application/
    ├── replay.py  inspect.py  export.py  llm_cases.py
tests/
    fixtures/   unit/  oracle/  e2e/
docs/
    rules.md  architecture.md  implementation-plan.md  reference-audit.md(待补)
references/
    chanlun-pro @ 78ffa470   chanlun.py @ 2e4fa135   chanlun_pine @ 0c028ef
```

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

**RulesConfig（config.py，对标 chanlun.py 60+ 字段配置的可序列化子集）**：
包含关系方向口径、`fx_qy_middle`、`fx_qj_ck`、`bi_type_new`、`zs_wzgx` 档位、高级别笔≥5元素、MACD(12,26,9)、级别链等，全部可序列化，写入每次计算结果的元数据。

## 7. 复用映射与反腐层

### 7.1 许可证与复用边界

| 仓库 | 固定版本 | 许可证 | 复用方式 |
|---|---|---|---|
| chanlun-pro | `78ffa470` | Apache-2.0 | 依赖级复用（经反腐层），保留版权声明 |
| chanlun.py | `2e4fa135` | MIT（NOTICE：Signal/Factor/Event 源自 czsc，Apache-2.0） | 借鉴逻辑，不整体依赖 |
| chanlun_pine | `0c028ef` | GPL-3.0（有传染性） | 仅视觉交叉校验，一行代码都不抄 |

### 7.2 复用地图

```text
依赖级复用（chanlun-pro，经反腐层）
    缠论K线包含 / 三根分型(fx_qy_middle, fx_qj_ck) / 新笔(bi_type_new)
    笔中枢三笔重叠 / 中枢 level / zs_wzgx / 背驰字段与一买标签语义

借鉴逻辑（chanlun.py / chanlun_pine）
    chanlun.py：弹出式级联重建（_弹出旧笔/_弹出线段/_从中枢序列尾部弹出）、
                配置序列化（to_dict/to_json/保存/加载/对比）
    chanlun_pine：信号生命周期状态机、收盘后小窗口全量重建的工程策略

CPT 自研（两个参考仓库都未覆盖）
    走势类型构成与分类 / 走势类型→高级别笔的递归映射 / 一买状态机
```

### 7.3 反腐层 `adapters/reference_chanlun.py`

chanlun-pro 的对象**绝不泄漏进 domain**。反腐层做三件事：

1. 把 `CanonicalBar` 转成 chanlun-pro 的输入 DataFrame；
2. 调用其公开接口，取回缠论K线/分型/新笔/笔中枢/`level`/`zs_wzgx`；
3. 把结果映射回 CPT 的不可变领域对象。

这样即便未来替换或移除 chanlun-pro，domain/engine 零改动。`user_custom_mmd` 扩展点可用于把 CPT 一买挂到 chanlun-pro 内部做对照验证。

### 7.4 配置覆盖点（已核实）

`cl_interface.py` 中背驰力度 `query_macd_ld` 与背驰比较 `compare_ld_beichi` 设计为**可覆盖**，因此背驰口径可由 CPT 自定义注入，无需破解加密核心 `cl.py`。

## 8. 已冻结约定（v0）

以下原"未冻结缝隙"已在本文档冻结，并在 `rules.md` §9 同步记录：

1. **高级别分型包含**：不做包含合并，只做过滤。结构元素由完整走势类型构成、时间相邻不重叠，包含合并既无原文依据、又会破坏 `source_structure_id` 追溯。
2. **级联重构**：确认即冻结，重构走新事件。候选结构就地更新（`revision+1`）；已确认结构绝不原地修改，失效就发 `invalidated` 事件。高层结构记录 `source_structure_id + source_revision`，低级别 `revision` 变化时扫描依赖滞后的高层结构——这是"无未来函数"的执行机制。
3. **开放终态**：增加显式终态 `open_end`（区别于 `closed_by_reversal`）。历史模式数据边界截断时标记，`open_end` 的走势类型不参与一买完成统计；实时模式无需特殊处理。
4. **术语**：走势类型状态 `candidate` 改名 `forming`，避免与一买信号状态 `candidate` 冲突。事件统一 `created/updated/confirmed/reclassified/invalidated/closed`。
5. **中枢位置关系档位**：首版固定 `zs_wzgx = zgd`（高点比 zg、低点比 zd），写入 `RulesConfig`，不单币配置。
6. **背驰力度口径**：复用 MACD 柱面积法（对应 `query_macd_ld`），参数固定 `(12,26,9)`，`divergence_status ∈ {not_checked, not_detected, detected}`。
7. **"≥5 结构元素"语义**：是 CPT 递归工程参数，为 chanlun-pro 新笔"至少5根K线"的**类比映射**而非数值等价，必须用人工构造案例验证。

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
tests/oracle/     chanlun-pro 对照：同输入 diff 分型/新笔/笔中枢基础序列
tests/e2e/        同输入必同输出的哈希校验（复现性）
```

测试金字塔：人工构造 fixture 先行（实施计划 M0 交付物直接 fixture 化）→ 引擎回放集成测 → oracle 对照测 → 端到端复现性校验。与 chanlun-pro/Pine 的差异必须可解释并记录。
