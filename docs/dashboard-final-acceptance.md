# CPT Dashboard 最终阶段验收报告

日期：2026-10-23
状态：审计报告全部整改项已闭合，逐项有代码+测试+文档证据。

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
- parity summary、CPT/Oracle 双 SVG 逐元素对比和差异选择定位；
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

- Dashboard 始终只读；
- 不提供下单、撤单、账户、持仓、盘口或结构写入；
- 不在浏览器复制 Binance HTTP 逻辑；
- oracle 只作为独立参考实现，不视为绝对正确答案；无 oracle 数据时 parity 显式 `available: false`，不再假装 0% 匹配（A2）；
- 本地研究注释不改变原始 snapshot、dataset hash 或结构数据。

## 自动化验收

```text
pytest tests -q -rs
ruff check cpt tests scripts/compare_oracle.py
ruff format --check cpt tests scripts/compare_oracle.py
mypy cpt
import-linter lint --config .importlinter
node --check dashboard/dashboard.js
git diff --check
vulture cpt scripts/compare_oracle.py --min-confidence 80 \
    --exclude 'cpt/web/app.py:77'
```

验收结果：全部通过；当前测试总数 160 passed，vulture 仅命中 `cpt/web/app.py:77`（`BaseHTTPRequestHandler.log_message` 标准签名，保留）。

## 已知边界

- 多级别真实叠加已通过递归链（classify_trend → map_trend_types → detect_fractals → build_bis/build_zhongshus）端到端跑通；最小级别由 `RulesConfig.levels[0]` 驱动，高级级别是递归链产出（target_level 由调用方传入）。默认 `RulesConfig.levels = (5, 30)` → 多级别返回 `{5: {...}, 30: {...}}`。
- HTTP adapter 现提供 snapshot、health、reproducibility、parity、runs、market-24h、engine-state、inspect 八组只读 GET 路由；snapshot 支持 `level` 查询参数触发多级别重算。
- 已安装 Chromium 153.0.8010.12 ARM64 至 `$HOME/.local/bin/chromium`；真实 headless dump-dom smoke 已通过。
- vulture 已接入 GitHub Actions CI（min-confidence 80）；dead-code 审计进入持续门禁。
- `cpt/llm/` 死代码已删除（审计 A1 处置）；架构文档保留 LLM 层蓝图但标注"预留，未实现"。
