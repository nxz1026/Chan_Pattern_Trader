# CPT 参考仓库审计报告

版本：v0.2 · 2026-09-24（R13/R14 更新）
状态：**活文档**。与 `references/` 下实际 checkout 的 commit 必须一致；`scripts/fetch_references.sh` 是唯一固化入口。
v0.1（2026-09-22，M0-02）的 chanlun-pro / chanlun.py / chanlun_pine / chan.py 部分已按 G1 决议删除，见 §4。

## 1. 当前引用（2 个）

| 名称 | URL | 固定 commit | LICENSE | 用途 |
|---|---|---|---|---|
| czsc | https://github.com/waditu/czsc | `701e480a545004f945bb1721e510ae610ad90c4c` | Apache-2.0（`LICENSE`） | **可选依赖** extra `chan = ["czsc==1.0.1"]`，经反腐层复用分型/笔/力度度量/一买谓词 |
| wbt | https://github.com/zengbin93/wbt | `39bb1e8ab7db71cce2dcea24150639e9470a4ed4` | MIT（`LICENSE`） | 仅作可视化与回测参考（R16 画布 D），不依赖、不复制 |

两者均已 `.gitignore`（`/references/czsc/`、`/references/wbt/`），仓库里只保留本审计与固化脚本。

## 2. czsc：复用边界

### 2.1 复用内容

| 环节 | 说明 |
|---|---|
| 缠论K线包含处理 | `remove_include` |
| 三根分型 | 直接取 `analyzer.fx_list` |
| 新笔判定 | 直接取 `analyzer.bi_list`，门槛由 `min_bi_len` 控制 |
| 笔的力度度量 | `BI.power_price` / `power_volume` / `length` |
| 一买/一卖结构谓词 | `check_first_buy` / `check_first_sell`，**逐行移植**为 `cpt/domain/first_buy.py` |

### 2.2 明确不复用

| 环节 | 原因（实测） |
|---|---|
| 中枢 | `zs_list` 会产出 **<3 笔的假中枢**（fixture3 有 2 个两笔中枢，其中一个还是首个），且 `ZS` 无 `bi_ids` 笔级溯源。CPT 用自己的 `build_zhongshus`（严格三笔重叠 + ≥3 笔 + 延伸不收缩，见 `rules.md` §9.9） |
| 走势类型 / 线段 / 递归 | czsc 无这些能力 |
| 信号模板体系 | `call_signal` 需要分析器对象 + 模板名 + 参数字典；CPT 需要的是"输入一串笔、输出布尔值"的纯谓词 |

> 补充实测：czsc 的 `zs_list` **不做** `is_valid()` 过滤（`crates/czsc-core/src/analyze/mod.rs`），
> 但 `is_valid() == False` 与 `zg < zd` 在 3 个 fixture 上**均为 0**——真正的缺陷只有
> "产出 <3 笔假中枢"这一条。`ZS::new` 用 `take(3)`：前三笔定 `zg`/`zd`，延伸不收紧。
> 这与 CPT 修正后的中枢口径一致，已作为交叉验证依据（见 `progress-log.md` R14-2）。

### 2.3 接入形态：为什么是可选依赖（方案 C）

用户 2026-09-24 定：**可选依赖 extra**，`dependencies = []` 必须保持为空。三个备选方案被实测否决：

| 方案 | 实测结论 |
|---|---|
| A 无条件 `pip install czsc` | 会拉入 pandas / numpy / pyarrow / polars / scipy / statsmodels / openpyxl / requests，破坏 `dependencies = []` |
| B vendor `_native.abi3.so` | **46.6 MB**（超 GitHub 50 MB 警告线），且**半废**——取 `FX.dt` / `BI.sdt` 直接 `ModuleNotFoundError: pandas`（`crates/czsc-core/src/objects/fx.rs` 的 `create_naive_pandas_timestamp` 是硬依赖） |
| D 移植 273 行 Rust | 可行但自担维护；优先复用上游 |

落地：`pyproject.toml` 的 `[project.optional-dependencies] chan = ["czsc==1.0.1"]`，
适配器内**延迟导入 + 版本校验**（`CzscNotInstalledError` / `CzscVersionError`），
未装 czsc 时核心功能与全部既有测试不受影响。

### 2.4 版本固定

