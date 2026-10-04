# CPT Web 接口清单（R45 实测生成）

> **这份清单是从 `cpt/web/` 代码抽出来、并逐条打真机验证的**，不是手写的。
> 生成脚本与实测记录见本文件末尾。
>
> R45 之前**全仓没有一份权威接口清单** —— `docs/dashboard-plan.md` 里那份是
> 2026-09-23 的**计划**，其中 `/candles`、`/events` 从未作为独立接口实现
> （数据已并入 `/api/dashboard/snapshot`），`/api/dashboard/stats` 代码里没有。

共 **25** 个接口，全部可达（状态码 2xx/4xx 均表示路由存在且按契约应答）。

## 接口一览

| 方法 | 路径 | 真机 | 用途 | 源码 |
|---|---|---:|---|---|
| `GET` | `/api/canvas/wbt` | `200` | 画布 D：wbt 报告视图 | `app.py` |
| `POST` | `/api/dashboard/a-share/llm/explain` | `200` | 提交一次 LLM 规则解释（写，fire-and-forget） | `app.py` |
| `GET` | `/api/dashboard/a-share/pool` | `200` | A 股热门池 / 观察池 | `app.py` |
| `GET` | `/api/dashboard/a-share/snapshot` | `200` | A 股单只快照 | `app.py` |
| `POST/DELETE` | `/api/dashboard/a-share/watchlist` | `400` | A 股自选增删（写） | `app.py` |
| `GET` | `/api/dashboard/compare` | `200` | 两次运行的对比 | `app.py` |
| `GET` | `/api/dashboard/engine-state` | `200` | 引擎状态（revision 等） | `app.py` |
| `GET` | `/api/dashboard/export` | `400` | JSON 导出（schema v1，冻结契约） | `app.py` |
| `GET` | `/api/dashboard/health` | `200` | 只读健康检查（read_only=true） | `app.py` |
| `GET` | `/api/dashboard/inspect` | `200` | 单根 bar 的结构细节 | `app.py` |
| `GET` | `/api/dashboard/inspection` | `200` | 巡检结论 | `app.py` |
| `GET` | `/api/dashboard/levels` | `200` | 级别树（⚠️ level 单位按市场而异） | `app.py` |
| `GET` | `/api/dashboard/llm/calls` | `200` | LLM 调用审计列表 | `app.py` |
| `GET` | `/api/dashboard/market-24h` | `200` | 24h 行情概览 | `app.py` |
| `GET` | `/api/dashboard/multi-run` | `200` | 多选运行对比 | `app.py` |
| `GET` | `/api/dashboard/parity` | `200` | 与参照实现的结构一致性对照 | `app.py` |
| `GET` | `/api/dashboard/reproducibility` | `200` | 可复现性哈希 / dataset_hash | `app.py` |
| `GET` | `/api/dashboard/runs` | `200` | 运行索引列表（ring 与 PG 表合并后的冷路径） | `app.py` |
| `GET` | `/api/dashboard/signal-radar` | `200` | 信号雷达 | `app.py` |
| `GET` | `/api/dashboard/signal-stats` | `200` | 信号状态跃迁统计 | `app.py` |
| `GET` | `/api/dashboard/structure-events` | `200` | 结构事件流（append-only） | `app.py` |
| `GET` | `/api/dashboard/sources` | `200` | 数据源列表与探活状态 | `app.py` |
| `GET` | `/api/dashboard/structure-events` | `200` | 结构事件流（append-only） | `app.py` |
| `GET` | `/api/dashboard/structure-events/timeline` | `200` | 单个结构的完整时间线 | `app.py` |
| `GET` | `/api/dashboard/watchlist` | `200` | 自选（读） | `app.py` |

## 两个写接口的额外约束

审计 M1 的结论：**写接口无鉴权**（单用户看板经 nginx `auth_basic` 暴露）。
唯一的客户端侧守卫是 **必须带 `Content-Type: application/json`**：

```
POST /api/dashboard/a-share/llm/explain?code=600519   (带 CT) → 200
POST 同样请求但不带 Content-Type                      → 415 content_type_required
```

HTML 表单 / `simple request` 发不出这个 CT，所以它挡住的是 CSRF。
⚠️ 但它**挡不住**任何能主动设 CT 的客户端 —— 单用户看板的定位下这是可接受的，
但**不要**把这层当认证用。

## 与计划文档的差异（R45 核实）

| 路径 | 计划文档 | 实际 |
|---|---|---|
| `/api/dashboard/candles` | 有 | ❌ 从未实现，数据在 `/snapshot` 的顶层 `candles` |
| `/api/dashboard/events` | 有 | ❌ 从未实现，数据在 `/snapshot` 的顶层 `events` |
| `/api/dashboard/stats` | 有（另一处） | ❌ 代码里没有；`progress-log` 记过一次「猜错路径 404」 |

`docs/dashboard-plan.md` 已加更正块指向本文档。

## 复现方式

