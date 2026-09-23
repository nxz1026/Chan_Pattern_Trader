# CPT M6 质量验收报告

日期：2026-09-23

## 1. 自动化验收

| 检查 | 结果 |
|---|---:|
| `pytest tests -q` | 107 passed |
| `ruff check cpt tests scripts/compare_oracle.py` | All checks passed |
| `ruff format --check cpt tests scripts/compare_oracle.py` | 46 files already formatted |
| `mypy cpt` | 27 source files，无问题 |
| `import-linter lint --config .importlinter` | 5 contracts kept，0 broken |
| `git diff --check` | 通过 |

## 2. 已验收能力

- M1 基础结构：缠K包含、三根分型、新笔候选、三笔重叠笔中枢。
- M2 对照：三段各 1000 根 BTCUSDT 永续 5m 冻结快照；Rust `chanlun==2606.73` 作为独立对照实现。
- M3：走势类型分类、一级递归结构元素、一买状态机、尾部重构事件和版本化依赖扫描。
- M4：CanonicalBar 去重/连续性/OHLC 校验、Binance Futures 窄接口、批量和前缀回放。
- M5：实时窗口、未收盘 alert、收盘 confirmed、窗口截断和冲突/缺口拒绝。
- schema v1 导出及既有人工 fixture 的稳定哈希测试保持通过。

## 3. 已知限制

1. 历史人工 fixture 仍使用早期 600ms 演示时间边界；它们通过兼容低层 `run_replay()` 测试，不代表 Binance 5m 契约。真实数据和新 M4 入口要求 `close_time = open_time + interval_ms - 1`。
2. Rust 参考实现与 CPT 不是同一规则实现；M2 报告是诊断对照，不是算法等价证明。
3. `chanlun-pro` 固定版本仍因运行许可不可执行。
4. `scan_stale_dependents()` 只能识别 `source_id@rN` 版本化引用；裸 source id 无法推断依赖版本。
5. 当前一买模块只实现确定性状态机和输入门槛，MACD 背驰计算尚未接入。
6. 当前实时引擎每次收盘使用窗口重建，未做增量性能优化；窗口上限控制成本。
7. LLM 独立服务层（M-LLM）尚未实现，且不影响核心结构链路。
8. M4 回放新入口尚未把旧人工 fixture 自动迁移为真实 5m 时间契约。

## 4. 结论

CPT 当前主线 M0-M5 的已实现部分通过全量自动化质量门；M6 的测试报告、差异报告和已知限制已落盘。剩余工作是针对上述限制的后续迭代，不应在本报告中宣称 M3/M4/M5 已达到生产交易系统级别。
