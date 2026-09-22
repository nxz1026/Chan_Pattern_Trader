# CPT 参考仓库

本目录保存 CPT 规则设计阶段使用的外部参考实现。它们用于审计、对照和测试，不直接构成 CPT 规则标准。

| 仓库 | 用途 | 固定提交 |
|---|---|---|
| [yijixiuxin/chanlun-pro](https://github.com/yijixiuxin/chanlun-pro) | Python 缠论结构、配置、多周期策略参考 | `78ffa470f1e9463809d8fe2a2802e9e84b896dfe` |
| [YuYuKunKun/chanlun.py](https://github.com/YuYuKunKun/chanlun.py) | Python/C99 多层结构和增量重建参考 | `2e4fa135b19eaa201fca7bfcc8ca4a86cbde7815` |
| [Ye-Yu-Mo/chanlun_pine](https://github.com/Ye-Yu-Mo/chanlun_pine) | TradingView Pine 单周期可视化参考 | `0c028ef52fa8474b212b9a234aabbf22c3f45b4e` |

## 使用边界

- 不复制第三方代码到 CPT 核心，先核对许可证和实现差异。
- 参考仓库的默认参数不等于 CPT 规则。
- 重点审阅：包含关系、分型、笔、线段、中枢、买卖点、重构和多周期机制。
- 每次升级参考版本必须更新本表和对应审计记录。
