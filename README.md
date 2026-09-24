# CPT — Chan Pattern Trader

一个以**学习金融与软件工程**为目标的缠论分析系统，覆盖**加密永续**与**A 股日线**两个市场。

CPT 基于缠中说禅理论做结构分析：缠论K线 → 分型 → 新笔 → 笔中枢 → 走势类型，并由低级别走势类型递归生成高级别笔，最终输出一买信号（预警，不下单）。

## 设计原则

1. **结构清晰**：单一职责分层，依赖方向由 import-linter 强制（4 条契约）。
2. **虚拟/物理分离**：缠论算法（domain/engine）不知道交易所、数据库、绘图、LLM 的存在。
3. **规则先行**：所有规则口径先冻结于 `docs/rules.md`，再写代码；配置集中在可序列化的 `RulesConfig`。
4. **无未来函数**：实时输出只依赖当前时点之前的数据，候选/确认/失效全程可追溯。
5. **可复现**：同一份输入数据 + 同一份配置，必须得到同一份结构结果。
6. **高复用率**：基础口径（分型/新笔/力度度量/一买谓词）经反腐层委托给固定版本的 czsc；CPT 自研 czsc 未覆盖的核心（笔中枢、走势类型、递归映射、一买状态机）。
7. **失败必须分类**：降级/空数据要写明**为什么**（`no_factor` / `no_data` / `db_error:*` / `invalid_code`），不静默成"看起来正常但没数据"。

## 架构速览

```text
application/   用例编排（回放 / 查询 / 导出 / 快照构造 / LLM 用例）
engine/        有状态编排（历史模式 / 实时模式 / 重构 / 事件）
domain/        纯算法与领域模型（零第三方依赖，一套算法跨级别复用）
adapters/      外部数据接入（Binance / ccxt / Wind / 腾讯 / 新浪 / 本地 PG）+ 缠论后端（czsc / native / 契约 reference_chanlun）
storage/       当前状态 + 不可变事件 + 信号（SQLite + JSON）
web/           只读 HTTP adapter + 两个入口（加密 / A股）
llm/           独立 LLM 服务层（可关闭、可审计、永不回写结构）
```

详细设计见 `docs/architecture.md`。

## 两个市场

| | 加密永续 | A 股日线 |
|---|---|---|
| 入口 | `python -m cpt.web` | 主看板内切「A股」，或 `python -m cpt.web.a_share --code 600519` |
| 行情源 | Binance USDT-M（主）/ ccxt（兜底） | 本地 PG（主）/ Wind（主，耗额度）/ 腾讯（兜底）/ 新浪（快照） |
| 周期 | 1m–1w，实时轮询 | 固定 `1d`，**不轮询**（收盘后不再变） |
| 复权 | 不适用 | `daily_bar`（不复权原始价）× `asel.ref_adjust_factor` → 后复权 |

**两个市场共用同一套缠论后端与同一份 `dashboard.v2` 快照 schema** —— 所以四个画布对 A 股**零改动复用**（由 `tests/test_dashboard_ashare_contract.py` 钉住：`canvas_b.js`/`canvas_c.js` 里不得出现市场分支）。

### A 股复权因子：按需获取

全库 5,225 只里本地只有约 100 只有复权因子（全市场 backfill 未执行）。因此做成了**按需**：输入代码 → 本地因子不足则从腾讯拉 `raw + hfq` 两个序列（同源同对，`hfq_factor = hfq/raw` 绝对自洽）→ 幂等落库 → 重出快照。

> 腾讯是否提供某标的的 `hfqday` 是**逐标的**属性、**无法用代码前缀预测**（实测 `688111`/`688036` 有而 `688981` 没有；多数 `301` 有而近期新股没有）。所以实现里**没有任何板块判断**，只如实报告腾讯实际返回了什么。教训见 `docs/progress-log.md` R15-1 节（同一处先后记错过两次）。

## 文档导航

