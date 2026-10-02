# CPT — Chan Pattern Trader

一个以**学习金融与软件工程**为目标的缠论分析系统，覆盖**加密永续**与**A 股日线**两个市场。

CPT 基于缠中说禅理论做结构分析：缠论K线 → 分型 → 新笔 → 笔中枢 → 走势类型，并由低级别走势类型递归生成高级别笔，最终输出一买信号（预警，不下单）。

## 设计原则

1. **结构清晰**：单一职责分层，依赖方向由 import-linter 强制（4 条契约，CI 门禁之一）。
2. **虚拟/物理分离**：缠论算法（domain）不知道交易所、数据库、绘图、LLM 的存在。
3. **规则先行**：所有规则口径先冻结于 `docs/rules.md`，再写代码；配置集中在可序列化的 `RulesConfig`。
4. **无未来函数**：实时输出只依赖当前时点之前的数据，候选/确认/失效全程可追溯。
5. **可复现**：同一份输入数据 + 同一份配置，必须得到同一份结构结果。
6. **高复用率**：基础口径（分型/新笔/力度度量/一买谓词）经反腐层委托给固定版本的 czsc；CPT 自研 czsc 未覆盖的核心（笔中枢、走势类型、递归映射、一买状态机）。
7. **失败必须分类**：降级/空数据要写明**为什么**（`no_factor` / `no_data` / `db_error:*` / `invalid_code`），不静默成"看起来正常但没数据"。

## 架构速览

**5 层**（`engine/` 已于 2026-09-25 整层删除；`storage/` 于 2026-10-01 的 R24
**恢复**；`llm/` 仍是未实现蓝图）：

```text
web/           只读 HTTP adapter + 两个入口（加密 / A股）
application/   用例编排（回放 / 查询 / 导出 / 快照构造）—— 不出现 SQL
storage/       CPT 自有持久化：public.cpt_* 的读写。SQL 只许出现在这一层和 adapters
adapters/      外部系统接入（行情源 + 共享数据源 emotion_core + 缠论后端委托）
domain/        纯算法与领域模型（零第三方依赖，一套算法跨级别复用）
```

依赖方向 `web → application → {storage, adapters} → domain`，`domain` 不导入任何
上层，由 `.importlinter` 的 **4 条契约**强制（CI 门禁之一）。

### 分层的两条硬规矩

1. **SQL 只许出现在 `cpt/adapters/` 与 `cpt/storage/`**。
   `import-linter` 查的是依赖方向，查不了职责归属 —— `conn` 只是个 `Any` 形参、
   没有 `import psycopg`，依赖图上看不出越界。R24 实测 SQL 曾铺在四层里而 3 条
   契约全绿。这条由 `scripts/check_sql_layering.py` 做**内容级**门禁，已进 CI。
2. **`cpt/storage/` 只装 CPT 自有表**（`public.cpt_*`）。`emotion_core` 共 28 张表，
   **只有 2 张是 CPT 的**，其余 26 张是跨项目共享的 A 股数据枢纽 —— 读它属于
   「接外部数据源」，归 `adapters`，不归 storage。

> 设计文档里另有一层「独立 LLM 服务层」的**未实现蓝图**（`architecture.md` §4），
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

### A 股复权因子：两条路，生产表用哪条要看报告

**先说清楚现状**（R39/R40 实测，不是推断）：

- `asel.ref_adjust_factor` 里 5222 只票中，**3028 只（58%）的 `hfq_factor` 全程
  等于 1.0** —— 那是 2026-09-29「全量兜底」写进去的占位值，从未计算过；
- 另有 **992 只票的因子会向下跳**（全表向上 4162 次 / 向下 2852 次）。纯后复权
  因子必须单调不降（除权日向上跳把分红加回去），所以**这一列对那 992 只不是
  后复权因子**。根因在上游 `asel` 的产数逻辑，不在本仓。

两条补算路径：

| 路径 | 来源 | 说明 |
|---|---|---|
| `scripts/factor_backfill.py` | 腾讯 `hfq/raw` | 按需拉单只标的；`hfq_factor = hfq/raw` 自洽 |
| `scripts/factor_recompute.py` | **东财** `RPT_SHAREBONUS_DET` | 全量重算，默认源；免费、从大阪直连 200、**无额度** |

Wind 路径（`--source wind`）保留作交叉校验，但要积分 —— 实测 24/2197 只就撞
「账户积分余额不足」，所以不再是默认。

> ⚠️ `factor_recompute.py` **只写暂存表 `asel.ref_adjust_factor_v2`**，
> **不碰生产表**。这是 R37 定下的纪律：先看对账报告，再由人决定是否切换。
> 切换前的唯一依据是 `scripts/factor_report.py` 的逐票结论。

⚠️ 东财的 `PRETAX_BONUS_RMB` 是**每 10 股**的税前派息（茅台 2024-12-31 报告期
276.73 ⇒ 每股 27.673 元）。忘了除以 10，因子会差整整一个数量级 ⇒ 所有历史价格 ×10。
该换算已由 R39 的真机对账钉死：东财算出的单次除权台阶与库里真实跳变 **18/23 匹配、
偏差 ±0.9% 内**。

