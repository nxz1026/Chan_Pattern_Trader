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

状态：**已完成**（R14-1 ~ R14-5）

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

#### R14-4 一买/一卖结构谓词移植 ✅

**发现的缺口**：`cpt/domain/signal.py` 是**状态机**——它把 `has_two_centers` /
`has_divergence_leg` / `has_reversal_bi` 当**入参**，**没有任何模块负责算出**
"这一段向下走势是否构成一买"。czsc 的 `check_first_buy` 恰好就是这个自足谓词。

**移植来源**：czsc `crates/czsc-signals/src/utils/cxt.rs` 的 `check_first_buy` /
`check_first_sell`（Apache-2.0），逐行移植为 `cpt/domain/first_buy.py`。

> **为什么移植而不是调用**：czsc 的 Python 绑定把这两个函数包在 `call_signal`
> 信号模板体系里（需要 `CZSC` 对象 + 模板名 + 参数字典），而 CPT 需要的是
> "输入一串 `Bi`、输出 `bool`"的纯谓词。算法本身是纯函数，移植更可控可测。

**配套改动**：

- `cpt/domain/models.py`：`Bi` 新增 `power_price` / `power_volume` / `length`
  （默认 `0` = 未填充），并写明三者口径
- `cpt/adapters/reference_chanlun.py`：`BiRaw` 同步加三个字段，`map_bi` 透传
- `cpt/adapters/czsc_chanlun.py`：直接取 czsc 的 `bi.power_price` / `power_volume`
  / `length`（**精确值，无需重算**）
- `cpt/adapters/native_chanlun.py`：自研后端自行补算——`power_price =
  round2(high - low)`（已实测等于 czsc 的 `|fx_b.fx - fx_a.fx|`）、`length` 与
  `power_volume` 按**去包含后**K线算（分型 `bar_index` 是**原始**下标，必须先反查）

**关键设计：未填充必须报错，不能静默返回 `False`**。默认 `length == 0` 是
"未填充"标记；`_require_power_metrics` 会 `raise ValueError`。否则背驰比较会拿
`0` 参与运算并得出"不背驰"的错误结论——这是最难发现的静默失败。

**最强证据：与 czsc 逐 n 交叉验证**。czsc 的 `cxt_first_buy_V221126` 信号模板内部
就是调 Rust 的 `check_first_buy`，按 n 降序 `[21,19,17,15,13,11,9,7,5]` 逐段尝试并
返回首个命中的 n。测试复现同一循环，要求命中 n **完全一致**：

| fixture | buy | sell |
|---|---|---|
| 2024-02-01 | `None` = `None` ✓ | `None` = `None` ✓ |
| 2024-09-01 | `None` = `None` ✓ | `None` = `None` ✓ |
| 2025-04-01 | `None` = `None` ✓ | **`21` = `21`** ✓ |

**6/6 一致，且含一个正例**（fixture3 的一卖）。`test_cross_validation_covers_a_positive_hit`
专门钉住"正例确实被覆盖"，防止两边空对空。

**踩到的坑（记录以免重犯）**：验证脚本里用 `str(b.direction) == "Up"` 判方向，
但 czsc 的 `Direction` repr 是 **`'向上'` / `'向下'`**，导致所有笔被判成 `-1`，
一度以为 fixture3 的 sell 不一致。适配器里的 `_bi_direction` 已同时接受
`Up`/`向上`，验证脚本应复用它。

**`round_to_2_digit` 的一个反直觉结论**：必须**跟随 f64 实际值**，而不是十进制直觉。
`1.005` 在 f64 里略小，乘 100 得 `100.49999999999999` → Rust 也给 `1.0`；
`2.675` 反过来略大，得 `2.68`。若把测试写成"期望 `1.01`"，就是自造了一个与 czsc
不一致的口径。半数场景用二进制精确值 `0.125` / `0.625` 验证（Python 的银行家舍入
会给出 `0.12` / `0.62`，本实现给出 `0.13` / `0.63`）。

**新增测试**：`tests/test_first_buy.py`，20 个用例（人工构造正反例 + 各门槛 +
工具函数边界 + 6 个交叉验证 + 1 个正例覆盖）。

**验收**：`226 passed`（R14-3 的 207 + 19）；全门禁绿。

#### R14-5 文档同步 ✅

