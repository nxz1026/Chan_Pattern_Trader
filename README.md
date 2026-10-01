# CPT — Chan Pattern Trader

一个以**学习金融与软件工程**为目标的缠论分析系统，覆盖**加密永续**与**A 股日线**两个市场。

CPT 基于缠中说禅理论做结构分析：缠论K线 → 分型 → 新笔 → 笔中枢 → 走势类型，并由低级别走势类型递归生成高级别笔，最终输出一买信号（预警，不下单）。

## 设计原则

1. **结构清晰**：单一职责分层，依赖方向由 import-linter 强制（3 条契约，CI 门禁之一）。
2. **虚拟/物理分离**：缠论算法（domain）不知道交易所、数据库、绘图、LLM 的存在。
3. **规则先行**：所有规则口径先冻结于 `docs/rules.md`，再写代码；配置集中在可序列化的 `RulesConfig`。
4. **无未来函数**：实时输出只依赖当前时点之前的数据，候选/确认/失效全程可追溯。
5. **可复现**：同一份输入数据 + 同一份配置，必须得到同一份结构结果。
6. **高复用率**：基础口径（分型/新笔/力度度量/一买谓词）经反腐层委托给固定版本的 czsc；CPT 自研 czsc 未覆盖的核心（笔中枢、走势类型、递归映射、一买状态机）。
7. **失败必须分类**：降级/空数据要写明**为什么**（`no_factor` / `no_data` / `db_error:*` / `invalid_code`），不静默成"看起来正常但没数据"。

## 架构速览

**4 层**（`engine/` 与 `storage/` 已于 2026-09-25 整层删除，`llm/` 从未有过代码；
早期版本的 6 层图见 git history）：

```text
web/           只读 HTTP adapter + 两个入口（加密 / A股）
application/   用例编排（回放 / 查询 / 导出 / 快照构造）
adapters/      外部数据接入（Binance / ccxt / Wind / 腾讯 / 新浪 / 本地 PG）+ 缠论后端（czsc / native / 契约 reference_chanlun）
domain/        纯算法与领域模型（零第三方依赖，一套算法跨级别复用）
```

依赖方向 `web → application/adapters → domain`，`domain` 不导入任何上层，
由 `.importlinter` 的 3 条契约强制（2026-09-25 起纳入 CI）。

> 设计文档里另有一层「独立 LLM 服务层」的**未实现蓝图**（`architecture.md` §6），
> 明确标注「`cpt/llm/` 从未有过代码」，不计入上表。

详细设计见 `docs/architecture.md`。

## 两个市场

| | 加密永续 | A 股日线 |
|---|---|---|
| 入口 | `python -m cpt.web` | 主看板内切「A股」，或 `python -m cpt.web.a_share --code 600519` |
| 行情源 | Binance USDT-M（主）/ ccxt（兜底） | 本地 PG（主）/ Wind（主，耗额度）/ 腾讯（兜底）/ 新浪（快照） |
| 周期 | 1m–1w，实时轮询 | 固定 `1d`，**不轮询**（收盘后不再变） |
| 复权 | 不适用 | `daily_bar`（不复权原始价）× `asel.ref_adjust_factor` → 后复权 |
| 标的显示 | 交易对下拉（`BTCUSDT`） | **代码 + 中文名**（`600519 贵州茅台`）—— 只有六位数字太容易看岔 |

**两个市场共用同一套缠论后端与同一份 `dashboard.v2` 快照 schema** —— 所以四个画布对 A 股**零改动复用**（由 `tests/test_dashboard_ashare_contract.py` 钉住：`canvas_b.js`/`canvas_c.js` 里不得出现市场分支）。

### A 股复权因子：全量兜底 + 按需精修

全库 5,222 只已全部写入 `asel.ref_adjust_factor`（默认 `hfq_factor=1.0`，兜底全覆盖）。对于除权日价格敏感的标的，可通过**按需拉取**精修：输入代码 → 从腾讯拉 `raw + hfq` 两个序列（同源同对，`hfq_factor = hfq/raw` 绝对自洽）→ 幂等落库 → 重出快照，覆盖默认的 1.0 因子。

> 腾讯是否提供某标的的 `hfqday` 是**逐标的**属性、**无法用代码前缀预测**（实测 `688111`/`688036` 有而 `688981` 没有；多数 `301` 有而近期新股没有）。所以实现里**没有任何板块判断**，只如实报告腾讯实际返回了什么。教训见 `docs/progress-log.md` R15-1 节（同一处先后记错过两次）。
>
> 2026-09-29 全量兜底：用 `daily_bar` 自身数据写入 `hfq_factor=1.0`，覆盖全部 5,222 只，
> 消除"约 100 只"的覆盖缺口。后续若发现价格偏差，可用 `scripts/factor_backfill.py`
> 从 akshare 拉真实因子覆盖。

