# CPT 进度日志

维护人：队长（DSH 会话）
本轮起算：2026-09-24（R13）
上一本日志：`docs/archive/progress-log-至R12-2026-09-24.md`（R1–R12，1067 行，含已废弃的 chanlun 参照记录）
总计划：`/home/ubuntu/work/cpt-audit/CPT-总计划-2026-09-24.md`（唯一任务台账，轮次与验收以它为准）
commit 规范：每个里程碑验收通过后一次 commit；阶段内允许 working commit

## 0. 开局盘点（R13 起点）

- 仓库：`/home/ubuntu/work/Chan_Pattern_Trader/`
- remote：`https://github.com/nxz1026/Chan_Pattern_Trader.git`，分支 `main`
- 起点 HEAD：`930051b`（R12），R1–R12 全部已推
- 测试基线：**182 passed**
- 门禁：ruff / ruff format / mypy / vulture / import-linter 全绿
- 看板：`https://127.0.0.1/cpt/`，后端 `127.0.0.1:8010`，前端 `/var/www/cpt-dashboard/`

## 1. 决策日志

### 2026-09-24 决策（本轮全部，对应总计划 §0）

- **G1** 移除 3 个参照：`chanlun==2606.73`、`chanlun-pro@78ffa470`、`chanlun_pine@0c028ef`（GPL-3.0）。理由：其分型/笔实现与缠论定义冲突，无参照价值。
- **F1** 修仓库 nginx 模板，补 `location /cpt/api/`（线上已修，仅模板漂移）。
- **F2** 删 oracle 全家桶。
- **F3** `reference_chanlun.py` 不改名（承重件，被 9 处 import）。
- **G2** `progress-log.md` 存副本后重开。
- **B** 不考虑授权，只考虑项目好不好用。
- **A1** 分型/笔 → czsc；中枢 → 留 CPT 自研但**改延伸不收紧**；走势类型/线段/递归 → 自研。
- **A2** `min_bi_len = 6`（czsc 默认；4–7 区间实测不敏感）。
- **A3** 新增 `RulesConfig.min_bi_len`（量纲=去包含后K线根数），`min_elements_for_higher_bi` 不动。
- **C1–C6** 复权因子走 Wind 后复权 / 涨跌停特殊处理 / 停牌跳过 / T+1 标注 / 非交易日不出图 / 只做日线但要合成周月线中枢。
- **D** 并入现有看板；热门池+自选；自选入口即时生效(localStorage)+落盘可选。
- **E1/E2** 四画布 flag 切换；lightweight-charts 内联 vendor 不走 CDN。
- **一买** 移植 czsc `check_first_buy`，`Bi` 加 `power_volume`。
- **未完成笔** 暂时不画。

### A1 的依据（本轮实测，决定性）

把 czsc 的笔喂进 **CPT 自己的中枢函数**：

| 方案 | 分型 | 笔 | 笔中枢 |
|---|---|---|---|
| CPT 现状 | 327/357/353 | **326/356/352** | 43/45/43 |
| czsc 笔 → CPT 中枢 | 202/203/245 | **50/49/49** | **6/6/6** |
| MIT Rust oracle | 58/62/60 | 57/61/59 | 9/8/8 |

CPT 笔端点中位跨度 = **2 根原始K线**；跨度 <4 根的笔占比 **66.9%/74.4%/73.0%** —— 两根 3K 分型窗口重叠，**按定义不可能成笔**。换笔后中枢自动回到 6/6/6。

中枢延伸规则的实测：CPT 现状**延伸时单调收紧**（`zhongshu.py:108-112`），偏离缠论原文，把一个大中枢切成 6 个并切出宽度 4.5 的畸形中枢；改为**不收紧**后与 czsc 中枢**逐项完全一致**（fixture1 区间宽 `[151.0, 66.5, 17.2]`、纳入笔数 `[38, 6, 5]`）。

## 2. 轮次记录

### R13 — 清账

状态：**已完工**（验收见下）

改动：

1. **去参照（G1）**
   - `pyproject.toml`：删 `oracle = ["chanlun==2606.73"]`
   - `scripts/fetch_references.sh`：REPOS 由 3 个缠论参照改为 `czsc@701e480a`（Apache-2.0）+ `wbt@39bb1e8a`（MIT）
   - `.gitignore`：3 行 chanlun 参照改为 `/references/czsc/`、`/references/wbt/`
   - `references/README.md`：重写参考表，记录已移除的参照与理由
   - 删本地 `references/{chanlun-pro,chanlun.py,chanlun_pine}`（392M）
2. **修 nginx 模板（F1）**
   - `deploy/nginx/cpt-dashboard.conf`：补 `location /cpt/api/ { proxy_pass http://127.0.0.1:8010/api/; }`（含注释说明最长前缀匹配）