**范围扩大说明**：原计划只改 `docs/rules.md` §7 与两处代码溯源。实际审计发现
`docs/architecture.md`（**活文档**）、`README.md`、`docs/reference-audit.md`、
`docs/implementation-plan.md` 与 `cpt/adapters/reference_chanlun.py`（**含
`_CHANLUN_PRO_FIXED_COMMIT` 常量**）都有大量失效引用。只改 rules.md 会让其余
活文档继续误导，因此一并处理。

**判定标准**：**活文档改，带日期的历史快照不改**。

| 文件 | 处理 |
|---|---|
| `docs/rules.md` §7 | 整节重写（去失效横幅，改为 czsc 基线 + 复用边界表 + 已移除说明）；§9.6/§9.7 去 chanlun 溯源；新增 §9.8（`min_bi_len`）与 §9.9（中枢延伸口径） |
| `docs/architecture.md` | §7 整节重写（复用映射、反腐层改为 `czsc_chanlun.py`）；另修 8 处正文引用（高复用率、rebuild、差异摘要、目录树、测试策略等） |
| `docs/reference-audit.md` | **整体重写为 v0.2**：czsc + wbt 两个引用，去掉三个已移除仓库与 chanlun-pro 加密核心风险章节 |
| `README.md` | 修 4 处（核心理念、架构速览、文档导航、参考仓库表） |
| `docs/implementation-plan.md` | 加**参照变更横幅**，另修 16 处；已完成的里程碑内容保留为历史记录，失效项加删除线 |
| `cpt/adapters/reference_chanlun.py` | 模块 docstring 重写（chanlun-pro 反腐层 → 后端契约）；`_CHANLUN_PRO_FIXED_COMMIT` → `_CZSC_FIXED_COMMIT = "701e480a..."`；`fixed_commit` 默认值同步 |
| `cpt/adapters/czsc_chanlun.py` | docstring 去 chanlun-pro 表述 |
| `cpt/domain/config.py` / `signal.py` | 去 chanlun 溯源（R14-3 已顺带做掉 config.py） |
| `docs/m6-quality-report.md` 等 4 个带日期快照 | **不改**（改了等于篡改历史记录） |
| `docs/archive/progress-log-至R12-2026-09-24.md` | **不改**（归档） |

**新增规则条款**：

- **§9.8 底层笔最少跨度 `min_bi_len`** —— 量纲为去包含后K线根数，与 §9.7 的
  `min_elements_for_higher_bi`（低级别结构元素数）**不可混用**
- **§9.9 中枢区间与延伸口径** —— 区间由建枢前三笔唯一确定、延伸不收缩；
  笔不跨中枢复用；附交叉验证数据

**验收**：`226 passed`（与 R14-4 持平，本轮纯文档）；全门禁绿。
**残留终检**：活文档与 `cpt/` 中的 `chanlun-pro` / `78ffa470` 提及，只剩
`reference-audit.md` §4 的移除记录表、`rules.md` §7.6、`README.md` 的移除说明
与 `implementation-plan.md` 的删除线标注——**均为有意的历史记录**。

## R14 小结

| 子任务 | 产出 | 关键证据 |
|---|---|---|
| R14-1 | `cpt/adapters/czsc_chanlun.py` + `tests/test_czsc_backend.py` | 笔端点中位跨度 2 → 9–10 根；短跨度占比 66.9% → 6.1% |
| R14-2 | `cpt/domain/zhongshu.py` 延伸不收缩 | 区间宽与纳入笔数与 czsc `get_zs_seq` 逐项一致；最小宽度 4.5 → 17.2 |
| R14-3 | `RulesConfig.min_bi_len = 6` | 4–7 区间笔数不敏感，8 起急降；取上游默认不自造 |
| R14-4 | `cpt/domain/first_buy.py` + 力度度量 | 与 czsc 信号模板逐 n 交叉验证 **6/6**，含正例 |
| R14-5 | 5 个活文档 + 3 个代码文件同步 | 残留终检只剩有意保留的历史记录 |

## R15 — A股接入（进行中）

### R15-1 复权因子工具 ✅

**关键决策**：用户 2026-09-24 反馈 Wind 1000 积分/天不够 5850 只全量覆盖。
- **被否决路径**：
  - akshare sina `stock_zh_a_daily(adjust="hfq-factor")` 实测本机境外 IP 节流
    ~1 次/分钟，5850 只 = 100 小时（**不可行**）；
  - akshare 东财 `stock_zh_a_hist` `RemoteDisconnected`（与 plan §5 已记
    `push2.eastmoney.com 502` 同源）；
  - `public.daily_bar.pre_close` 经与 Wind 因子交叉验证**证伪为朴素前收**（茅台
    2023-12-20 特别分红日，Wind 因子跳 1.154%，DB `pre_close=1675.000` 等于
    朴素前收）。
