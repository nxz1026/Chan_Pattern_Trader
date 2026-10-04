# CPT Dashboard 最终阶段验收报告

日期：2026-09-25
状态：**历史快照** —— 下列能力清单与验收数字均为当时实况，保留仅为溯源。

> ## R45 新增（2026-10-04）：结构判断摘要卡
>
> 本文是 2026-09-25 的验收快照，但下面这块是**现状**、不写在这里就无处可查。
>
> | | |
> |---|---|
> | 接口 | `GET /api/dashboard/a-share/recommendation?code=&level=` |
> | 逻辑 | `cpt/application/recommendation.py` —— **买卖与价格纯确定性**，不经 LLM |
> | LLM 摘要 | `POST /api/dashboard/a-share/llm/summarize`（只喂那三行事实） |
> | 信号历史 | 同一响应里的 `history`，按因子口径分「旧 / 新」 |
> | 数据质量 | K 线不足 30 根 ⇒ 显示「数据不足」，**不与「无信号」混淆** |
> | 真机复盘 | `docs/review-dashboard-r45.md` |
>
> 完整字段见 `docs/web-api-reference.md`。

> ## ⚠️ 不要再把本文当现状
>
> 1. **原文件头写的日期是 2026-10-23，比它自己最后被修改的时间还晚三周**，
>    且内容是 511 passed（R21 的数）—— 属明显笔误，已按文件真实 mtime 更正。
> 2. **「审计报告全部整改项已闭合」这句话不准确**，已在
>    [`audit/cpt-code-audit-20260930.md` §0.0](./audit/cpt-code-audit-20260930.md)
>    回填真实销账表：**M3（canvas iframe 信任边界）至今仍开放**。
> 3. **「Dashboard 始终只读」已不成立** —— R20 之后加了 A 股自选写接口
>    `POST/DELETE /api/dashboard/a-share/watchlist`（需 `Content-Type: application/json`，
>    否则 415）。`README.md`「当前状态」一节也有同样的过时说法。
> 4. 验收数字、路由条数均已过时。**现行门禁见 `README.md`「质量门」**。
> 5. 24h 数据的说法与 `docs/dashboard-product-roadmap.md` 冲突 —— 本文说
>    「已接入」，roadmap 说「仍是硬编码」。以 `cpt/web/__main__.py:844 _safe_24h_for`
>    为准：realtime 模式有真实 `fetch_24h_ticker`，fixture/demo 模式没有。

## 已交付能力

### 共享底座

- watch/research 双模式和 URL 模式切换；
- SVG K 线、成交量、分型、笔、中枢、走势类型叠加；
- 十字线 OHLCV；
- 周期选择器；
- 多级别真实叠加（按 `level` 查询参数后端重算递归链）；
- 结构点击联动；
- stale/gap/offline 状态；
- 离线回放、事件时间线和 snapshot JSON 导出；
- 图表缩放（`installZoomControls` + 滚轮/dblclick/按钮，max ×4）；
- MACD 副图（EMA 12/26/9 + histogram ×2）；
- 最新价标记线（横虚线 + 价格标签 + 未收盘 alert 状态）。

### 研究者模式

- 运行/dataset 索引；
- raw bar 逐根检查；
- **B3 逐根检查器接后端 inspect 端点**：调用 `trace_containment` 产出 `containment_chain`（决策 + resulting_high/low + direction），非手工常量；
- merged bar 和 containment decision 溯源；
- source_ids 结构树；
- dataset/config/rules/schema/engine 复现信息；
- snapshot 字段 diff 和多运行 open_time 对齐（`config_compare` 由 `compare_configs(default, current)` 注入）；
- 信号历史；
- 事件 before/after/changed_fields 审计；
- 本地注释（localStorage，key = symbol + level + kind + start_time/bar_index，不进入数据集/hash）；
- 信号状态/背驰分布和转换率统计基础；
- 时间范围切片服务；
- 多级别结构注入（`snapshot.multi_level.levels` 含每级别 fractals/bis/zhongshus 计数）。

### 盯盘模式

- 当前价格和窗口涨跌幅（**24h 真实数据由 `fetch_24h_ticker` + `normalize_24h` 接入，窗口/24h 双标签语义**）；
- 窗口高低与成交量；
- MACD 服务；
- 收盘倒计时基础显示；
- signal status 和 divergence status；
- **浏览器通知 + 蜂鸣提醒**：realtime 模式下 alert triggered 时 WebAudio 蜂鸣 + `Notification` API（用户手势授权）；
- 没有真实 24h 聚合源时不伪造 24h 数据，返回 unavailable；
- 不在 realtime 模式下显示提醒/刷新按钮（C5 占位处理）。

## 安全与边界

- Dashboard 的**行情与结构数据只读** —— 不提供下单、撤单、账户、持仓、盘口或结构写入；
- ⚠️ **但自选列表可写**：`POST/DELETE /api/dashboard/a-share/watchlist`。
  处置是要求 `Content-Type: application/json`（否则 415），用来抬高跨站触发门槛，
  **不引入鉴权系统**。单用户看板经 nginx 暴露的前提下这是刻意取舍，
  不是遗漏 —— 但也别把「只读」写进对外承诺里；
- 不在浏览器复制 Binance HTTP 逻辑；
- 本地研究注释不改变原始 snapshot、dataset hash 或结构数据。

## 自动化验收

> 以下命令为 2026-09-25 当时的门禁。**现行门禁以 `README.md`「质量门」为准**
> （6 条，3.12 + 3.14 双版本，vulture 阈值 60，`scripts/` 已纳入）。

```text
pytest tests -q -rs
ruff check cpt tests scripts
ruff format --check cpt tests scripts
mypy cpt scripts
lint-imports
node --check dashboard/dashboard.js
git diff --check
vulture cpt --min-confidence 60 whitelist.py
```

验收结果（2026-09-25 当时）：全部通过；测试总数 **511 passed, 29 skipped**，vulture 0 告警（whitelist.py 逐条登记）。此数字已过时，勿引用。

## 已知边界

- 多级别真实叠加已通过递归链（classify_trend → map_trend_types → detect_fractals → build_bis/build_zhongshus）端到端跑通；最小级别由 `RulesConfig.levels[0]` 驱动，高级级别是递归链产出（target_level 由调用方传入）。默认 `RulesConfig.levels = (5, 30)` → 多级别返回 `{5: {...}, 30: {...}}`。
- HTTP adapter 的**只读 GET 路由**至少包含 snapshot、health、reproducibility、runs、market-24h、engine-state、inspect
（⚠️ R45 实测：这是当时的快照，**当前共 27 个接口**，含 2 个写接口。完整清单见 `docs/web-api-reference.md`）；snapshot 支持 `level` 查询参数触发多级别重算。
- 已安装 Chromium 153.0.8010.12 ARM64 至 `$HOME/.local/bin/chromium`；真实 headless dump-dom smoke 已通过。
- vulture 已接入 GitHub Actions CI（min-confidence 60，whitelist.py 逐条登记）；dead-code 审计进入持续门禁。
- `cpt/llm/` 死代码已删除（审计 A1 处置）；架构文档保留 LLM 层蓝图但标注"预留，未实现"。