### A 股标的名称

名称取自本地 `asel.security_master`（对 `daily_bar` 的 5,225 只**零缺失**，另带 `board` 板块），**不花 Wind 额度**。显示在三处，都是"确认自己在看哪只票"的位置：

| 位置 | 形式 |
|---|---|
| 顶栏「标的」 | `600519 贵州茅台`（名称加粗、非等宽） |
| 代码下拉 | `002119 康强电子（#1）` |
| 页面标题 | `贵州茅台 600519 · CPT`（多标签页时看岔发生在标签栏，名称放最前） |

> 名称里有**填充空白**：老行情源按 4 字宽补齐，于是存成 `深 赛 格`、`ST 中 侨`、
> `万  科Ａ`（全库 80 条）。归一化必须**删掉全部空白**（折叠成单个空格则
> `深 赛 格` 原样不变、没用）；已核对这 80 条里没有一条是"ASCII 单词之间的有意义
> 空格"，所以删除是安全的。
>
> 名称是纯展示信息，不参与任何结构计算；查询失败一律降级为空，**绝不影响出图**。

## 文档导航

| 文档 | 内容 |
|---|---|
| `docs/rules.md` | 规则口径唯一事实来源（含 §9 已冻结约定） |
| `docs/architecture.md` | 软件结构设计（分层、模块、数据模型、复用映射、测试策略） |
| `docs/implementation-plan.md` | 实施计划（M0–M6 垂直切片里程碑 + M-LLM 独立线） |
| `docs/reference-audit.md` | 参考仓库许可证与复用边界 |
| `docs/progress-log.md` | 逐轮进度日志（R13 起；更早见 `docs/archive/`） |
| `docs/known-traps.md` | 已知陷阱与非缺陷清单（8 类"像 bug 其实不是"，每条附判定命令） |
| `deploy/README.md` | Nginx / systemd / 静态看板部署说明（含权限坑） |

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

核心算法、只读 Dashboard（四画布）、研究服务、HTTP adapter、**加密实时行情**、**A 股日线链路**（含按需复权因子与证券名称）均已实现并上线（`cpt-dashboard.service`，`--mode realtime`）。API 只读，不提供自动下单、撤单、账户、持仓或订单簿接口。

## 本地启动

### 加密看板

```bash
cp deploy/env/cpt-dashboard.env.example deploy/env/cpt-dashboard.env
.venv/bin/python -m cpt.web --host 127.0.0.1 --port 8010 --mode realtime --poll-seconds 30
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

CI（`.github/workflows/ci.yml`）在 **Python 3.12 与 3.14 两个版本**上各跑一遍以下 6 条，
逐条执行、任一失败即红：

```bash
pytest tests -rsq
ruff check cpt tests scripts
ruff format --check cpt tests scripts
mypy cpt scripts
lint-imports                                    # 3 条分层契约
vulture --min-confidence 60 cpt whitelist.py    # 死代码审计
```

三点容易记错，都是踩过的坑：

- **`scripts/` 在门禁范围内**。2026-09-25 之前只覆盖 `cpt tests`，等于给 364 行的
  运维入口开了后门，它自带的重复实现和有顺序 bug 的函数都没人发现。
- **`lint-imports` 现在也在 CI 里**。此前只本地/pre-commit 跑，层级可以被打破而 CI 全绿。
- **vulture 阈值是 60 不是 80**。80 会把「未使用的函数/类」（置信度正好 60%）全滤掉，
  该步骤永远 exit 0。也不再 `--exclude 'cpt/web/app.py'`——那会连带隐藏 app.py 里对
  别处符号的真实使用；框架回调改由 `whitelist.py` 逐条登记。

> Windows 开发机上有两个**已知且与代码无关**的基线偏差：
> `a_share_pool.py` 顶层 `import fcntl` 导致该模块在 Windows 不可导入
> （跑全量需 `--ignore=tests/test_a_share_pool.py`），以及由此产生的
> `test_web_a_share_routes.py` 13 条 failed 与 mypy 4 条 `flock` 报错。
> CI 跑在 ubuntu 上不受影响。

CI 只装 `requirements-dev.txt`，因此 czsc/ccxt/psycopg/pandas/plotly/wbt 均不在
CI 环境里 —— 依赖它们的测试一律走 `pytest.importorskip`，**不允许**用 mock 假装
它们在位。

首版非目标：自动下单、多交易所、LLM 参与结构判断。

## 风险声明

本项目仅用于个人学习与技术研究，不构成任何投资建议。金融市场存在风险，缠论信号（含一买）的历史表现不代表未来收益；任何实盘决策请独立判断并自行承担风险与责任。