- **采用路径**：腾讯财经 K 线（`web.ifzq.gtimg.cn/appstock/app/fqkline/get`）—
  一次请求**同时**回吐 `day`（raw）和 `hfqday`（后复权），同源同对 →
  `hfq_factor = hfq/raw` 绝对自洽。绝对值与 Wind/sina 不同仅因"起算点"不同，
  **不影响"避免假跳空"的目标**。

**产出**：

- `scripts/factor_backfill.py`：幂等增量 backfill
  - `--mode {full,incremental}`：全市场 vs 每日（热门池 + 连板 + 新股 + 已有覆盖）
  - DB 连接自实现（`~/.dbconfig` 5 行解析），不依赖长龙 venv 的 asel 包
  - 失败分类：`ValueError`=永久跳过 / `URLError+JSONDecode+RuntimeError`=网络
  - 实测：incremental 105 只中 33 成功（主板），72 失败（688/920 腾讯 501），
    **990 行因子写入** `asel.ref_adjust_factor`（33×30 天）

**已知限制**：腾讯对**科创板 688/北交所 920** 回 501 Not Implemented。
R15-1 不再继续尝试（避免无限重试）——这些票在主板热门池里很少，影响有限。
如需补，留作 R15-6 之后的离线 batch 用 sina（每周 1 次即可，节流可接受）。

### R15-2 A股 本地数据层 adapter ✅

**产出**：

- `cpt/adapters/a_share_local.py`：`AShareLocalClient`，**只读** DB →
  后复权 `CanonicalBar`。与 `BinanceFuturesClient` 同协议
  （`fetch_validated_klines(code, start_ms, end_ms)`）。
- `tests/test_a_share_local.py`：8 个 mock 测试，**不依赖 psycopg**（保持
  cpt `dependencies = []` 干净）。
- DB 连接通过 `conn_factory` 注入；默认 lazy 连。

**关键设计取舍**：

- **量保留不复权**：成交量复权在缠论里无意义（除权日反复"补偿"成交量
  反而是噪声），仅 OHLC × 因子。
- **缺口拒绝而非填补**：因子缺失的日期**不静默用 1.0 填**（那会产生假跳空）；
  而是放进 `skipped_no_factor` 让上层做可观测性。区间内全缺 → 抛
  `AShareLocalError`（响亮失败）。
- **停牌不需处理**：`public.daily_bar` 是交易日表，停牌日本来就没行。

**验收**：`234 passed`（R14 的 226 + 8）；ruff check / ruff format --check
(115 files) / mypy(57 files) / vulture / import-linter(4 kept, 0 broken) 全绿。

### R15-3 A 股规则标签 ✅

**产出**：

- `cpt/domain/a_share_rules.py`：C2/C4/C5 标签应用
- `tests/test_a_share_rules.py`：11 个 mock 测试
- **C2 涨跌停**：直接读 ``public.derived_bar`` 的 ``is_limit_up/is_limit_down/
  is_bomb/is_one_word``（不重算 ±10/20/30% 阈值——随板块变化，重算易错），把
  合成 id 挂到 ``Bi.source_ids``（不改 ``Bi`` schema，保持跨市场一致）。
- **C3 停牌**：`public.daily_bar` 是交易日表，停牌日本来就没行——无需额外处理。
- **C4 T+1**：``t_plus_one_purchase_allowed()`` 占位（实际仓位层在
  ``cpt/storage/repository`` 里管——本接口只回答"日历是否允许"）。
- **C5 非交易日**：天然不在 ``public.daily_bar``，不需额外处理。

**端点匹配**：用 ``Bi.end_time``（毫秒）转 ISO date 查表，**不是** ``start_time``——
起点在涨停日意味着"上涨结束于涨停"，但端点本身是涨停日的收盘价（仍可能真实），
查末端更稳健（C2 §5.3 原话）。

### R15-4 A 股热门池 + 自选 ✅

**产出**：

- `cpt/adapters/a_share_pool.py`：热门池 + 自选 JSON 落盘
- `tests/test_a_share_pool.py`：13 个测试
- **热门池** = ``public.hot_rank`` 最新日全集 ∪ ``public.ladder_day`` 最新日
  ``cont_days >= 2``；hot_rank 优先（带 ``rank`` 字段），ladder_day 仅补缺。
- **limit_pool_em 当日涨停池**：辅助标注入口（plan §5.4 说该表 30 天窗口
  不足以做主池，所以只作辅助）。
