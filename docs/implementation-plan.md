# CPT 实施计划

版本：v0.1 · 2026-09-22
状态：替代旧 `plan.md`（已删除）。架构见 `docs/architecture.md`，规则口径见 `docs/rules.md`。

## 1. 总体原则

1. **垂直切片，不做水平推进**：每个里程碑都是一条可演示的端到端窄链路，不等全部模块做完才看到结果。学习项目靠正反馈活着。
2. **规则先行**：动手写某层代码前，`docs/rules.md` 对应口径已冻结。
3. **测试与代码同阶段交付**：人工构造 fixture 先行，oracle 对照随后，复现性校验兜底。
4. **LLM 层独立成线**：M-LLM 与主线解耦，任何时候不做都不影响主线。

## 2. 里程碑总览

| 里程碑 | 内容 | 关键验收 |
|---|---|---|
| M0 | 基线补齐与工具链 | references 落盘可复现；审计文档落盘；CI 依赖规则生效 |
| M1 | 单级别全链路（复用层） | 5m 数据→分型→笔→中枢→JSON 导出，人工案例通过 |
| M2 | oracle 对照 | 与 chanlun-pro 基础结构序列 diff 可解释 |
| M3 | 走势类型 + 递归 + 一买（自研核心） | 两级递归贯通；一买状态机人工案例通过 |
| M4 | 数据与回放 | Binance 回填 + 缺口检测 + 回放 CLI |
| M5 | 实时预警 | 收盘确认升级；重构事件可追溯 |
| M-LLM | 独立 LLM 服务层（可在 M2 后任意点插入） | 三用例跑通；可关闭、可审计、有预算 |
| M6 | 质量验收 | 测试报告、差异报告、已知限制清单 |

## 3. M0：基线补齐与工具链

**目标**：把"可复现"三个字落到实处，再写一行业务代码。

任务：
- `scripts/fetch_references.sh`：clone 三个参考仓库并 checkout 到固定 commit（chanlun-pro `78ffa470` / chanlun.py `2e4fa135` / chanlun_pine `0c028ef`），输出版本与 LICENSE 校验信息。
- `docs/reference-audit.md`：落盘三仓库许可证（Apache-2.0 / MIT+NOTICE / GPL-3.0）、复用边界、加密核心风险、配置覆盖点（`query_macd_ld`/`compare_ld_beichi`/`user_custom_mmd`）。
- 冻结 `docs/rules.md` §9 七条约定（已随本次架构文档完成）。
- 工具链：ruff + mypy + pytest + pre-commit；import-linter 配置依赖方向契约。
- **验证 PyPI 旧 MIT 版 `chanlun` 包**（约半小时）：确认其源码覆盖范围（分型/新笔/笔中枢/走势类型）与协议（MIT），评估是否可作为比加密 pro 更干净的 oracle。结论记入 `docs/reference-audit.md`。

验收：`fetch_references.sh` 在干净环境一键复现 references；`import-linter` 对空骨架生效；`pytest` 可运行。

## 4. M1：单级别全链路（复用层）

**目标**：打通 `5m 数据 → 包含 → 分型 → 新笔 → 笔中枢 → JSON 导出`，先喂 chanlun-pro 的结果，让反腐层和存储先立起来。

任务：
- `domain/types.py`（`BarLike` 协议）、`models.py`（不可变领域对象）、`config.py`（`RulesConfig`，含 `zs_wzgx=zgd`、MACD(12,26,9)、≥5元素）。
- `adapters/reference_chanlun.py` 反腐层：CanonicalBar → chanlun-pro 输入 → 领域对象。
- `storage/`：SQLite 三层模型（状态 + 事件 + 信号）+ repository。
- `application/export.py`：JSON 导出，**schema v1 此刻冻结并版本化**（回放/评测/未来 UI 三方契约）。
- 人工构造案例 5–8 个（JSON fixture，含预期结构序列与事件）。

验收：人工案例全过；`python -m cpt.application.replay --input fixture.json --export out.json` 可重复；同输入导出哈希一致。

## 5. M2：oracle 对照

**目标**：把"复用"转化成"免费验证器"。

任务：
- `tests/oracle/`：同一份输入，CPT 管线 vs chanlun-pro 直接输出，diff 分型/新笔/笔中枢基础序列。
- 若 M0 验证通过，切换/新增 MIT 版 `chanlun` 包为 oracle，对比加密 pro 的可审计性收益。
- 差异报告模板：每条 diff 必须能归因（配置口径 / 边界处理 / 真 bug）。

验收：连续 3 个真实数据切片（各 ≥1000 根 5m K线）基础结构序列 diff 全部可解释并记录。

## 6. M3：走势类型 + 递归 + 一买（自研核心）

**目标**：CPT 真正自研的三个模块，两级递归贯通。