3. **删 oracle（F2）**
   - `git rm`：`cpt/adapters/rust_chanlun.py`（311 行）、`scripts/compare_oracle.py`（382 行）、`tests/test_compare_oracle.py`（40 行）、`docs/m2-oracle-diagnostic.md`（52 行）
   - `.github/workflows/ci.yml`：去掉 `oracle-parity` job；ruff/format/vulture 命令去掉 `scripts/compare_oracle.py`
4. **重置日志（G2）**
   - `docs/archive/progress-log-至R12-2026-09-24.md`（1067 行）存档；本文件从 R13 起
5. **清失效引用**
   - `docs/rules.md:8`：去掉 chanlun-pro/chanlun.py/Pine 三处实现参照，改为 czsc

**未改**：`docs/m6-quality-report.md`、`docs/dashboard-final-acceptance.md`、`docs/low-risk-hardening.md`、`cpt-feature-review-and-deadcode-audit.md` —— 它们是带日期的历史验收/审计快照，改它们等于篡改历史记录。其中命令已失效，以本日志和总计划为准。

### R14 — 算法换引擎

状态：**进行中**（R14-1 完工）

#### R14-1 czsc 接入（方案 C）✅

用户 2026-09-24 定：**方案 C —— 可选依赖**。

**否决 A/B/D 的实测依据**：

| 方案 | 实测结论 |
|---|---|
| A `pip install czsc` 无条件依赖 | 会拉入 pandas/numpy/pyarrow/polars/scipy/statsmodels/openpyxl/requests，破坏 `dependencies = []` |
| B vendor `_native.abi3.so` | **46.6 MB**（超 GitHub 50 MB 警告线），且**半废**：取 `FX.dt`/`BI.sdt` 直接 `ModuleNotFoundError: pandas`（`crates/czsc-core/src/objects/fx.rs` 的 `create_naive_pandas_timestamp` 是硬依赖） |
| D 移植 273 行 Rust | 可行但自担维护；用户选择优先复用上游 |

**落地**：

- `pyproject.toml` 加 `chan = ["czsc==1.0.1"]`，**`dependencies = []` 保持为空**
- 新增 `cpt/adapters/czsc_chanlun.py`：延迟导入 + 版本校验（`CzscNotInstalledError` / `CzscVersionError`）
- 新增 `tests/test_czsc_backend.py`（18 个测试，其中 7 个不依赖 czsc 始终运行）

**核心回归（决定性）**：

| 指标 | CPT 自研笔 | czsc 笔（本适配器） |
|---|---|---|
| 笔端点跨度**中位** | **2 根**原始K线 | **9–10 根** |
| 跨度 <4 根占比 | **66.9% / 74.4% / 73.0%** | **6.1% / 4.2% / 6.2%** |
| 分型 / 笔 / 中枢 | 327/326/43 · 357/356/45 · 353/352/43 | 202/50/6 · 203/49/6 · 245/49/6 |

> 残留的 4–6% 短跨度不是 bug：czsc `check_bi`（`analyze/utils.rs:417`）的门槛是
> `if !ab_include && bars_a.len() >= min_bi_len` —— 当两端分型 K 线互相包含时
> （`ab_include`）**门槛不适用**。这是 czsc 的既定规则。

**关键设计决定**：

- **只借分型与笔**，中枢仍用 `cpt.domain.zhongshu.build_zhongshus`。实测 czsc 的
  `zs_list` 虽 `is_valid()` 全过、无 `zg<zd`，但**会产出 <3 笔的假中枢**
  （fixture3 有 2 个两笔中枢，其中一个还是首个），且 `ZS` 无 `bi_ids` 溯源。
  测试 `test_real_fixture_zhongshu_never_has_negative_width` 把这个事实钉住。
- **必须传原始 K 线**：czsc 内部自己做包含处理（`remove_include`），
  先跑 `merge_contained_bars` 会让包含关系被处理两次。
- **周期自动推断**：由相邻 `open_time` 的**中位**间隔反推（用中位而非均值，
  避免停牌/断线缺口带偏）。
- **时间必须能反查回原始 K 线下标**：czsc 只回吐 `Timestamp`，映射失败即响亮报错，
  不静默用错时间（这是 R1–R12 期间踩过的坑）。

**验收**：`198 passed`（R13 的 180 + 18 新增）；ruff check / ruff format --check（111 files）/
mypy（55 files）/ vulture / import-linter 全绿。

#### R14-2 中枢延伸改为不收缩 ✅

