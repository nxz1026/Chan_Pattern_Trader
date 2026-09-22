# CPT 参考仓库审计报告

版本：v0.1 · 2026-09-22
状态：M0-02 交付物。记录三个参考仓库的元数据、复用边界与许可证合规结论。
验证脚本：`scripts/fetch_references.sh`（一键 clone + 固定 commit 校验，见 §6）。

## 1. 参考仓库元数据

| 名称 | URL | 固定 commit | LICENSE | 用途 |
|---|---|---|---|---|
| chanlun-pro | https://github.com/yijixiuxin/chanlun-pro | `78ffa470f1e9463809d8fe2a2802e9e84b896dfe` | Apache-2.0（`LICENSE`） | 依赖级复用基础口径（分型/新笔/笔中枢/`level`/`zs_wzgx`）+ oracle 对照 |
| chanlun.py | https://github.com/YuYuKunKun/chanlun.py | `2e4fa135b19eaa201fca7bfcc8ca4a86cbde7815` | MIT（含 `NOTICE`） | 借鉴级联重建与配置序列化逻辑，不复制 |
| chanlun_pine | https://github.com/Ye-Yu-Mo/chanlun_pine | `0c028ef52fa8474b212b9a234aabbf22c3f45b4e` | GPL-3.0（`LICENSE`） | 仅视觉交叉校验，不复制代码 |

> chanlun.py 的 `NOTICE` 声明 Signal/Factor/Event 等部分逻辑源自 czsc（Apache-2.0），因此其 LICENSE 记为「MIT（含 NOTICE）」。

## 2. 复用边界

| 仓库 | 允许 | 禁止 | 仅对照 |
|---|---|---|---|
| chanlun-pro | 依赖级复用基础口径：分型/新笔/笔中枢/`level`/`zs_wzgx`；经反腐层 `adapters/reference_chanlun.py` 调用公开接口，结果映射回 CPT 领域对象 | 对象不泄漏进 `domain/`；不破解 `cl.py` 加密核心 | oracle 对照：同输入 diff 基础结构序列 |
| chanlun.py | 借鉴级联重建（尾部弹出 + 回溯重算）与配置序列化（to_dict/to_json/保存/加载/对比）的工程思路 | 不复制代码，不整体依赖 | — |
| chanlun_pine | — | 一行代码都不抄；不与 CPT 静态/动态链接 | 仅视觉交叉校验信号生命周期与图表呈现 |

## 3. 许可证合规要点

- **Apache-2.0（chanlun-pro）**：保留原版权声明与 LICENSE；对修改过的部分需注明修改。依赖级复用（非复制源码进仓库）不触发传染义务。
- **MIT + NOTICE（chanlun.py）**：保留 `NOTICE` 与版权声明即可；MIT 无传染性，CPT 不因借鉴逻辑而改变自身许可证。
- **GPL-3.0（chanlun_pine）**：强传染。CPT 必须与 chanlun_pine 保持零静态/动态链接、零代码复制，仅做**离线视觉对照**。若违反（引入其代码或链接其产物），将触发 GPL-3.0 的源码分发义务，污染 CPT 整体许可证。

## 4. chanlun-pro 加密核心风险

`chanlun-pro/src/chanlun/cl_interface.py` 中以下两个函数设计为**可覆盖**，是 CPT 注入自定义背驰口径的合法扩展点：

- `query_macd_ld(cd, start_fx, end_fx)` — 背驰力度（MACD 柱面积）查询；
- `compare_ld_beichi(one_ld, two_ld, line_direction)` — 背驰比较。

CPT 不直接破解加密核心 `cl.py`；而是通过 `user_custom_mmd` 扩展点注入 CPT 一买对照逻辑，实现「用 CPT 口径验证 chanlun-pro 结构序列」的 oracle 对照，避免触碰加密实现。

## 5. PyPI MIT 版 chanlun 旧包验证

**结论：不可用（M0-04 实测）**。PyPI 上不存在「MIT 许可的旧版 Python `chanlun` 包」；当前该包名指向作者 YuYuKunKun 的 **Rust 重写版 `chanlun.rs`**（PyO3 绑定），与旧 Python 项目 `chanlun.py`（固定 `2e4fa135`）不是同一发行物。

### 5.1 PyPI 发行事实（https://pypi.org/pypi/chanlun/json）