- `cpt/adapters/czsc_chanlun.py` 的 `PINNED_CZSC_VERSION = "1.0.1"`
- `cpt/adapters/reference_chanlun.py` 的 `_CZSC_FIXED_COMMIT`
- 测试 `test_pinned_version_matches_pyproject_extra` 与 `test_core_dependencies_stay_empty` 钉住这两条不变式

### 2.5 已知副作用

安装 czsc 会**附带**引入 `wbt-0.9.1` 与 `plotly-7.1.0`（czsc 声明的依赖）。这两者正好可用于 R16 的画布 C/D。

## 3. wbt：复用边界

仅作**可视化与回测口径参考**（R16 画布 D）。CPT 不 import wbt、不复制其代码，只参考它如何组织报告与回测指标。

## 4. 已移除的参照（2026-09-24，G1 决议）

| 仓库 | 原固定 commit | LICENSE | 移除原因 |
|---|---|---|---|
| chanlun-pro | `78ffa470f1e9463809d8fe2a2802e9e84b896dfe` | Apache-2.0 | 分型/笔实现与缠论定义冲突（见 §4.1） |
| chanlun.py | `2e4fa135b19eaa201fca7bfcc8ca4a86cbde7815` | MIT | 只提供借鉴价值，带来不可核验的溯源负担 |
| chanlun_pine | `0c028ef52fa8474b212b9a234aabbf22c3f45b4e` | GPL-3.0 | 强传染性；仅视觉对照，价值不足 |
| chan.py | 未固定 | MIT | 同上；未固定 commit，故从未实际引用 |

一并删除的还有：`oracle` 可选依赖、`oracle-parity` CI 作业及其在 `.github/workflows/ci.yml` 中的对照步骤、`references/chanlun*/` 目录。

### 4.1 chanlun-pro 移除的实测依据

| 指标 | chanlun-pro（已移除） | czsc（现用） |
|---|---|---|
| 笔端点跨度**中位** | **2 根**原始K线 | **9–10 根** |
| 跨度 <4 根占比 | **66.9% / 74.4% / 73.0%** | **6.1% / 4.2% / 6.2%** |
| 分型 / 笔 / 中枢（fixture1） | 327 / 326 / 43 | 202 / 50 / 6 |

两根 3K 分型窗口重叠，定义上不可能成笔。差距是**定义级**的，不是参数级的——因此移除而不是调参。

### 4.2 PyPI `chanlun` 包的历史结论（保留备查）

M0 曾验证「PyPI 上是否存在 MIT 许可的旧版 Python `chanlun` 包」。**结论：不存在**。
该包名自 2026-05 起指向同作者 YuYuKunKun 的 Rust 重写版 `chanlun.rs`（PyO3 绑定），
与旧 Python 项目 `chanlun.py` 不是同一发行物。相近包名 `chanlun-py` / `chanlunpy` /
`chanlun_py` 均 404，旧 Python 代码未在 PyPI 任何名下发过。

该结论随 §4 一并作废（不再需要 oracle），仅作历史记录。Rust 版 `chanlun.rs` 虽许可证更干净
（纯 MIT、无加密核心、无 czsc 传染），但形态为编译 wheel、无法直接读源码做口径 diff，
且缺 `走势类型` 模块——即使当初保留也不足以覆盖对照面。

## 5. 许可证义务

- **Apache-2.0（czsc）**：保留原版权声明与 `LICENSE`；对修改过的部分须注明修改。
  CPT 的 `cpt/domain/first_buy.py` 是从 czsc `crates/czsc-signals/src/utils/cxt.rs`
  **逐行移植**的，已在模块 docstring 中注明来源与许可证。
- **MIT（wbt）**：无传染性；CPT 未复制其代码。
- **CPT 自身许可证不受影响**：可选依赖（非复制源码进仓库）不触发传染义务。

## 6. 校验方式

```bash
# 固化引用（clone + checkout 到固定 commit）
scripts/fetch_references.sh

# 核对本地 commit 与本文档一致
for d in references/*/; do git -C "$d" rev-parse HEAD; done
```

期望输出（顺序与 §1 表格一致）：

```text
701e480a545004f945bb1721e510ae610ad90c4c
39bb1e8ab7db71cce2dcea24150639e9470a4ed4
```
