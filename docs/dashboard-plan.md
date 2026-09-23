# CPT Dashboard 实施计划

版本：v0.1 · 2026-09-23
状态：已冻结 D0 方案，按阶段执行

## 1. 目标与边界

为 CPT 增加一个参考币安交易页面布局的信息型 Dashboard，展示行情、K 线、缠论结构、实时状态和回放结果。

Dashboard 默认只读，不实现：

- 下单、撤单、交易指令；
- 账户余额、杠杆和持仓管理；
- 浏览器端 API Key / Secret 管理；
- 让 LLM 决定分型、笔、中枢或买卖点。

Dashboard 是 CPT 的观察与回放界面，不是交易终端。

## 2. 分阶段路线

### D0：Web 基础盘点与方案冻结

- 盘点仓库现有 Web 前端、启动命令、HTTP 入口和图表依赖。
- 确认 Dashboard 与 domain/application/engine 的边界。
- 确认离线回放优先，真实 Binance 数据通过已有窄接口进入。
- 冻结本文件及 Dashboard schema 版本策略。

验收：现有 Web 基础和缺口有文档记录；不新增重复行情 HTTP 逻辑。

### D1：DashboardSnapshot 数据契约与只读 API

新增稳定 ViewModel，不让前端直接消费内部 dataclass：

```text
DashboardSnapshot
├── schema_version
├── market
├── candles
├── overlays
│   ├── fractals
│   ├── bis
│   ├── zhongshus
│   └── trend_types
├── signal
├── events
├── data_quality
└── runtime
```

约束：

- 时间统一 Unix 毫秒；
- 价格、数量、成交量字段明确单位；
- 结构对象保留 `level`、`revision`、`source_ids`；
- 未收盘必须标记 `alert`，不得伪装 `confirmed`；
- 缺口、乱序、stale 必须显式返回；
- JSON 序列化稳定并带 schema 版本。

初版只读接口：

```text
GET /api/dashboard/snapshot
GET /api/dashboard/candles
GET /api/dashboard/events
GET /api/dashboard/health
GET /api/dashboard/export
```

若仓库没有现成 HTTP 服务，D1 先交付可被 HTTP 层调用的 snapshot application service，不擅自引入完整 Web 框架。

### D2：页面骨架与行情概览

参考币安深色交易终端布局，但不复制品牌资源：

- 顶部：项目、模式、symbol、周期、来源、更新时间、连接状态；
- 左侧：当前价格、24h 信息、成交量、数据质量；
- 中央：K 线图容器、成交量区域和时间轴；
- 右侧：走势类型、当前笔、中枢、一买状态和 revision；
- 底部：事件时间线和回放控制占位。

必须完整处理 loading、empty、error、stale、gap 状态。

### D3：K 线与缠论结构叠加

- K 线与成交量；
- 缠K合并边界；
- 顶/底分型；
- 新笔折线；
- 笔中枢矩形；
- 走势类型区间；
- 一买状态标记；
- alert 与 confirmed 使用不同状态色。

图形元素与右侧详情通过 `source_ids` 联动。

### D4：事件时间线与回放

- created / updated / confirmed / invalidated 事件；
- 一买状态变化；
- 播放、暂停、单步、跳转；
- 窗口大小、截断状态、回放进度；
- schema v1 / Dashboard snapshot 导出。

离线回放不得依赖 Binance 网络。

### D5：实时更新

- 优先复用 `BinanceFuturesClient` 与 `RealtimeEngine`；
- 初版采用 SSE 或短轮询；
- 连接断开、重连、stale、缺口告警；
- 未收盘显示 alert，收盘后显示 confirmed；
- 不跨缺口生成正式结构。

### D6：质量验收与阶段性提交

- Dashboard focused 测试；
- 现有 CPT 全量测试；
- snapshot 重复加载稳定；
- 离线演示不依赖网络；
- import-linter 保持 UI 不侵入 domain；
- 更新本计划、progress-log 和质量报告；
- 阶段验收通过后 git commit。

## 3. 统一执行门禁

每个阶段遵循：

```text
IMPLEMENT → focused test → 全量验证 → 文档更新 → git commit
```

统一检查：

```text
.venv/bin/python -m pytest tests -q
.venv/bin/python -m ruff check cpt tests scripts
.venv/bin/python -m ruff format --check cpt tests scripts
.venv/bin/mypy cpt
.venv/bin/import-linter lint --config .importlinter
git diff --check
```

## 4. 当前状态

- D0：已完成。仓库当前为纯 Python CPT，没有 `package.json`、Vite、React 或现成 HTTP 服务；因此先交付可被未来 HTTP 层调用的 application service，不引入 Web 框架。
- D1：已完成基础 snapshot service，见 `cpt/application/dashboard.py`；暂未创建 HTTP server。
- D2：已完成静态页面骨架、深色终端布局、响应式状态样式和无第三方 SVG 图表容器。
- D3：已完成手写 SVG K 线、成交量、分型、笔、中枢和走势类型叠加；支持点击结构查看详情。
- D4：已完成离线事件时间线与播放/暂停/单步/重置/跳转控制。
- D5：已完成 snapshot 短轮询、连接异常提示和 stale 超时状态；仍不包含交易操作。
- D6：基础质量验收完成；后续可继续补真实 HTTP adapter 和更细的前端自动化测试。

D1 当前契约入口：

```python
from cpt.application.dashboard import build_dashboard_snapshot, dashboard_json
```

该入口可消费 CanonicalBar、Fractal、Bi、ZhongShu、TrendType、Signal 和 StructureEvent，输出稳定的 `dashboard.v1` JSON-compatible snapshot。

## 5. 后续限制

- 首版只读，不承诺交易生产能力；
- Rust chanlun 仍是独立参考实现，不是绝对正确答案；
- 实时行情请求必须经过已有 Binance adapter；
- Dashboard 不得复制 domain 算法或行情 HTTP 逻辑。