| 项 | 值 |
|---|---|
| 最新版本 | `2606.73`（upload 2026-06-12） |
| 全部版本 | 仅 10 个：`2605.11`/`2605.46`/`2605.86`/`2605.94`/`2605.101`/`2605.103`/`2606.17`/`2606.44`/`2606.47`/`2606.73`，全部 2026-05-26→2026-06-12 上传 |
| 作者 | YuYuKunKun |
| summary | 「缠论技术分析库 — Rust 高性能实现」 |
| `license` / `license_expression` 元数据字段 | **null（核心元数据未声明）** |
| classifier | `License :: OSI Approved :: MIT License` |
| sdist 内 LICENSE | 存在（`license_files: ["LICENSE"]`） |
| 源码仓库 | https://github.com/YuYuKunKun/chanlun.rs |

### 5.2 关键事实链

1. **无「旧版 Python 包」**：全部 10 个 release 均为 Rust 重写版；旧 Python 项目 `chanlun.py` 仓库（固定 commit）无 `setup.py`/`pyproject.toml`，从未作为 pip 包发布。其 README 的 PyPI badge 指向的正是同一个 `chanlun`（Rust 版）包，即该包名已被 Rust 重写版「接管」。另查 `chanlun-py`/`chanlunpy`/`chanlun_py` 三个相近包名，均 404，旧 Python 代码未在 PyPI 任何名下发过。
2. **同作者近亲而非同源**：`chanlun.rs` 与 `chanlun.py` 同作者；Rust README 明确「API 参考 `chan.py` 设计，高度兼容」「类名/方法名/字段名与 `chan.py` 保持一致」，是 `chan.py` 的 Rust 重写（PyO3 绑定），非旧 Python 源码的再发行。
3. **许可证更干净但字段未声明**：`chanlun.rs` 仓库根 `LICENSE` 为纯净 MIT，**无 `NOTICE`、无 czsc（Apache-2.0）痕迹、无加密核心**；但 PyPI 核心元数据 `license`/`license_expression` 为 null，仅靠 classifier + LICENSE 文件声明，元数据卫生欠佳。

### 5.3 算法覆盖（Rust 版 `chanlun.rs`）

| 口径 | Rust 模块 | 覆盖 |
|---|---|---|
| 分型 fx | `structure/fractal_obj.rs` | ✅ |
| 笔 bi | `algorithm/bi.rs` | ✅ |
| 中枢 zs | `algorithm/hub.rs` | ✅ |
| 线段 | `algorithm/segment.rs` | ✅ |
| 背驰 | `algorithm/divergence.rs` | ✅ |
| 买卖点 | `business/bsp.rs` | ✅ |
| **走势类型 trend_type** | —（无该模块，`lib.rs` 无导出） | ❌ |

> 走势类型在旧 `chan.py` 中本就残缺：`走势.分析`/`_同级分解`/`_非同分解` 均为 `pass` 桩，仅 `走势.日内分类`（第 46 课）有实现；Rust 重写未移植该概念。

### 5.4 评估结论

- **不可作为「旧版 Python MIT oracle」**：前提不成立——PyPI 上不存在 MIT 许可的旧版 Python `chanlun` 包；`chanlun` 包名自 2026-05 起即指向 Rust 重写版。
- **作为「更干净 oracle」的附带判断**：即便退而考虑 Rust 版 `chanlun`（`pip install chanlun`，MIT、无加密核心、无 czsc 传染），它相对 chanlun-pro（Apache-2.0 + 加密 `cl.py`）确有许可证优势，但存在两点阻断：
  1. **形态**：编译型 Rust wheel（PyO3 绑定），非 Python 源码；无法像 chanlun-pro 那样直接读源码做口径 diff，只能通过 Python API 调用做黑盒对照。
  2. **覆盖不全**：缺 `走势类型`，且不含 chanlun-pro 的 `level`/`zs_wzgx` 等对照口径，无法覆盖现有 oracle 对照面。
- **遗留 TODO（不阻塞 M0）**：若 M2 需 MIT oracle，Rust 版 `chanlun` `2606.73` 是候选，但需补齐走势类型对照并接受 PyO3 调用形态；本结论留待 M2 oracle 切换策略评估。

## 6. 验证脚本

- `scripts/fetch_references.sh`：一键 clone 三个仓库到 `references/` 并 checkout 到固定 commit；已存在则 fetch + checkout；最后打印每个仓库实际 HEAD 与期望 commit 比对，不一致则退出码非 0；并打印各仓库 LICENSE 路径供审计。
