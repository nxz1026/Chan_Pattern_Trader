# CPT — Chan Pattern Trader

一个以**学习金融与软件工程**为目标的虚拟货币缠论分析系统。

CPT 基于缠中说禅理论，对 Binance BTCUSDT 永续合约行情做结构分析：缠论K线 → 分型 → 新笔 → 笔中枢 → 走势类型，并由低级别走势类型递归生成高级别笔，最终输出一买信号（预警，不下单）。

## 设计原则

1. **结构清晰**：单一职责分层，依赖方向由 import-linter 在 CI 强制。
2. **虚拟/物理分离**：缠论算法（domain/engine）不知道交易所、数据库、绘图、LLM 的存在。
3. **规则先行**：所有规则口径先冻结于 `docs/rules.md`，再写代码；配置集中在可序列化的 `RulesConfig`。
4. **无未来函数**：实时输出只依赖当前时点之前的数据，候选/确认/失效全程可追溯。
5. **可复现**：同一份输入数据 + 同一份配置，必须得到同一份结构结果。
6. **高复用率**：基础口径经反腐层委托给固定版本的 chanlun-pro；CPT 只自研参考仓库都未覆盖的核心（走势类型、递归映射、一买状态机）。

## 架构速览

```text
application/   用例编排（回放 / 查询 / 导出 / LLM 用例）
engine/        有状态编排（历史模式 / 实时模式 / 重构 / 事件）
domain/        纯算法与领域模型（零第三方依赖，一套算法跨级别复用）
adapters/      外部数据接入（Binance、反腐层 reference_chanlun）
storage/       当前状态 + 不可变事件 + 信号（SQLite + JSON）
llm/           独立 LLM 服务层（可关闭、可审计、永不回写结构）
```

详细设计见 `docs/architecture.md`。

## 文档导航

| 文档 | 内容 |
|---|---|
| `docs/rules.md` | 规则口径唯一事实来源（含 §9 已冻结约定） |
| `docs/architecture.md` | 软件结构设计（分层、模块、数据模型、复用映射、测试策略） |
| `docs/implementation-plan.md` | 实施计划（M0–M6 垂直切片里程碑 + M-LLM 独立线） |
| `docs/reference-audit.md` | 参考仓库许可证与复用边界 |
| `docs/m2-oracle-diagnostic.md` | M2 诊断对照：固定版真实行情快照与 MIT Rust oracle（非算法等价验收） |

## 参考仓库

| 仓库 | 固定版本 | 许可证 | 用途 |
|---|---|---|---|
| [chanlun-pro](https://github.com/yijixiuxin/chanlun-pro) | `78ffa470` | Apache-2.0 | 依赖级复用基础口径（分型/新笔/笔中枢）+ oracle 对照 |
| [chanlun.py](https://github.com/YuYuKunKun/chanlun.py) | `2e4fa135` | MIT | 借鉴级联重建与配置序列化逻辑 |
| [chanlun_pine](https://github.com/Ye-Yu-Mo/chanlun_pine) | `0c028ef` | GPL-3.0 | 仅视觉交叉校验，不复制代码 |

固化脚本：`scripts/fetch_references.sh`（M0 交付，待补）。

## 当前状态

设计阶段完成，代码未开始。下一步见 `docs/implementation-plan.md` §12（M0：固化基线 + import-linter + PyPI MIT 版 chanlun 验证）。

首版非目标：自动下单、线段、多交易所、Web UI、LLM 参与结构判断。

## 风险声明

本项目仅用于个人学习与技术研究，不构成任何投资建议。金融市场存在风险，缠论信号（含一买）的历史表现不代表未来收益；任何实盘决策请独立判断并自行承担风险与责任。
