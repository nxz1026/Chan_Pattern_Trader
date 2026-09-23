# CPT M2 Oracle 诊断对照

生成方式：`python scripts/compare_oracle.py --data-dir tests/fixtures/oracle --output docs/m2-oracle-diagnostic.md`

> **诊断结果，不是规则等价验收。** CPT M1 后端是用于 fixture 的极简占位实现；本报告量化其与独立 Rust 实现的差异，不据此评价正式 CPT 算法正确性，也不宣称 M2 完成。

## 范围与复现

- 标的：`BTCUSDT` 永续；周期：`5m`。
- 行情来源：[Binance USDⓈ-M K 线 API](https://fapi.binance.com/fapi/v1/klines)；每个窗口冻结 1000 根完整、连续 K 线 CSV。
- Oracle：`PyPI chanlun==2606.73 (MIT, Rust/PyO3)`；通过 PyO3 独立运行，不调用 CPT 占位算法生成 oracle 结果。安装：`.venv/bin/pip install -e '.[oracle]'`。
- 原计划 chanlun-pro 固定版：`78ffa470f1e9463809d8fe2a2802e9e84b896dfe`；状态：未执行（本机运行时明确报“缺少运行许可文件”）。
- CPT 对照对象：`InMemoryChanlunBackend`；每根普通 K 线严格使用相同原始 OHLCV 输入。
- 分型精确匹配键：原始 K 线索引、顶/底类别、特征价格（8 位小数）。笔中枢仅报告数量，不声称概念或映射完全等价。

## 结果

| UTC 窗口起始 | K线数 | 缠K数 (oracle) | 分型 oracle / CPT | 精确匹配 / oracle-only / CPT-only | 笔 oracle / CPT | 笔中枢 oracle / CPT |
|---|---:|---:|---:|---:|---:|---:|
| 2024-02-01T00:00:00+00:00 | 1000 | 704 | 58 / 287 | 39 / 19 / 248 | 57 / 286 | 9 / 0 |
| 2024-09-01T00:00:00+00:00 | 1000 | 744 | 62 / 333 | 50 / 12 / 283 | 61 / 332 | 8 / 0 |
| 2025-04-01T00:00:00+00:00 | 1000 | 774 | 60 / 357 | 50 / 10 / 307 | 59 / 356 | 8 / 0 |

## 差异归因与解释边界

1. **CPT 分型/笔数量较多属于预期的实现差异**：M1 占位仅检查原始 K 线左右邻居的严格高低点，并做同类极值替换；Rust oracle 先做包含合并，再运行自己的分型、新笔递归算法。两套算法不是可互换实现。
2. **CPT 笔中枢恒为 0 是占位后端的明确能力缺口**：`InMemoryChanlunBackend` 文档说明不计算中枢；Rust 结果来自独立笔中枢序列。不能把此差异解释为 oracle 错误。
3. **索引坐标不同**：Rust 分型中心的 `原始起始序号` 指回合并缠K包含的原始K线；CPT 占位索引是未经包含处理的普通K线。匹配使用对应原始 K 线索引，但被合并的 candle 不保证一一映射。
4. **仍需正式算法才能逐项归因**：当前占位版本没有缠K包含规则、新笔规则或中枢结果；本诊断只能将差异归因为已知实现边界，不覆盖“CPT 正式算法 vs oracle”的逐结构 M2 审计。
5. **替代 oracle 的口径边界**：PyPI MIT Rust 版用于黑盒对照；其规则/API 与 chanlun-pro 配置字段并非天然等价，不能验证 `level` / `zs_wzgx` 等 chanlun-pro 专属口径。

## 快照校验

| 文件 | SHA-256 | 最后一根开盘时间 (UTC) |
|---|---|---|
| `btcusdt_5m_2024-02-01.csv` | `b954140fb943b3e3b78078718aeeb08e1dee9ab6d6ecd9f8c69b42e9f814beb4` | 2024-02-04T11:15:00+00:00 |
| `btcusdt_5m_2024-09-01.csv` | `7bc13ca07279d3d3b68e853aefc115eac0e4bd3572d9fd537c980183ddf42a89` | 2024-09-04T11:15:00+00:00 |
| `btcusdt_5m_2025-04-01.csv` | `692236dcb7979aed1707d12502d14400601f4648d451ac6bed0e1c9aa15dbd6c` | 2025-04-04T11:15:00+00:00 |

快照可离线复跑；重新抓取网络数据会产生新快照哈希，不覆盖原快照，需由调用者显式确认后替换。

## 外部证据

- [PyPI chanlun 2606.73 项目页与 API 示例](https://pypi.org/project/chanlun/2606.73/)：Rust/PyO3 绑定、Python 3.9+、MIT。
- [chanlun.rs 仓库](https://github.com/YuYuKunKun/chanlun.rs)：公开说明其 MIT 许可证、Python 绑定及参考 chan.py API。
- [Binance USDⓈ-M K 线接口](https://binance-docs.github.io/apidocs/futures/en/#kline-candlestick-data)：公开 K 线源。

## 踩坑记录

- 当前 CPT venv 为 Python 3.14、Linux aarch64；PyPI 未提供可直接使用的 wheel，本次通过包构建后端编译成功。首次构建因用户缓存目录不可写失败；改用已允许的环境后重试安装成功。安装依赖组已记录为 `oracle` optional extra。
- chanlun-pro 的 `cl.py` 可见文本仅有 PyArmor 启动封装；`import chanlun.cl` 实跑失败：`RuntimeError: 缺少运行许可文件 (1:10672)`。本诊断不尝试绕过授权。
- oracle 的分型/笔字段来自中文 PyO3 扩展；时间戳单位为秒、Binance 输入为毫秒。导出映射统一为毫秒，避免 1000 倍偏差。
