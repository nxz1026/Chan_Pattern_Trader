# CPT Dashboard 产品落地路线图

版本：v1.0 · 2026-09-23
依据：`cpt-feature-review-combined.md`

## 产品形态

一个 Dashboard 外壳、两种模式：

- `watch`：盯盘模式，低信息密度、快速回答价格与信号状态。
- `research`：研究模式，高信息密度、回答结构为什么这样形成。

共享图表、回放、结构联动、数据契约和健康状态。两种模式通过 `?mode=watch|research` 和顶部切换进入。全程只读，不实现下单、撤单、账户、持仓、盘口或修改结构数据。

## 共享底座

1. K 线/成交量/缠论结构 SVG 渲染器；
2. 缩放、拖拽、时间轴和十字线；
3. 十字线 OHLCV tooltip；
4. 结构点击与右侧详情联动；
5. 统一健康灯：live、offline、stale、gap、error；
6. 回放播放、暂停、单步、重置、跳转；
7. 稳定 `dashboard.v1/v2` JSON 契约；
8. snapshot、dataset hash、config hash、rules version 的可复现信息。

## Phase 0：共享底座和双模式

### 功能

- `?mode=watch|research`；
- 顶部模式切换；
- 图表缩放/拖拽；
- 十字线和 OHLCV；
- 结构点击联动；
- 健康灯合并；
- 空数据、缺口、stale、断线状态。

### 代码边界

```text
dashboard/js/state.js
dashboard/js/chart.js
dashboard/js/crosshair.js
dashboard/js/app.js
```

当前无构建工具，迁移期保留 `dashboard/dashboard.js` 和 `window.CPTDashboard` 兼容入口。

## Phase 1：研究者模式 P0

研究模式北极星指标：三次点击内回答“这个结构为什么长这样”。

### R1 数据集/运行浏览器

展示 symbol、级别链、K 线数、生成时间、配置摘要、dataset hash、来源和运行状态。

### R2 逐根检查器

选择 raw bar 后展示：

- 原始 OHLCV 全字段；
- 缠K合并结果；
- 包含方向和决策；
- 关联分型、笔、中枢、走势类型；
- 反向引用。

### R3 结构溯源树

支持：

```text
结构 → source_ids → merged bar → raw bar
raw bar → 所有关联结构
```

### R5 可复现性面板

展示：

- dataset hash；
- config hash；
- rules version；
- schema version；
- engine version；
- source；
- generated_at；
- 两份 snapshot 字段级 diff。

## Phase 2：Oracle 对比

标准化 parity：

```text
parity
├── summary: matched/missing/extra/mismatched/match_rate
├── fractals
├── bis
└── zhongshus
```

每项保留 kind、status、CPT ref、oracle ref、difference_fields、时间范围，并可点击定位图表。

## Phase 3：盯盘模式 P0

- 真实涨跌幅；
- 真实 24h 高低点和成交量，无法提供时明确 unavailable 或隐藏，不显示永久 `—`；
- 周期切换；
- 多级别筛选；
- MACD 副图；
- 收盘倒计时；
- 当前信号状态。

## Phase 4：盯盘模式 P1

- 信号历史列表；
- 信号到达提醒；
- 多交易对；
- 最新价线；
- 成交额；
- reconnect/stale；
- SSE 或高效实时更新。

## Phase 5：研究模式 P1

- 级别递归树；
- 事件前后状态对比；
- 配置字段 diff；
- 回放与引擎内部状态；
- 数据质量报告；
- localStorage 本地注释。

本地注释不进入数据集、不影响 hash、不修改结构数据。

## Phase 6：研究模式 P2 与最终验收

- 一买统计；
- alert→confirmed 转化率；
- invalidated 原因分布；
- 双数据集同步对比；
- 时间范围切片导出。

## 后端代码框架

```text
cpt/domain/provenance.py
cpt/domain/containment_trace.py
cpt/domain/inspector.py
cpt/application/dashboard_snapshot.py
cpt/application/dashboard_runs.py
cpt/application/dashboard_inspector.py
cpt/application/dashboard_parity.py
cpt/application/dashboard_reproducibility.py
cpt/web/app.py                 # 有 HTTP 需求后再引入
```

Application 层先提供纯 Python service，不引入 Web 框架；Web 层不得复制 Binance 请求或 domain 算法。

## 前端代码框架

```text
dashboard/js/state.js
dashboard/js/chart.js
dashboard/js/crosshair.js
dashboard/js/overlays.js
dashboard/js/inspector.js
dashboard/js/provenance.js
dashboard/js/parity.js
dashboard/js/reproducibility.js
dashboard/js/replay.js
dashboard/js/watch-mode.js
dashboard/js/research-mode.js
dashboard/js/formatters.js
```

拆分期间保留现有 `dashboard/dashboard.js` 对外 API。

## 测试和验收

Python：

- schema v2 与 v1 兼容；
- source_ids 展开与反向引用；
- containment decision；
- dataset/config/rules hash；
- parity matched/missing/extra；
- snapshot 稳定；
- 空数据、gap、未收盘、stale。

浏览器 smoke：

1. watch/research 模式加载；
2. 缩放；
3. 十字线 OHLCV；
4. 结构点击；
5. 溯源树展开；
6. raw bar 反向高亮；
7. oracle 差异定位；
8. snapshot diff；
9. 回放单步；
10. stale/gap 状态。

每个 Phase 必须：

```text
focused test → 全量测试 → 静态门禁 → 浏览器 smoke → 文档 → commit → push
```

## 明确不做

下单、撤单、账户、持仓、盘口深度、画线工具、任何修改结构数据的写操作、让 LLM 决定结构。