> 腾讯是否提供某标的的 `hfqday` 是**逐标的**属性、**无法用代码前缀预测**（实测 `688111`/`688036` 有而 `688981` 没有；多数 `301` 有而近期新股没有）。所以实现里**没有任何板块判断**，只如实报告腾讯实际返回了什么。教训见 `docs/progress-log.md` R15-1 节（同一处先后记错过两次）。

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
| `docs/known-traps.md` | 已知陷阱与非缺陷清单（16 条"像 bug 其实不是"，每条附**判定命令**） |
| `docs/pending-wiring.md` | 尚未接线模块清单（是产品决策，不是死代码） |
| `docs/dashboard-product-roadmap.md` | 看板产品路线（Phase 1–5） |
| `deploy/README.md` | Nginx / systemd / 静态看板部署说明（含权限坑） |

### 运维脚本

| 脚本 | 作用 | 会写库吗 |
|---|---|---|
| `scripts/run_inspection.py` | 每日巡检：读 `cpt_run_metric` + 源状态 → 飞书告警 | 只写巡检结论行 |
| `scripts/snapshot_a_share_batch.py` | A 股批量快照（systemd timer 每日触发） | 是 |
| `scripts/factor_recompute.py` | 按公司行动重算后复权因子 | **只写暂存表** |
| `scripts/factor_report.py` | 因子对账报告（切生产表的唯一依据） | 否 |
| `scripts/golden_set.py` | 结构指纹基线，`--check` 有差异 exit 1 | 会留水位行 |
| `scripts/check_sql_layering.py` | 门禁：SQL 只许出现在 `adapters`/`storage` | 否 |

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

`--backend native|czsc|auto` 选缠论后端，**默认是 `native`（自研）**。
`auto` 的语义是「装了 czsc 就用 czsc」—— ⚠️ **它只用于 CI 与画面对照，任何情况下都不要
用它跑生产**：一旦某台机器多装了 `czsc`，同一份配置就会在**毫无报错**的情况下切到
另一套实现，结构随之全变。R36 因此把 `DEFAULT_BACKEND` 从 `auto` 钉回 `native`；
czsc 现在的位置是 parity 的**参照侧**（`parity_reference.py`），那边回落是安全的。

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
lint-imports                                    # 4 条分层契约
python scripts/check_sql_layering.py           # SQL 只许在 adapters/storage（R24 新增）
vulture --min-confidence 60 cpt whitelist.py    # 死代码审计
```

四点容易记错，都是踩过的坑：

- **`scripts/` 在门禁范围内**。2026-09-25 之前只覆盖 `cpt tests`，等于给 364 行的
  运维入口开了后门，它自带的重复实现和有顺序 bug 的函数都没人发现。
- **`lint-imports` 现在也在 CI 里**。此前只本地/pre-commit 跑，层级可以被打破而 CI 全绿。
- **vulture 阈值是 60 不是 80**。80 会把「未使用的函数/类」（置信度正好 60%）全滤掉，
  该步骤永远 exit 0。也不再 `--exclude 'cpt/web/app.py'`——那会连带隐藏 app.py 里对
  别处符号的真实使用；框架回调改由 `whitelist.py` 逐条登记。
- **`check_sql_layering.py` 管的是 import-linter 管不了的那一半**。import-linter
  查依赖方向，查不了「职责有没有放对层」。

> Windows 开发机上有几个**已知且与代码无关**的基线偏差（`fcntl` / 无浏览器）：
> `a_share_pool.py` 顶层 `import fcntl` 导致该模块在 Windows 不可导入
> （跑全量需 `--continue-on-collection-errors`），并连带
> `test_web_a_share_routes.py` 14 条 failed、`test_a_share_pool.py` 1 条 collection error、
> 两条 chromium smoke failed，以及 mypy 4 条 `flock` 报错。
>
> **权威基线（2026-10-02 实测，`--junit-xml` 取值）**：
> **853 tests / 15 failures / 1 error / 29 skipped / 808 passed**。
> 判断「有没有打破契约」时，**不要数失败条数**，要把 `git stash` 后的干净基线
> 跑一遍**逐条对比失败名单** —— 条数会随环境漂，名单不会。
> CI 跑在 ubuntu 上不受影响。

CI 只装 `requirements-dev.txt`，因此 czsc/ccxt/psycopg/pandas/plotly/wbt 均不在
CI 环境里 —— 依赖它们的测试一律走 `pytest.importorskip`，**不允许**用 mock 假装
它们在位。

首版非目标：自动下单、多交易所、LLM 参与结构判断。

## 风险声明

本项目仅用于个人学习与技术研究，不构成任何投资建议。金融市场存在风险，缠论信号（含一买）的历史表现不代表未来收益；任何实盘决策请独立判断并自行承担风险与责任。
