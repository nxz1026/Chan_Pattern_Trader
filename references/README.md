# CPT 参考仓库

本目录保存 CPT 使用的两个外部参考实现。它们用于审计、对照和测试，不直接构成 CPT 规则标准。

| 仓库 | 用途 | 固定提交 | 许可证 |
|---|---|---|---|
| [waditu/czsc](https://github.com/waditu/czsc) | 分型/笔的参考实现（Rust 内核 + 信号引擎）；一买/二买/三买判定的移植来源 | `701e480a545004f945bb1721e510ae610ad90c4c` | Apache-2.0 |
| [zengbin93/wbt](https://github.com/zengbin93/wbt) | 回测报告 HTML 模板参考（四画布之 D） | `39bb1e8ab7db71cce2dcea24150639e9470a4ed4` | MIT |

拉取与校验：`scripts/fetch_references.sh`（`git clone --no-checkout` + checkout 到上表 commit，HEAD 不符即非 0 退出）。

## 使用边界

- 不复制第三方代码到 CPT 核心，先核对许可证和实现差异。
- 参考仓库的默认参数不等于 CPT 规则。
- czsc 只提供分型/笔/中枢/买卖点信号；**走势类型、线段、递归 czsc 没有**，CPT 必须自研。
- 每次升级参考版本必须更新本表和对应审计记录。

## 已移除的参照（2026-09-24）

`chanlun-pro`（Apache-2.0）、`chanlun.py`（MIT）、`chanlun_pine`（GPL-3.0）三个参照已按 G1 决议移除：
其分型/笔实现与缠论定义冲突（笔端点中位跨度仅 2 根原始K线，66–74% 的笔跨度 < 4 根，两根 3K 分型窗口重叠，定义上不可能成笔），
不具备作为参照的价值。同时移除的还有 `chanlun==2606.73` 依赖与全部 oracle 对照基建（F2）。