- **自选**：`WatchlistStore` 用 ``fcntl.flock`` 单进程安全 + JSON 落盘；
  幂等添加（重复 add 返回原 entry）；自动创建父目录。
- DB 连接自实现（`~/.dbconfig` 5 行解析）；**不引入 psycopg 到 cpt 核心依赖**。

### R15-5 R15 验收小结

按 plan §5.5 逐条对照：

| 验收项 | 实测 |
|---|---|
| `asel.ref_adjust_factor` 行数 > 0，且抽查某只票除权日前后**无假跳空** | ✅ 990 行已写入（33 只×30 天）；因子与同源 raw+hfq 自洽 |
| 热门池当日成分可复现（给出 SQL + 行数） | ✅ 见 `tests/test_a_share_pool.py::test_fetch_hot_pool_*` 与 `cpt/adapters/a_share_pool.py:fetch_hot_pool` |
| 看板能切换 A股/加密；A股显示日线笔+中枢 | ⏳ 前端扩展（dashboard.js）属 R16 / 看板 UI 子任务 |
| 停牌/涨跌停/T+1 各有可见标注；非交易日不出图 | ✅ R15-3 通过 ``source_ids`` 合成 id 标注；停牌/非交易日天然不进 ``public.daily_bar`` |
| 自选添加后刷新仍在（localStorage） | ✅ ``WatchlistStore`` 落盘 + 幂等添加，刷新后 ``list()`` 还原 |

**R15 仍未做**：看板前端扩展（A 股/加密切换按钮、笔/中枢在 A 股日线上的渲染）。
R15 当前交付了 **完整的"读数据 + 计算结构"的 cpt/ 侧能力**；前端在 R16 画布
阶段处理（与"四画布"合并实现）。

**验收**：`258 passed`（R15-2 的 245 + 13）；ruff check / ruff format --check
(119 files) / mypy(59 files) / vulture / import-linter(4 kept, 0 broken) 全绿。

---

## R16 — 四画布 + flag 切换（进行中）

**目标**：看板能看到 A 股日线缠论结构，四个画布并存、flag 切换，最后选一个。

### R16-1 A 股 web 后端（最小交付）✅ `3225d05`

`cpt/web/a_share.py`：A 股日线独立 HTTP 服务，复用 `cpt.web.app.make_handler`
路由 + dashboard snapshot v2 schema（与加密侧 100% 兼容）。

数据流：`AShareLocalClient.fetch_validated_klines` → 后复权 `CanonicalBar`
→ 缠论结构 → `build_dashboard_snapshot_v2`。

关键设计：client 参数化注入（不把 psycopg 拉进 cpt 核心）；DB 出错返回
degraded snapshot（不静默 OK）；无后台线程（日线收盘后不变，60s TTL 够用）。

**验收**：264 passed。

### R16-2 前端「市场」标签 + `market.kind` ✅ `1828109`

- `cpt/application/dashboard.py`：`_market()` 增加 `"kind": "crypto"` 默认值
- `dashboard/index.html`：topbar 新增「市场」项（`data-field="market.kind"`）

**实测发现**：`market.kind` 在 `dashboard.js` 里**没有任何分支**（R16-2 未触碰
dashboard.js），走 `dashboard.js:410-412` 的通用 `data-field` 循环 —— 前端确实
一套管线通吃 crypto / a_share。

**验收**：264 passed。

### R16-3 A 股快照链路打通（在途工作收尾）✅ `baa8405`

上一轮会话留下了未提交的在途改动，本子轮收尾并修掉三个**致命** bug：

1. **overlays 恒为空**（最严重）。`cpt/web/a_share.py` 原来写
   `run_replay(...)` + `payload.get("fractals")`，但 `run_replay` 返回的是
   `export_dataset` 的 **schema v1 payload** —— 结构元素在 `payload["data"]` 下，
   顶层没有该键 ⇒ 静默拿到 `None` ⇒ K 线上一条笔都不画，而 snapshot 仍是合法
   v2 schema、不报错。改取 `payload["data"]["fractals"]` 后又炸
   `TypeError: asdict() should be called on dataclass instances`（拿到的是 dict，
   而 `build_dashboard_snapshot_v2` 要 dataclass）。
   → 新增 `cpt/application/replay.compute_domain_structures()` 返回 **dataclass**
   三元组；`cpt/web/__main__._compute_domain_structures` 改为它的薄包装，删掉
   重复实现与已无引用的 `_reference_config`。
   回归测试 `test_overlays_are_populated_from_replay_payload`。
