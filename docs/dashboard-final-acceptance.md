# CPT Dashboard 最终阶段验收报告

日期：2026-10-10
状态：未完整项已逐项收敛，等待产品人工审核

## 已交付能力

### 共享底座

- watch/research 双模式和 URL 模式切换；
- SVG K 线、成交量、分型、笔、中枢、走势类型叠加；
- 十字线 OHLCV；
- 周期选择器；
- 多级别筛选；
- 结构点击联动；
- stale/gap/offline 状态；
- 离线回放、事件时间线和 snapshot JSON 导出。

### 研究者模式

- 运行/dataset 索引；
- raw bar 逐根检查；
- merged bar 和 containment decision 溯源；
- source_ids 结构树；
- parity summary、CPT/Oracle 双 SVG 逐元素对比和差异选择定位；
- dataset/config/rules/schema/engine 复现信息；
- snapshot 字段 diff 和多运行 open_time 对齐；
- 信号历史；
- 事件 before/after/changed_fields 审计；
- 本地注释（localStorage，不进入数据集/hash）；
- 信号状态/背驰分布和转换率统计基础。
- 时间范围切片服务。

### 盯盘模式

- 当前价格和窗口涨跌幅；
- 窗口高低与成交量；
- MACD 服务；
- 收盘倒计时基础显示；
- signal status 和 divergence status；
- 没有真实 24h 聚合源时不伪造 24h 数据，返回 `None`。

## 安全与边界

- Dashboard 始终只读；
- 不提供下单、撤单、账户、持仓、盘口或结构写入；
- 不在浏览器复制 Binance HTTP 逻辑；
- oracle 只作为独立参考实现，不视为绝对正确答案；
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
```

验收结果：全部通过；当前测试总数为 206 passed，另有真实 Chromium headless smoke 2 passed。

## 已知边界

- parity 已完成标准化结果服务、summary、双 SVG 逐元素绘制、键盘/鼠标差异选择、定位字段展示和 `cpt:parity-selected` 事件；定位通过 item 的 `start_time`/`bar_index` 字段保留；
- 真实 24h 统计契约已落地：由上游注入完整聚合时显示，否则明确为 unavailable，不从窗口数据推导冒充；
- 已安装 Chromium 153.0.8010.12 ARM64 至 `$HOME/.local/bin/chromium`；真实 headless dump-dom smoke 已通过，复杂交互仍由静态契约覆盖；
- HTTP adapter 现提供 snapshot、health、reproducibility、parity、runs、market-24h、engine-state 七组只读 GET 路由；
- 已提供标准库只读 HTTP adapter；不提供任何写接口。