任务：
- `domain/trend_type.py`：盘整/趋势构成与完成分类（rules.md §8.1），状态含 `forming/consolidation/trend/extended/reclassified/closed/open_end`。
- `domain/recursion.py`：走势类型 → `StructureElement`（实现 `BarLike`），喂回同一条管线；先贯通 5m→30m 一级。
- `engine/rebuild.py`：尾部弹出 + 回溯重算 + 级联事件（借鉴 chanlun.py `_弹出旧笔` 系列）；级联重构按已冻结约定 2（确认即冻结、重构走新事件、`source_revision` 依赖扫描）。
- `domain/signal.py`：一买状态机（`structure_ready/alert/candidate/confirmed/invalidated`），背驰段硬条件 + 背驰成立非硬门槛，`divergence_status` 三态。
- 人工构造案例补充：开放终态 `open_end`、级联重构、`forming` 与信号 `candidate` 的术语边界各 ≥2 例。

验收：5m→30m 递归在人工案例与 1 个真实切片上结构/事件可追溯；一买状态机人工案例全过；低级别 `revision` 变化时依赖滞后的高层结构被正确标记。

## 7. M4：数据与回放

**目标**：真实数据可复现回放。

任务：
- `adapters/binance_futures.py`：`GET /fapi/v1/klines` → `CanonicalBar`（httpx，窄接口）。
- `adapters/validators.py`：去重、缺口检测、连续性检查；缺口不填充并阻断跨缺口正式结构。
- `application/replay.py`：批量回放 + 单根推进；历史/实时模式共享 domain 算法。
- 断点续传：本地原始数据与标准化数据分层保存，记录数据版本与来源。

验收：BTCUSDT 永续 5m 指定区间回填可复现；含已知缺口的区间被正确阻断并记录；回放导出与 M1 schema v1 兼容。

## 8. M5：实时预警

**目标**：盘中预警、收盘升级，不输出交易指令。

任务：
- `engine/realtime.py`：增量输入、候选维护、收盘升级；未收盘K线只触发预警。
- 收盘后小窗口全量重建（借鉴 chanlun_pine `needFullRebuild` 策略：每根收盘 bar O(n) 重建，限制窗口规模，历史加载期不重建避免 O(n²)）。增量优化留到性能真成问题时再做。
- 实时输出与历史最终结果分层保存；预警、候选、确认、失效时间全程可追溯。

验收：模拟实时流与事后批量回放的结构结果一致；未确认结构重构产生完整事件链；无未来函数（t 时刻输出只依赖 ≤t 数据）有专项测试。

## 9. M-LLM：独立 LLM 服务层

**插入时机**：M2 之后任意点；不阻塞主线。

任务：
- `llm/` 骨架：`LLMClient` Protocol + `LLMResult` + registry 工厂 + OpenAI 兼容 provider（首版唯一实现，不引入 LangChain 等重框架）。
- `prompts.py`：三个用例的提示词模板，集中管理可版本化。
- `cache.py` + `budget.py`：请求 hash 缓存、每日/每次预算与速率限制；`llm.enabled=false` 整层短路。
- `application/llm_cases.py`：规则解释 / 差异摘要 / 标注辅助三用例；标注建议落盘前经确定性校验 + 人工确认。
- `llm_call` 表：prompt 摘要、模型、token、费用、响应 hash 全量落盘。

验收：三用例各跑通 ≥3 个真实案例；关闭开关后主流程全部测试仍通过；`import-linter` 证明 domain/engine/storage 不依赖 `llm/`；每次调用审计记录完整。

## 10. M6：质量验收

验收清单（逐项过）：
- 人工案例逐条通过；批量历史回放结果可复现（同输入同输出哈希）。
- 实时模式与离线回放结果一致；信号出现/确认/撤销时间可追溯。
- 结构重构不产生隐性未来函数；与 chanlun-pro 和 Pine 的差异可解释并记录。
- 数据缺口、重复K线、未收盘K线有明确处理。
- 交付：测试报告、差异报告、已知限制清单。

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| chanlun-pro 加密核心行为与文档不符 | M0 验证 MIT 版 `chanlun` 包作备选 oracle；差异报告强制归因 |
| 递归/走势类型规则在边界数据上发散 | 人工构造案例先行；`open_end` 显式终态；差异可解释才准合并 |
| 范围蔓延（线段/Web/多交易所） | 架构文档 §10 非目标清单；每个里程碑只做验收清单内的事 |
| LLM 成本失控 | budget 硬上限 + 缓存 + 可关闭开关；审计表每周 review |

## 12. 当前下一步

M0：先跑 `fetch_references.sh` 固化基线 → 落盘 `docs/reference-audit.md`（含 PyPI MIT 版 `chanlun` 验证结论）→ 配好 import-linter，然后进入 M1。