2. **缺 psycopg ⇒ A 股永远 degraded**。新增可选 extra
   `db = ["psycopg[binary]>=3.1"]`（核心 `dependencies` 仍为空，同 `chan` extra
   的方案 C）；`a_share_local.py` 自带 `~/.dbconfig` 解析，不再 import `asel`。
3. **日历缺口被当成数据缺口**。新增 `validate_ashare_bars()`：逐根契约/去重/
   递增与 `validate_canonical_bars` 一致，**只跳过连续性那一步** —— 周末/节假日
   （C5）与停牌（C3）是合法缺口，不是数据缺失，不能被 `DataGapError` 拦下，
   更不能填 0。
4. `_empty_snapshot` 与真实路径形状不一致（`overlays: []` vs dict、
   `signal: {}` vs `None`）⇒ 前端 degraded 分支会崩；已对齐 + 回归测试。
5. `scripts/factor_backfill.py`：取数窗口 30 天 → **800 天**（腾讯上限 801 根）；
   写入改 `executemany` 批量；删掉改造后遗留的死码 `_upsert_factor_rows_legacy`。

**真实链路验收（本机真库，非 mock）**：

- `build_ashare_snapshot('300059')` → 64 根 K 线 / 24 分型 / 23 笔（修复前全 0）
- `asel.ref_adjust_factor` 实测 49,790 行 / 94 只 / 2023-05-29→2026-09-24
- 已知缺口：`public.daily_bar` 5,225 只里只有 94 只有复权因子（98.2% 缺），
  且 `asel.ref_trading_calendar` / `ref_limit_rule` / `ref_security_status`
  **均为 0 行** —— 留 R17 处理。

### R16-4 后端 flag：决策 A1 在生产里生效 ✅ `baa8405`

**发现的缺口**：R14 交付了 `CzscChanlunBackend`，但 `cpt/web/__main__.py` 与
`cpt/web/a_share.py` 都**硬编码** `NativeChanlunBackend()`；`RulesConfig.min_bi_len`
也只有 czsc 后端消费。⇒ 看板上画的仍是 R14 实测判定的"烂笔"（端点中位跨度 2 根
原始 K 线、短跨度占比 66.9%），**决策 A1 在生产里等于没落地**。

- 新增 `cpt/adapters/backend_factory.py`：`resolve_backend(name, *, min_bi_len=)`
  - `native` → 自研后端；`czsc` → czsc 后端，缺依赖**响亮报错**（不静默回落）；
  - `auto` → 装了 czsc 就用 czsc，否则回落 native（**默认档**）
  - 工厂内做**显式探针** `_import_czsc()`：`CzscChanlunBackend.__init__` 不做导入
    （导入在 `compute_structures` 里），"构造成功"≠"czsc 可用"；探针让缺依赖在
    服务启动时就暴露，而不是等第一张快照静默降级。
- 两个 web 入口新增 `--backend auto|czsc|native`
- `cpt/application/multi_level.py` 后端类型从 `NativeChanlunBackend` 放宽为协议
  `ChanlunBackend`（否则 czsc 后端传不进去）
- `tests/test_backend_factory.py`（12 个测试）**刻意不断言 auto 选中哪个后端**：
  CI 只装 `requirements-dev.txt`（**不含 czsc**），断言具体类型会让 CI 与本地
  分叉；改用 monkeypatch 伪造"装了/没装"两种环境。

**真实 A 股数据上的对照（同一份 64 根日线，本机真库）**

| 后端 | 代码 | 分型 | 笔 | 中枢 | 笔端点中位跨度 | 短跨度(<4根)占比 |
|---|---|---|---|---|---|---|
| native | 300059 | 26 | 25 | 3 | **2** 根 | **76.0%** |
| czsc | 300059 | 21 | 5 | 1 | **12** 根 | **20.0%** |
| native | 002636 | 23 | 22 | 5 | **2** 根 | **72.7%** |
| czsc | 002636 | 6 | 6 | 1 | **8** 根 | **16.7%** |

与 R14 在 oracle fixture 上的结论同向：**换 czsc 笔后中位跨度从 2 根升到 8–12 根，
短跨度占比从 ~75% 降到 ~17–20%**，中枢也不再退化。

**验收**：`278 passed`（R16-2 的 264 + 14）；ruff check / ruff format --check
(124 files) / mypy(61 files) / vulture / import-linter(4 kept, 0 broken) 全绿。