| 文档 | 内容 |
|---|---|
| `docs/rules.md` | 规则口径唯一事实来源（含 §9 已冻结约定） |
| `docs/architecture.md` | 软件结构设计（分层、模块、数据模型、复用映射、测试策略） |
| `docs/implementation-plan.md` | 实施计划（M0–M6 垂直切片里程碑 + M-LLM 独立线） |
| `docs/reference-audit.md` | 参考仓库许可证与复用边界 |
| `docs/progress-log.md` | 逐轮进度日志（R13 起；更早见 `docs/archive/`） |
| `deploy/README.md` | Nginx / systemd 上线说明 |

## 参考仓库

| 仓库 | 固定版本 | 许可证 | 用途 |
|---|---|---|---|
| [czsc](https://github.com/waditu/czsc) | `701e480a` | Apache-2.0 | 可选依赖 extra `chan`，复用分型/新笔/力度度量/一买谓词 |
| [wbt](https://github.com/zengbin93/wbt) | `39bb1e8a` | MIT | 可视化与回测参考（画布 D） |

> `chanlun-pro` / `chanlun.py` / `chanlun_pine` 已于 2026-09-24 移除：前者的分型/笔
> 实现与缠论定义冲突（笔端点中位跨度仅 2 根原始K线，66–74% 的笔跨度不足 4 根），
> 后两者只提供借鉴价值却带来不可核验的溯源负担。依据见 `docs/rules.md` §7.6。

固化脚本：`scripts/fetch_references.sh`。

## 当前状态

核心算法、只读 Dashboard（四画布）、研究服务、HTTP adapter、**加密实时行情**、**A 股日线链路**均已实现并上线（`cpt-dashboard.service`，`--mode realtime`）。API 只读，不提供自动下单、撤单、账户、持仓或订单簿接口。

## 本地启动

### 加密看板

```bash
cp deploy/env/cpt-dashboard.env.example deploy/env/cpt-dashboard.env
.venv/bin/python -m cpt.web --host 127.0.0.1 --port 8000 --mode realtime
```

`--mode` 三档：`demo`（内置样例）/ `fixture`（确定性离线，UI 冒烟用）/ `realtime`（Binance 轮询）。
`--backend auto|czsc|native` 选缠论后端（`auto` = 装了 czsc 就用 czsc）。

### A 股

主看板切「A股」标签即可（顶栏市场切换 + 代码下拉），或独立起一个服务：

```bash
.venv/bin/python -m cpt.web.a_share --code 600519 --port 8011
```

### 看板 URL 约定

单页、单快照，渲染全部交给画布分发器。URL 参数可分享、可审计：

| 参数 | 作用 |
|---|---|
| `?canvas=A\|B\|C\|D` | 选画布（A 原生 / B lightweight-charts / C plotly / D wbt 服务端报告） |
| `?market=a_share&code=600519` | 切 A 股并指定代码 |
| `?snapshot=<url>` | 指向固定快照（离线审计用；优先级高于 `data-snapshot-url`） |
| `?mode=watch\|research` | 视图模式 |

Dashboard 静态文件位于 `dashboard/`，Nginx/systemd 模板见 `deploy/README.md`。

## 质量门

CI（`.github/workflows/ci.yml`）逐条执行：

```bash
pytest tests -rsq
ruff check cpt tests
ruff format --check cpt tests
mypy cpt
vulture cpt --min-confidence 80 --exclude 'cpt/web/app.py'
```

本地另跑两条（CI 未覆盖，但同样视为门禁）：

```bash
ruff format --check cpt tests scripts      # 比 CI 多覆盖 scripts/
import-linter lint --config .importlinter  # 4 条分层契约（pre-commit 里也会跑）
```

CI 只装 `requirements-dev.txt`，因此 czsc/ccxt/psycopg/pandas/plotly/wbt 均不在
CI 环境里 —— 依赖它们的测试一律走 `pytest.importorskip`，**不允许**用 mock 假装
它们在位。

首版非目标：自动下单、多交易所、LLM 参与结构判断。

## 风险声明

本项目仅用于个人学习与技术研究，不构成任何投资建议。金融市场存在风险，缠论信号（含一买）的历史表现不代表未来收益；任何实盘决策请独立判断并自行承担风险与责任。