**改动**：`cpt/domain/zhongshu.py` 的 `_extend` 不再做 `high = min(...)` /
`low = max(...)`。中枢区间由**建枢的前三笔唯一确定**，延伸只把后续重叠笔纳入
（后移 `end_time`、追加 `bi_ids`）。返回值由 `(end, high, low)` 简化为 `end`。

**依据（缠论原文）**：中枢区间＝连续三段走势类型的**共同重叠部分**；延伸只延长
中枢，不收缩区间。旧口径是"与**当前**（已收缩的）区间比较后继续收缩"，收敛到
一个越来越窄的核，会把一个中枢切成多个窄区间。

**实测对照（3 个 oracle fixture）**：

| fixture | 旧口径（收缩） | 新口径（不收缩） | czsc `get_zs_seq` |
|---|---|---|---|
| 2024-02-01 | 6 个，宽 `[117.8, 78.0, 19.4, 14.9, 66.5, 17.2]` | 3 个，宽 `[151.0, 66.5, 17.2]`，笔数 `[38, 6, 5]` | **逐项一致** |
| 2024-09-01 | 6 个 | 5 个，宽 `[247.7, 258.5, 291.0, 232.7, 396.7]` | 区间宽逐项一致 |
| 2025-04-01 | 6 个 | 6 个，宽 `[356.4, 613.6, 720.3, 467.5, 754.3, 307.5]` | 区间宽逐项一致 |

**畸形区间消失**：最小宽度由 **4.5** 提升到 **17.2 / 232.7 / 307.5**。

**新增/改动测试**：

- `tests/test_zhongshu.py`：原 `test_overlapping_followup_bi_extends_and_shrinks_zone`
  断言的是旧收缩行为（`(9.5, 7.5)`），改写为
  `..._extends_without_shrinking_zone`（断言 `(10, 7)`）；新增
  `test_zone_range_is_immutable_under_long_extension`（延伸几十笔区间不变）与
  `test_extension_never_produces_degenerate_zero_width_zone`
- `tests/test_czsc_backend.py`：新增
  `test_cpt_zhongshu_matches_czsc_get_zs_seq`（czsc 最长连续合法中枢段的区间宽
  必须是 CPT 的连续子序列）与
  `test_cpt_zhongshu_matches_czsc_exactly_on_clean_fixture`（fixture1 无中间退化
  中枢，严格逐项比对区间宽 + 纳入笔数）与
  `test_cpt_zhongshu_extension_never_shrinks_the_zone`

> 纳入笔数在 czsc 退化中枢的**边界**处会差 1：czsc 把一个 <3 笔的"中枢"插在
> 序列中间并吃掉笔，CPT 要求至少 3 笔。区间宽不受影响，所以交叉验证比区间宽。

**验收**：`203 passed`（R14-1 的 198 + 5）；全门禁绿。

#### R14-3 `RulesConfig.min_bi_len = 6` ✅

**改动**：

- `cpt/domain/config.py` 新增字段 `min_bi_len: int = 6`，量纲＝**去包含后的 K 线根数**
- **不动** `min_elements_for_higher_bi = 5`（量纲＝低级别结构元素数，递归层用）
- `__post_init__` 加 `min_bi_len >= 1` 校验；类 docstring 显式写明两个门槛量纲不同、不可混用
- `config.py` 模块 docstring 去掉 chanlun.py 溯源（R14-5 的一部分提前做掉）
- `docs/rules.md` 新增 **§9.8**；§9.6/§9.7 去掉 chanlun-pro 溯源并加量纲说明

**取值依据**：与 czsc `check_bi` 的门槛对齐（`analyze/utils.rs`：
`!ab_include && bars_a.len() >= min_bi_len`）。实测 `min_bi_len` 在 **4–7 区间笔数
几乎不敏感**（3 个 fixture 恒 ~50 笔），8 起急降（44/49/40），9–10 更差
（32/40/30、30/36/30），故直接采用 czsc 上游默认 6，**不自造数值**。

**新增测试**（4 个，`tests/test_czsc_backend.py`）：

- `test_rules_config_min_bi_len_matches_adapter_default` —— 配置与适配器默认值防漂移
- `test_min_bi_len_is_distinct_from_higher_bi_gate` —— 两个门槛共存、独立可调、值域校验
- `test_min_bi_len_survives_config_roundtrip` —— 序列化往返
- `test_adapter_honours_rules_config_min_bi_len` —— 配置能真正驱动适配器

**验收**：`207 passed`（R14-2 的 203 + 4）；全门禁绿。

#### R14 剩余子任务

- R14-4 一买移植（`Bi` 加 `power_volume`；czsc `BI` 已直接暴露 `length`/`power_price`/`power_volume`）
- R14-5 重写 `docs/rules.md` §7 + 修 `signal.py:14` 的 chanlun 溯源
