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

已开始实现：`cpt/application/dashboard_inspector.py` 提供 raw bar → merged bar → 结构引用的只读检查服务。

研究模式北极星指标：三次点击内回答“这个结构为什么长这样”。

### R1 数据集/运行浏览器

展示 symbol、级别链、K 线数、生成时间、配置摘要、dataset hash、来源和运行状态。

### R2 逐根检查器

当前已交付 application inspector 与 containment provenance trace，前端逐字段检查器已接入。

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
- 两份 snapshot 字段级 diff（✅ R22：`dashboard_compare.py` 接进 `GET /api/dashboard/compare?left=&right=`，
  底层 `dashboard_reproducibility.py::snapshot_diff`；序列型值降级为 `{__summary__,count,hash}`，
  防止原始 candles 数组灌进响应）。

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
- 收盘倒计时（✅ R21：`_attach_close_countdown` 接 `public.trade_calendar`）；
- 当前信号状态。

## Phase 4：盯盘模式 P1

- 信号历史列表；
- 信号到达提醒（✅ R21：`_attach_signal_change` 检测 status 变化，前端弹 ⚡ 通知）；
- 多交易对（✅ R22：`GET /api/dashboard/watchlist` 投影自 A 股候选池 `pool_payload`，
  条目含 code/name/board/drawable/group/last_price/change_pct；`watchlist_rows` 接进 `cpt/web/app.py:234`）；
- 最新价线；
- 成交额；
- reconnect/stale（✅ R22：`dashboard_watch.py::watch_metrics` 接进 `dashboard_snapshot_v2.py:29`
  → `snapshot.watch_metrics`，前端 `renderWatchMetrics()` 显示最新价/涨跌幅/窗口高低/窗口量；
  **注意 `"24h": None` 仍是硬编码**——窗口统计来自 bars，不是 24h 聚合）；
  > 📌 **范围说明（2026-10-01 补）**：上面这句说的是 **A 股**路径。A 股没有 24h
  > 行情源，前端按 `available:false` 显示「—」（`progress-log.md` R21 摘掉窗口兜底
  > 就是为这个）。**加密 realtime 路径有真实 24h**：
  > `__main__.py:844 _safe_24h_for()` → `fetch_24h_ticker()` → `normalize_24h()`，
  > 上游不可达时降级为 `{"available": false, "reason": "upstream_ticker_unavailable"}`。
  > 另有 `docs/dashboard-final-acceptance.md` 第 40 行说「24h 真实数据已接入」——
  > 那句对加密成立、对 A 股不成立。三处并非矛盾，是**没标范围**。
- SSE 或高效实时更新（✅ R22：`dashboard_realtime.py::realtime_update` 接进
  `cpt/web/__main__.py:572` 的 `_RealtimeProvider._cached_snapshot`，容量 8 的 `(symbol, interval_ms)` LRU；
  仍是 HTTP 轮询 + 缓存命中，**不是真的 SSE 推流**）。

## Phase 5：研究模式 P1

- 级别递归树（✅ R22：`dashboard_levels.py::level_tree` 接进 `dashboard_snapshot_v2.py:48`
  → `snapshot.level_tree`，并另有 `GET /api/dashboard/levels`；前端 `renderLevelTree()` 优先读后端，
  后端不可用时**回退**到从 `snapshot.overlays` 现算，`ul[data-source=backend|overlays]` 标注来源）；
- 事件前后状态对比；
- 配置字段 diff；
- 回放与引擎内部状态；
- 数据质量报告（✅ R22：`dashboard_quality.py::quality_report` 接进 `dashboard.py:140 _data_quality`，
  两条活路径都过；`data_quality` 由 4 键扩为 9 键，新增 severity/gap_count/out_of_order_count/gaps/out_of_order）；
- localStorage 本地注释（✅ R21：`dashboard.js:1113/1119` setItem/getItem 已实现）；

本地注释不进入数据集、不影响 hash、不修改结构数据。

## Phase 6：研究模式 P2 与最终验收

- 一买统计（✅ R22：`dashboard_stats.py::signal_statistics` 接进 `GET /api/dashboard/signal-stats`，
  数据源 `signal_event_store.py::load_signal_events` 读 `public.cpt_signal_event` 的**状态跃迁事件**）；
- alert→confirmed 转化率（✅ R22：同一路由的 `alert_to_confirmed_rate`；
  **口径必须自报** —— 响应带 `"basis": "signal_event_transitions"`，因为这是"事件流里的转化率"，
  不是"当前若干只票的状态"）；
- invalidated 原因分布（✅ R22：同一路由的 `invalidated_count`；**降级**：无 psycopg/无库时
  返回 `{"available": false, "reason": "signal_history_unavailable"}`，不假装是 0）；
- 双数据集同步对比（✅ R21：`_attach_dual_compare` 直连东财 `push2.eastmoney.com`，比 CPT 本地 vs 实时。
  ⚠️ **这与 `dashboard_multi_run.py::align_runs` 不是同一件事** —— 后者比的是**本进程内两次 run 的快照**，
  R22 接进 `GET /api/dashboard/multi-run?run_ids=a,b,c`（2–5 个），输出
  `{run_count, timestamps, points[{open_time, run_0, run_1, ...}]}`，故本行两条并列，互不替代）；
- 时间范围切片导出（✅ R22：`dashboard_export.py::slice_snapshot` 接进
  `GET /api/dashboard/export?start_ms=&end_ms=`；
  **⚠️ R32 更正：前端「范围导出」面板并不存在** —— 真机 grep
  `/var/www/cpt-dashboard/*.js`，没有任何文件引用 `dashboard/export`，本接口是
  纯 API。且它**只切 `candles`**：结构/指标块仍是完整窗口的内容（下标会越界），
  详见 `slice_snapshot` docstring；
  与 `docs/export-schema-v1.md` 的**数据集导出 schema v1 是两件事**，后者未动）。

## 后端代码框架

```text
cpt/domain/provenance.py
cpt/domain/containment_trace.py
cpt/domain/inspector.py
cpt/application/dashboard_snapshot.py
cpt/application/dashboard_runs.py
cpt/application/dashboard_inspector.py
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