```bash
python scripts/verify_public_contracts.py    # 外部契约（adapters 层）
# web 接口清单：抽出 + 打真机，见 docs/review-web-layer-r45.md 记录的方法
```

## 已知不在本清单里的东西

- **静态资源**（`/cpt/dashboard/*.js|css|html`）由 nginx 直接从
  `/var/www/cpt-dashboard/` 提供，**不经** Python。改前端要同步那个目录，
  用 `deploy/dashboard-sync.sh`（带三层校验：仓库→部署目录→线上 HTTP）。
- **认证**由 nginx `auth_basic` 做（`/etc/nginx/.htpasswd`），应用层不做认证。

---

## 附：四画布一致性契约的实测（2026-10-03）

`dashboard/canvas_registry.js` 与 `dashboard.js` 写明「四个画布必须一致：
结构元素数量两两相等」（R16-5 验收项）。R45 用 CDP 直连 chromium 实测（BTCUSDT）：

| 画布 | library | candles | fractals | bis | zhongshus | trendTypes |
|---|---|---:|---:|---:|---:|---:|
| A 手写 SVG | — | 180 | 63 | 63 | 8 | 5 |
| B | `lightweight-charts` | 180 | 63 | 63 | 8 | 5 |
| C | `plotly-finance` | 180 | 63 | 63 | 8 | 5 |
| D | `wbt.report.HtmlReportBuilder` | 180 | 63 | 63 | 8 | 5 |

⇒ **五项共享计数四画布完全一致，契约成立。**

两个必须知道的读法陷阱：

1. **画布计数 ≠ API 计数**。API 侧 `/snapshot` 返回 `candles=600 / fractals=222 /
   bis=221 / zhongshus=21 / trendTypes=9`，而画布侧是 `180 / 63 / 63 / 8 / 5`。
   差额来自**可视窗口过滤**（`applyZoomWindow`，R16-5 加的）——
   只画与当前窗口相交的结构。**两者本就不该相等**，脚本别拿它们互相比。
2. **画布 D 多两个字段**：`pending` 与 `library`。D 是**异步渲染**（先取 wbt 报告
   再写 iframe），`draw()` 在异步完成前就返回计数，所以带 `pending: true`。
   `canvas_d.js` 里有注释记着为此踩过一次（`Object.assign` 把这两个字段混进
   `counts`，首轮审计里 D 的计数多了两项）。审计脚本**必须只挑那五个键**。

## R45 新增：`/api/dashboard/a-share/recommendation`

```
GET /api/dashboard/a-share/recommendation?code=600519[&level=1d]
```

结构判断摘要：**动作 + 参考价 + 依据**。**纯确定性** ——
买卖与价格由 `cpt/application/recommendation.py` 算出，**不经 LLM**
（理由：让模型生成「买/卖 + 价格」会不可复现、不可测，且在最要紧的输出上
引入幻觉风险）。

| 字段 | 说明 |
|---|---|
| `available` | 是否有可用信号；false 时降级为「观望」并写明原因 |
| `action` / `action_label` | `buy` / `sell` / `watch` / `hold` |
| `headline` | 一句话结论，如「一买已确认」 |
| `reason` | 依据（信号状态 / 背驰 / 参考价） |
| `price` | **后复权价**（与快照同口径，用于核对结构） |
| `raw_close` | **不复权收盘价** —— 前端显示的「参考价」用这个 |
| `disclaimer` | 恒为「结构状态翻译，非投资建议」 |

**为什么需要 `raw_close`**：快照里的 K 线是**后复权价**（茅台会显示 8886，
实际约 1258）。给「买卖 + 价格」的面板一个挂不了单的数字是错的。

## R45 新增：`POST /api/dashboard/a-share/llm/summarize`

给**结构判断**配一段人话。与 `/llm/explain` 的关键差别：

| | explain | summarize |
|---|---|---|
| 喂给模型 | 完整结构明细（分型/笔/中枢） | **只有推荐那三行**（动作/结论/依据/参考价） |
| 模型能做什么 | 解释结构怎么形成 | **只能复述**，没有机会算出别的结论 |

⇒ 卡片上「确定性结果」与「人话」并排显示。模型看不到结构明细，
所以**产不出与前者矛盾的判断** —— 矛盾时没人知道该信哪个。

请求体（`Content-Type: application/json` 必需，否则 415）：

```json
{"action_label": "观望", "headline": "信号已失效",
 "reason": "一买已失效（不取反方向）", "raw_close": 1258.62,
 "price": 8886.53, "disclaimer": "结构状态翻译，非投资建议"}
```

返回 `{"available", "call_id", "status", "reason"}`，**入队即返回**（实测 74 tokens / 约 4s）。

> ⚠️ **重复提交返回的是已存在那条的真实 `call_id`**（R45 修）。
> 原来返回的是**刚生成但根本没进库**的 id ⇒ 前端拿着它轮询永远查不到，
> 表现是「明明算过，刷新一下摘要就没了」。
> **同一段代码抄了两份，bug 也抄了两份** —— `explain` 和 `summarize` 都中招。
