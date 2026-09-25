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
  - 实测：incremental 105 只中 33 成功（主板），72 失败（688/920 取不到后复权），
    **990 行因子写入** `asel.ref_adjust_factor`（33×30 天）

**已知限制（2026-09-24 R17-3 复核更正）**：腾讯对**部分标的**不提供后复权序列。
R15-1 不再继续尝试（避免无限重试）。如需补，留作离线 batch 用 sina。

> **⚠️ 本条曾被记为"腾讯对科创板 688/北交所 920 回 501 Not Implemented"，实测证伪。
> 且第一次更正（改成"688/301/920 三个板块不支持"）同样是错的 —— 记录两次踩坑，
> 因为这两次的错误类型不同，都值得留下。**
>
> **机制错了**：不是 501，是 **HTTP 200 但 payload 里没有 `hfqday` 键**（只有
> `day`）。501 是"服务端不认识该接口"，会引导人换端点；实际换端点也没用，是这批
> **标的本身**没有后复权数据。
>
> **"按板块"这个模型也错了**：后复权可用性是**逐标的**的，不是逐板块的。实测：
>
> | 板块 | 有 `hfqday` | 无 `hfqday` |
> |---|---|---|
> | 沪主板 600/601/603/605 | 600519 / 601398 / 603259 / 605499（全 800 行） | — |
> | 深主板 000/001/002/003 | 000002 / 001979 / 002614 / 003816（全 800 行） | — |
> | 创业板 300/301 | 300059 / 300750 / 300124 / **301029 / 301047 / 301060 / 301078 / 301080 / 301200 / 301300 / 301500 / 301630** | 301686(3) / 301689(11) / 301699(12) |
> | 科创板 688 | **688111 / 688036（800 行）** | 688981(800!) / 688825(44) / 688836(27) |
> | 北交所 920/83 | — | 920201 / 920025 / 920002 / 920099 / 832735 / 873169（`day` 仅 0–1 行） |
>
> 三处反例各自打掉了三种猜测：**① 688111/688036 有 hfq** ⇒ "科创板不支持"错；
> **② 多数 301 有 hfq（只有近期新股没有）** ⇒ "创业板 301 不支持"错；
> **③ 688981（中芯国际，800 根 raw 但无 hfq）** ⇒ "只有次新股没有"也错 ——
> 它是老牌大票，说明腾讯的 hfq 源**本身有洞**。
>
> ⇒ 结论：**没有任何前缀规则能预测**。唯一可靠的做法是**去问腾讯、然后如实报告它
> 实际返回了什么**。任何"按板块硬编码"的优化都会重犯本条记录的错。
> 北交所则是另一回事：`bj` 前缀认得（`qt` 节点有值），但日线历史基本不提供。

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
- **C4 T+1**：``t_plus_one_purchase_allowed()`` 占位（本接口只回答"日历是否允许"；
  仓位层**当前不存在** —— 原写的 ``cpt/storage/repository`` 已于 2026-09-25 审核
  P0-2 整层删除，且实查 ``git show 79170b6:cpt/storage/repository.py`` 里**从来没有**
  过 position/OPEN 相关逻辑，故那句话当时就是错的）。
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

  > **缺口成因（2026-09-24 R17-3 查证，此前只记了数字没记原因）**：
  > ① **全市场 backfill 从未执行** —— 只跑过 `--mode incremental`（热门池 ∪ 连板
  > 梯队 ∪ 近 7 天新股 ∪ 已有覆盖），DB 实证 94 只因子**全部落在热门池并集内、
  > 池外 0 只**；`--mode full`（5,225 只）在脚本里存在但一次没跑。
  > ② 热门池 100 只里差的 6 只（`301686/301689/301699`、`688825/688836`、
  > `920201`）腾讯不提供 `hfqday`（见 R15-1 节的更正表；注意这是**逐标的**的
  > 属性，不是板块属性）。
  > ③ Wind 不返回因子（`fetch_adjust_factors` 每只 2 次调用，全市场 ≈ 10,450 次），
  > 从来不是全市场 backfill 的可行通道 —— 这才是 R15 改用腾讯的原因。
  > ⇒ `94 = 100（热门池）− 6（腾讯无 hfqday）`；`5225 − 94` 是**没试过**，不是**失败**。

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

## R16-5 — 四画布落地 + flag 切换

**目标（总计划 §6）**：同一份快照，四个画布渲染一致（结构元素数量一致），
`?canvas=A|B|C|D` 切换，**断网可用（0 个 CDN 请求）**。

### 架构决定：数据归一化留在 dashboard.js，画布只负责"画"

四个画布若各自归一化快照，"渲染一致"就无从证明（口径可能不同）。所以
`dashboard.js` 新增 `buildCanvasView()`（由原 `drawChart()` 前 50 行原样抽出），
统一产出 K 线、叠加层、价格域、`xForIndex`/`yForPrice`，画布模块只消费它。

- `dashboard/canvas_registry.js`：`window.CPT_CANVASES`（`register/get/has/ids/list`）。
  必须**先于**画布模块加载（`defer` 按文档顺序执行），`dashboard.js` 最后 ——
  `boot()` 时要读到完整注册表。
- `drawChart()` 从"唯一实现"变成"唯一分发点"：原实现改名 `drawChartA(view)`，
  新增 3 行分发读 `state.canvas`。缩放/滚轮/双击/级别筛选/`render`/`ResizeObserver`
  **6 个调用点 + 1 个观察者一行未改**（这是选择"改分发而不是改调用点"的理由）。
- 每个画布的 `draw(view)` 必须返回**自己真正画出来的**元素个数（不是"快照里有几个"）。
  画布 A 直接数 DOM 节点（K 线按 `data-bar-index` 去重：一根 K 线画影线+实体 2 个节点，
  直接数会得到 2 倍 —— 首轮审计实测 360 vs 180）。

### 四个画布

| 画布 | 实现 | 中枢表达 | 离线资产 |
|---|---|---|---|
| A | 原手写 SVG（行为未改，仅新增窗口过滤） | SVG rect | 无依赖 |
| B | lightweight-charts 4.2.0 | 两条虚线边界（LWC 无矩形图元） | 163 KB，Apache-2.0 |
| C | plotly 2.35.2 finance 构建 | `layout.shapes` 真矩形 | 1.17 MB，MIT |
| D | 服务端 wbt `HtmlReportBuilder` 报告外壳 | plotly rect | 复用 C 的 plotly + bootstrap |

`dashboard/vendor/` 内联 6 个资产（≈1.87 MB，含 sha256 清单与许可证，
`tests/test_dashboard_canvas_contract.py` 逐字节校验哈希）。用 `plotly-finance-dist-min`
而不是完整 plotly：完整包 4.6 MB，只为画 K 线浪费 4 倍体积。

### 画布 D 的一处**计划偏差**（依据实测，不是妥协）

总计划 §6 把 D 定为 "wbt report"。实测 `references/wbt/python/wbt`：

1. **wbt 画不了 K 线**：全仓没有 `go.Candlestick` / `go.Ohlc`，5 个 tab 全是
   净值/回撤/分布/绩效表，零价格图；
2. **wbt 报告外壳带 6 个 CDN 外链**（Google Fonts ×3、bootstrap CSS/JS、
   bootstrap-icons），离线打开会丢样式、tab 失效。

⇒ D 的落地方式：**真实复用 wbt 的 `HtmlReportBuilder`**（`add_header`/`add_metrics`/
`add_chart_tab`/`add_table`/`add_footer`/`render`）产出报告外壳，CPT 侧丢弃 `<head>`
外链、只取 `<body>`，K 线用 plotly 补上（`include_plotlyjs=False`，页面已 vendor
plotly）。渲染在**同源 iframe** 里 —— wbt + bootstrap 的样式会重排全局
（`.container`/`.table`/`.nav-tabs`），直接注入主页面会打乱现有看板
（R12 刚验过 375px 移动端触摸目标与水平溢出）；同源 iframe 下父页面仍能读
`contentDocument`，审计照做。

服务端 `/api/canvas/wbt?start_ms=&end_ms=`（`cpt/application/canvas_wbt.py`）：
客户端**必须传可视窗口**，否则只画窗口的 A/B/C 与画全量的 D 计数必然不等。
`innerHTML` 不执行 `<script>` ⇒ 片段里的 plotly `newPlot` 与 wbt 主题脚本被抽出来
由父页面重建元素执行；**外链脚本一律剥离**（wbt 模板的 bootstrap CDN script 就在
body 里，`_CDN_RE` 会对残留外链响亮失败而不是静默降级）。

### 两处 R16-5 顺带修掉的真 bug（都是审计先发现的）

1. **`?snapshot=` 完全失效**：`root.dataset.snapshotUrl || params.get("snapshot")`
   —— 属性在 index.html 里恒非空，参数永远走不到，文档里写的"file:// 内联 JSON
   离线"是死代码，离屏审计也无法把页面指到固定快照。改为参数优先。
2. **窗口外的笔/分型被"钳到图边缘"**：`timeIndexOf` 会把任意时间戳夹到 0 或
   length-1，于是窗口外的笔被画成贴着左右边框的假线段。改为在 `buildCanvasView()`
   里统一按 `overlapsWindow` 过滤四种结构（中枢本来就有这个过滤），既消灭假线段，
   也让"四个画布消费同一批元素"有了定义。
3. 分发前不检查 `view.ready`：未就绪时 `view` 里没有 `geom`/`windowStart`，画布
   B/C/D 会拿 `undefined` 拼请求参数（审计实测画布 D 发了 `start_ms=undefined` 的
   400 请求）。现在未就绪统一走 `renderCanvasPlaceholder`，不交给画布模块。

### 验收证据（Playwright，`audit_R16.js`，真机 `https://127.0.0.1/cpt/`）

四画布计数（真实 Binance 1h 快照，可视窗口 180 根）：

| 画布 | candles | fractals | bis | zhongshus | trendTypes | 绘制物证 |
|---|---|---|---|---|---|---|
| A | 180 | 67 | 10 | 2 | 2 | 447 个 SVG 图元 |
| B | 180 | 67 | 10 | 2 | 2 | 7 个 `<canvas>` 位图（672×1298 等） |
| C | 180 | 67 | 10 | 2 | 2 | plotly 503 个图元 |
| D | 180 | 67 | 10 | 2 | 2 | iframe 内 wbt 6 指标卡 + 2 数据表 + 1 tab 导航 + plotly 315 图元 |

- **`comparison` 五项全 true**（两两相等）
- **`externalRequests = 0`**（离线可用：全部资源同源）
- `consoleErrors = 0`、`pageErrors = 0`、P0/P1/P2 = 0/0/0
- 顶栏 4 个按钮 A/B/C/D，点 B 后 `data-canvas=B`、`aria-pressed=true`、
  URL 写回 `?canvas=B`
- 产物：`screenshot-R16-canvas-{A,B,C,D}.png`（各 ~155 KB）、`audit_R16.json`

**踩坑记录**（全部由审计/测试先发现）：K 线节点 2 倍计数、画布 C 因"查表找 K 线"
丢掉起点在窗口前的笔（9 vs 10）、`Object.assign(counts, ...)` 把 `pending`/`library`
污染进计数、`wbt.__version__` 不存在（版本必须走 `importlib.metadata`，否则误报
"版本不符"把画布 D 打成不可用）。

**验收**：`305 passed`（R16-4 的 278 + 27）；ruff check / ruff format --check
(128 files) / mypy(62 files) / vulture / import-linter(4 kept, 0 broken) 全绿。
CI 无 wbt ⇒ 画布 D 的渲染断言 `importorskip`（已用 meta_path 屏蔽 wbt 模拟 CI：
13 passed, 2 skipped）。

## R17 — 数据源扩充

**目标（总计划 §7）**：加密走 czsc `ccxt_connector` 那条路（ccxt 公开端点，
无鉴权）；A 股以 **Wind 为主通道**，公开源只作兜底。

### 第一件事不是"再加一个源"，而是把源的状态变成一等对象

R15/R16 的踩坑都是同一类 —— **某个源悄悄不可用，链路静默降级**：A 股快照
`overlays` 恒为空没报错；R14 的 czsc 后端从未接进生产没报错；A 股 98.2% 的代码
缺复权因子只在取数时才暴露。所以 R17 先落地
`cpt/adapters/source_registry.py` + `GET /api/dashboard/sources`：

- `SOURCES` 是**声明**（静态、可单测、不联网）：id / 市场 / 角色（primary /
  fallback / local）/ 提供的能力 / 需要的可选依赖 / **额度代价**；
- `probe_source()` 是**实测**：任何异常都折叠成 `status`（ok / degraded /
  unavailable / skipped），**绝不让 `/api/dashboard/sources` 500**；
- **Wind 默认 `skipped`**：一次探测就是一次真实万得额度，必须显式
  `?include_quota=1` 才允许 —— 默认路径绝不花配额（有专门的测试钉这条纪律）；
- 探活结果 60s 进程内缓存，`?refresh=1` 强制重探（别把看板刷成 DDoS）；
- `known_dead_endpoints` 留档已知不可用端点，避免每轮重复侦察。

### 四个新数据源

| 源 | 文件 | 角色 | 依赖 | 实测 |
|---|---|---|---|---|
| ccxt 统一交易所接口 | `cpt/adapters/ccxt_source.py` | 加密兜底 | extra `crypto` | ok，255ms |
| 腾讯 fqkline 后复权日线 | `cpt/adapters/a_share_public.py` | A 股兜底 | 无 | ok，285ms |
| 新浪 hq.sinajs.cn 快照 | `cpt/adapters/a_share_public.py` | A 股兜底 | 无 | ok，394ms |
| Wind CLI 主通道 | `cpt/adapters/wind_source.py` | A 股主通道 | wind CLI + key | **skipped（默认不探测）** |

**ccxt** 不转手 czsc 的 `ccxt_connector`：那个模块 ① 顶层 `import ccxt`（缺依赖时
连 czsc 都导入失败）② 依赖 loguru ③ 返回 pandas DataFrame。CPT 要的是"请求 →
CanonicalBar"的窄接口，所以直接用 ccxt，只沿用它的选所与代理约定。

**Wind** 的通道形态是实测出来的：本机没有 WindPy，取数走
`cd <wind-mcp-skill> && node scripts/cli.mjs call <server_type> <tool> '<params_json>'`，
成功时数据在 `content[0].text`（JSON 字符串），失败时 stdout 是
`{"ok":false,"code":...}`。**额度不足必须与"源坏了"分开报**：实测 Wind 的
"试用已到期"不带任何英文关键词，所以 `_QUOTA_MARKERS` 专门收录了中文提示，
否则运维会去查网络而不是去充值。

### 三个必须写进代码的坑（都有回归测试）

1. **腾讯 `fqkline` 的字段顺序是 `[日期, 开, 收, 高, 低, 量]`，不是 OHLC**。
   实测 `["2026-09-24","8838.081","8764.863","8872.523","8731.378","31239"]` 是
   开 8838.081 / 收 8764.863 / 高 8872.523 / 低 8731.378。按 OHLC 解析会得到
   "最高价 < 收盘价"的坏数据**且不会报错**。适配器逐根做 OHLC 自洽校验，
   不自洽就报错。
2. **复权口径必须与本地库一致**：`public.daily_bar × asel.ref_adjust_factor` 是
   **后复权**，所以腾讯侧请求 `hfq`（同一天后复权 8838.081 vs 不复权 1250.010，
   因子 ≈ 7.07）、Wind 侧 `aftype="1"`。口径不一致会让兜底源和主源画出完全不同的笔。
3. **`validate_ashare_bars` 的默认周期**原本是 5 分钟（`DEFAULT_INTERVAL_MS`），
   调用方忘传 `interval_ms` 就会收到 `open_time+300000-1` 的契约报错 —— 已改为
   新增的 `A_SHARE_DAILY_INTERVAL_MS = 86_400_000`。

### 顺带修掉的真 bug

- `cpt/adapters/a_share_local.py::_to_wind_code` 的**前缀判断顺序**错误：
  `code.startswith(("6","9","5"))` 排在 `("4","92")` 前面，导致北交所新代码段
  `920025` 被推成 `920025.SH`，Wind 查无此码。已把 `92/43/83/87/88` 提前，
  公开源适配器同口径（并拒绝未知交易所后缀，而不是静默丢掉 `.XX`）。

### 端到端验证：公开源真的能喂进结构引擎

腾讯后复权日线 300 根 → `validate_ashare_bars` → `compute_domain_structures`
（`RulesConfig()`，两个后端）：

| 代码 | 后端 | bars | 分型 | 笔 | 中枢 | 笔中位跨度 | 短跨度(<4天)占比 |
|---|---|---|---|---|---|---|---|
| 600519 | native | 300 | 99 | 98 | 16 | **3 天** | **51.0%** |
| 600519 | czsc | 300 | 96 | 14 | 2 | **21 天** | **0.0%** |
| 300059 | native | 300 | 113 | 112 | 21 | **3 天** | **54.5%** |
| 300059 | czsc | 300 | 110 | 24 | 4 | **15 天** | **0.0%** |

与 R14（oracle fixture）/ R16（本机真库 64 根）的结论完全同向：**自研 native
后端产出的笔严重退化**（中位跨度 3 天、过半笔短于 4 天），czsc 后端正常
（中位 15–21 天、短跨度 0%）。这次是**通过完全独立的公开数据通道**复现的。

### 验收证据（Playwright，`audit_R17.js`，真机 `https://127.0.0.1/cpt/`）

| 源 | 角色 | 状态 | 延迟 | 额度 |
|---|---|---|---|---|
| binance_futures | primary | ok | 32 ms | none |
| ccxt | fallback | ok | 256 ms | none |
| tencent_kline | fallback | ok | 285 ms | none |
| sina_quote | fallback | ok | 394 ms | none |
| a_share_local | local | ok | 164 ms | none |
| wind | primary | **skipped** | 6 ms | **wind** |

- `/cpt/api/dashboard/sources` 200、`schema_version=sources.v1`、6 个源齐全
- `?markets=crypto` → `[binance_futures, ccxt]`；`?markets=a_share` →
  `[a_share_local, sina_quote, tencent_kline, wind]`
- **Wind 默认 `skipped`**（额度纪律），`include_quota=false`
- **R16 四画布不回归**：A/B/C/D 计数仍然全等（180/66/10/2/2）
- `externalRequests=0`、consoleErrors=0、pageErrors=0、**P0/P1/P2 = 0/0/0**
- 产物：`screenshot-R17-sources.png`、`audit_R17.json`

**验收**：`370 passed`（R16-5 的 305 + 65）；ruff check / ruff format --check
(136 files) / mypy(66 files) / vulture / import-linter(4 kept, 0 broken) 全绿。
CI 无 ccxt/wbt/psycopg/pandas/plotly ⇒ 用 meta_path 屏蔽这些包模拟 CI：
77 passed, 3 skipped（全部落在 `importorskip`）。

**待人类拍板（消耗真实额度，未擅自执行）**：是否花 1 次 Wind 额度实探当前
`WIND_API_KEY` 是否有效；以及 98.2% 缺复权因子的 backfill 规模（Wind 不返回
因子，`fetch_adjust_factors` 需要**每只标的 2 次调用**，5225 只 ≈ 10450 次）。

## R17-2 — Wind 额度实探（已获授权）+ 两个数据缺陷

用户批准后花掉 **2 次真实 Wind 额度**（台账 `~/.cache/cpt/wind_quota.jsonl`）：
1 次 `get_stock_price_indicators` 探活、1 次 `get_stock_kline` 验证解析链路。
结论：**`WIND_API_KEY` 有效，Wind 主通道可用**（探活 3.49s 返回真实数据）。

### 实探立刻打出一个 P0 解析 bug（假形状测试给的安全感是假的）

R17 的 `parse_wind_kline` 之前只认 list-of-dicts 与"列名→数组"两种自造形状，
**真实回执是第三种**：

```json
{"data": {"columns": [{"name": "TIME"}, {"name": "OPEN"}, {"name": "MATCH"}, ...],
          "rows": [["2010-01-04T00:00:00.000+02:00", "91.55", "90.45", ...], ...],
          "unit": {}}}
```

拿真实回执跑，直接 `WindSourceError`。修的过程中又发现一个**更危险**的变体：

- 真形状的内层**同时有 `rows` 键**，通用解包循环会先把 `columns` 丢掉，于是位置
  数组被当成 `[TIME, OPEN, MATCH, HIGH, LOW, ...]` 硬读 —— 而实测列序第 6 列是
  `TURNOVER`（成交额 753405635）而不是 `VOLUME`（成交量 4430488），**差 170 倍且
  不报任何错**。修法是表格检测放进解包循环内部，按 `columns[].name` 建索引。
- `TIME` 实测是 `2010-01-04T00:00:00.000+02:00`。按瞬时时刻换算成 UTC 会得到
  `2010-01-03T22:00Z`，**日线日期整整退一天**。必须只取前导日期。
- MCP 外壳的 `isError: true` 失败信封（错误文本在 `content[0].text` 里）之前完全
  没处理，会被当成成功返回。

三处都补了回归测试，并且**用真实回执形状**（`REAL_WIND_KLINE`，逐字段照抄实测）
钉死，不再只测自造形状。

### 缺陷 1：本地库 `public.daily_bar` 缺 2026-09-22 整个交易日

```sql
select date, count(*) from public.daily_bar
where date between '2026-09-14' and '2026-09-25' group by date order by date;
```

除 2026-09-22 外每个交易日都有 5,206–5,221 行，**2026-09-22（周二）为 0 行**。
腾讯与 Wind 两个**独立**通道都确认当天有成交（成交量 24,573 手 / 2,457,294 股，
吻合到 0.006%）。同一窗口内 2026-06-19 也缺整天，但那**是**端午节，属正常。

`/api/dashboard/sources` 的本地源探针原本只报总行数，"少了一整天"完全隐形；
已加**缺整天 + 低覆盖日检测**，现在它报 `status=degraded`、
`missing_weekdays=["2026-09-22"]`。

### 缺陷 2：口径判定（**含一次自我纠错**）

**第一版结论是错的，已改正。** 第一版用 600519 的 2026-09-17..24 五天窗口比对，
发现腾讯 `qfq` 与本地库逐日 ratio=1.000000，就下了"本地库是前复权"的结论。
**错在窗口选取**：那五天没有除权事件，而 `bfq == qfq` 恰恰只在无除权区间成立 ——
这个窗口**在原理上就无法区分不复权与前复权**。

换成跨除权日的日期后结论反转（腾讯 `bfq` / `qfq` 对照本地库）：

| 代码 | 日期 | 本地库 | 腾讯 `bfq` | 腾讯 `qfq` |
|---|---|---|---|---|
| 600519 | 2025-07-01 | 1405.100 | **1405.100** | 1353.119 |
| 600519 | 2025-07-15 | 1411.000 | **1411.000** | 1359.019 |
| 600519 | 2026-01-05 | 1426.000 | **1426.000** | 1397.976 |
| 000002 | 2025-07-01 / 07-15 / 2026-01-05 | 6.350 / 6.600 / 4.750 | 同值 | 同值 |

⇒ 本地 `public.daily_bar` 是**不复权原始价**。R15 文档里"后复权"的说法同样不准确，
注册表 note 已按实测改写。

**顺带验证了 R15 的后复权计算是对的**：000002 本地 3.760 × `hfq_factor` 335.6787
= 1262.152，与腾讯 `hfq` 的 1262.152 **完全相等**。也就是说
`raw × asel.hfq_factor` 这条链路能精确复现第三方后复权序列。

**教训（已写进验收纪律）**：判定复权口径必须选**跨除权日**的日期，否则
`bfq == qfq`，任何比对都会给出"相等"的假阳性。

**影响面**：笔/中枢等结构判定对价格整体缩放不变，所以口径差异不影响已有的结构
结论（含 native 笔退化的结论）；但**两通道序列绝不可混用**，跨源校验必须先统一口径。
另：腾讯 `hfq` 与其自身不复权价的比值在 5 个交易日内漂移 0.49%（无除权区间本应
恒定），这一条仍需单独复核，暂按"兜底源默认口径待定"挂起。

### 验收

`379 passed`（R17 的 370 + 9）；ruff check / ruff format --check(136 files) /
mypy(66 files) / vulture / import-linter(4 kept, 0 broken) 全绿；CI 无可选依赖
模拟：86 passed, 3 skipped。

`audit_R17.js` 复跑：**P1 = 1**（`a_share_local 降级：缺 1 个工作日整天：2026-09-22`，
evidence 里带 `missing_weekdays` / `median_bars_per_day` / `low_coverage_days`），
P0 = 0、P2 = 0、外部请求 0、页面异常 0、四画布计数仍全等。审计脚本原本只校验
status 合法（`degraded` 合法所以被放过），已改成**把 degraded 顶成 P1** ——
合法状态不等于健康状态。

### 台账可归属化

`~/.cache/cpt/wind_quota.jsonl` 里除我自己那 2 条 `ok:true` 之外，还有 13 条
**空参数 + `duration_ms=0.0` 的 `TIMEOUT`** 记录。这不是本模块能产生的：本模块的
超时路径记的是 `~timeout` 毫秒（默认 90000），而 0.0ms 意味着执行器瞬间抛
`TimeoutExpired`；且公开方法（`fetch_daily_bars` / `fetch_adjust_factors`）都不可能
发出空参数。全盘 grep 确认只有 `cpt/adapters/wind_source.py` 引用该路径，且当时
无其它进程在跑 Wind。

结论：**台账是共享追加文件，存在无法归属的写入方**。已给每条记录加 `pid` 字段，
后续任何一笔额度消耗都能定位到进程，不再靠推断。

---

## R17-3 A 股主看板 UI（市场切换 + 四画布复用）

### 目标

R17 把 A 股的**后端**链路打通了（Wind 因子 backfill / 日线接入 / 热门池 / 自选），
但看板上**没有任何 A 股入口** —— 只能靠 `python -m cpt.web.a_share --code XXX
--port 8011` 另起一个进程、自己在浏览器里开第二个页面看。R17-3 把 A 股接进主看板。

### 落地方案：换 snapshot URL，不碰画布

A 股快照走的是与加密侧**完全同构**的 `dashboard.v2` schema，所以接入点只有一个：
**换掉 `loadSnapshot()` 的 URL**。四个画布（A/B/C/D）零改动 —— 这条性质由测试钉住
（`tests/test_dashboard_ashare_contract.py::test_render_canvas_modules_are_market_agnostic`：
canvas_b.js / canvas_c.js 里不允许出现 `a_share` 或 `market` 字样）。

改动清单：

| 文件 | 作用 |
| --- | --- |
| `cpt/application/a_share_snapshot.py` | **新**：`build_ashare_snapshot` 从 web 层迁到 application 层（两个调用方：独立 A 股服务 + 主看板路由）；`DEFAULT_WIDTH_K` 30 → **120** |
| `cpt/web/a_share_routes.py` | **新**：三条路由的载荷构造（snapshot / pool / watchlist） |
| `cpt/web/app.py` | 新增 `/api/dashboard/a-share/{snapshot,pool,watchlist}`，**在 `provider()` 之前分发**；`do_POST` / `do_DELETE` |
| `cpt/adapters/a_share_local.py` | 新增 `AShareNoDataError` / `AShareNoFactorError`（原来两者都抛同一个 `AShareLocalError`） |
| `dashboard/market_a_share.js` | **新**：市场切换 + 代码下拉 + `?market=&code=` |
| `dashboard/index.html` / `.css` / `dashboard.js` | 顶栏市场切换、A 股选择器、降级文案、A 股模式停轮询 |
| `dashboard/canvas_d.js` | 透传 `code`（见下文"跨市场错配"） |

### 为什么默认宽度从 30 改成 120

30 根（≈1.5 个月）实测只能出 **2 笔**（`000002`），笔/中枢的形态根本看不出来；
120 根（≈半年）才是缠论结构的可读尺度。带因子的 61 只**全部**有 ≥250 根历史，
所以调大默认值**零覆盖损失**。

### 三个实测踩到的坑

**1. 画布 D 跨市场错配（最严重）**

D 与 A/B/C 的取数方式**根本不同**：A/B/C 画客户端那份快照，而 D 是
`/api/canvas/wbt` **服务端**取数的。不带 `code` 时服务端拿的是 provider 的
**加密**快照，于是出现 A/B/C 画 123 根 A 股 K 线、D 画 **579 根 BTCUSDT** K 线，
**一屏两个市场**。这个错配在"四画布计数全等"的旧断言下是**看不出来**的 ——
旧审计逐个画布独立断言，只要每个画布内部自洽就放过。已改为跨画布比对，
并加了后端回归测试（`test_canvas_wbt_route_with_code_uses_ashare_snapshot`）。

**2. 中文错误消息会**断开**HTTP 连接**

`BaseHTTPRequestHandler.send_error()` 把 message 写进 HTTP **状态行**，而状态行
只能 latin-1 编码 —— 任何中文提示都会抛 `UnicodeEncodeError` 并直接断连接，
浏览器侧只看到 `RemoteDisconnected`（网络错误），看不到原因。所有 A 股 400 改成
JSON 错误体（`_write_json_error`）。

**3. `select.value` 写标签而不是值 → 控件显示空白**

`interval-select` 的 option `value` 是**毫秒**（`86400000` = 1d），写
`interval.value = "1d"` 匹配不到任何 option，select 会显示成空白。

### 失败语义（A 股特有）

全库 5225 只里只有 **94 只有复权因子**，"点进去是空图"是常态而非异常，所以
失败必须分类说清楚：

| reason | 含义 | 前端文案 |
| --- | --- | --- |
| `no_factor` | 有行情但整段缺因子 | 该代码缺复权因子，画不出后复权序列（94/5225） |
| `no_data` | `public.daily_bar` 里没有该代码 | 本地库里没有该代码的行情 |
| `db_error:*` | DB 不可达 | 保留原始异常类型名 |
| `invalid_code` | 代码格式非法 | HTTP 400 + JSON 体 |

热门池列表里缺因子的票**仍然列出但禁用并标注"缺因子"** —— 直接过滤会让人以为
池子少了票（100 只里 6 只缺因子）。

### 验收

`409 passed`（R17 的 379 + 30）；ruff check / ruff format --check(140 files) /
mypy(68 files) / vulture / import-linter(4 kept, 0 broken) 全绿；
CI 无可选依赖模拟：338 passed, 28 skipped。

`audit_R17-3.js`（真机 Chromium + 公网 URL）：**P0 = 0、P1 = 0、P2 = 0**，
外部请求 0、控制台错误 0、页面异常 0。四画布计数**全等**
（`002614`：123 根 K 线 / 52 分型 / 10 笔 / 2 中枢）；缺因子代码显示明确文案；
interval 锁 `1d` 且禁用；点击市场按钮 URL 写 `?market=a_share&code=002614`。

审计脚本自身也修了两个**假绿/假红**来源：`canvasCounts` 在**首次**渲染（空快照）
就会出现，只等它存在会在数据到达前返回 0 计数（上一版因此误报"一笔都没有"）；
`state-degraded-message` 的文案是 `index.html` 里的静态 placeholder，恒非空，
等"文本非空"会立刻拿到占位文案。

---

## R17-3b 按需取因子（输入代码即自动补因子 + 重新生成快照）

### 背景

R17-3 落地了 A 股主看板，但全库 5,225 只里只有 94 只有复权因子 —— 用户输一个
代码，大概率看到"该代码缺复权因子"。全市场批量被否决（5,225 只太多），改为
**用户输入哪个代码就按需补哪个**。

### 流程

```
GET /api/dashboard/a-share/snapshot?code=600000
  └─ build_ashare_snapshot(code)
       ├─ 1. 读本地因子
       ├─ 2. 覆盖不足（整段缺 **或部分缺**）→ 腾讯 raw+hfq（2 次请求，~1.7s）
       │     └─ upsert asel.ref_adjust_factor（幂等，主键 code+trade_date）
       ├─ 3. 重读 → 生成快照
       └─ 4. 仍无 → 区分 unsupported / cooldown / fetch_failed
```

**部分缺也补**：`skipped_no_factor` 非空即触发。K 线序列中间有空洞比整段缺更隐蔽
——画出来的笔/中枢是错的，但图上看起来"有东西"。

### 关键设计：不预测，只询问

腾讯是否提供某标的的 `hfqday` 是**逐标的**属性，**无法用代码前缀预测**（实测
688111/688036 有、688981 没有；多数 301 有、近期新股没有）。所以实现里**没有任何
板块判断** —— 拉一次，然后如实报告腾讯实际返回了什么（`detail` 里带它返回的键名）。
任何"按板块硬编码"的优化都会重犯 R15-1 那两次记录错误。

### 三个踩到的坑

**1. 默认"开"导致跑测试写脏了生产库（最严重）**

早期 `default_factor_ensurer()` 在环境变量未设置时返回"启用"。结果跑一次
`pytest`，`test_snapshot_reason_no_factor_is_not_db_error` 就真的去腾讯拉了 600519
的 **800 行**因子并 upsert 进生产库（因子表 94 → 95 只）。

修法分三层：
- application 层默认**关**：`build_ashare_snapshot` 不传 `ensure_factors` 就绝不
  联网、绝不写库（直接调用它永远安全）；
- 只有**生产入口** `cpt.web.a_share_routes.snapshot_payload` 用
  `factor_ensurer_from_env(default=True)` 显式打开；
- 新增 `tests/conftest.py` 全局 autouse 夹具把 `CPT_ASHARE_ONDEMAND_FACTOR=0`，
  整个测试会话离线。跑测试前后 DB 行数一致（95 只 / 50,590 行 → 不变）。

**2. `unsupported` 被冷却降级成 `cooldown`**

同一只票第一次报 `no_factor_unsupported`、10 分钟内再问报 `no_factor_cooldown`，
用户看到的原因随请求变化，审计断言也因此**不可重复**（同一断言时而通过时而失败）。

修法：`unsupported` 是**永久**结论（同进程内不会再变），单独存 `_unsupported`
字典，命中就直接复用、不再问腾讯、也不受冷却影响；只有**可重试**的失败（网络 /
落库）才走冷却。语义更准，也少打腾讯。

**3. 审计自己写死了 600519 的因子状态**

第 5 步原本用 600519 验"缺因子必须明说"。按需补因子上线后 600519 变得**可画**
（腾讯有它的 hfqday），`state-degraded` 永远不出现 → 审计超时失败。
改用 `301689`（腾讯**没有** hfqday 的标的，永久稳定）。
7.5 步也重写为**可重复**的形式：不断言"必须发生一次拉取"（因子会落库，第二次
审计就命中缓存），而是断言每个本地无因子的代码都落到两种合法结果之一
（变成可画 / 明确报 unsupported|cooldown|fetch_failed）。

### 验收

**真实链路（非 mock）**：

| 代码 | 拉取前因子 | 结果 |
|---|---|---|
| `601398` | **0 行** | 2.5s → 123 根 K 线 / 10 笔 / 51 分型 / 1 中枢；落库 800 行 |
| `600000` | **0 行** | 123 根 K 线 / 10 笔；落库 800 行 |
| `301689` / `688825` | 0 行 | `no_factor_unsupported`，`detail` 带腾讯实际返回的键名 |
| `920201` | 0 行（且无本地日线） | `no_data`（日线入库是另一条链路，不在按需范围） |

**数据正确性独立复核**：`601398` 落库的 800 行逐行与"直接问腾讯重算"比对，
**0 行不一致**；交叉验证 `2026-09-24` DB 原始价 `8.13 × 1.621525 = 13.183`
== 腾讯 `hfq` `13.183`（精确相等）。

**DB 覆盖变化**：94 只 → **100 只**（本次实拉 6 只：600519/601398/600036/
600000/600004/600006）。

**门禁**：`432 passed`（R17-3 的 409 + 23）；ruff/format/mypy/vulture/
import-linter 全绿；CI 无可选依赖模拟 361 passed / 28 skipped。

**审计**：`audit_R17-3.js` 连跑两次均 **P0=0 P1=0 P2=0**，四画布计数全等
（123/52/10/2），0 外部请求、0 控制台错误、0 页面异常。

### 需要说明的一处副作用

上面第 1 个坑里，测试**真的往生产库写了 600519 的 800 行因子**。这批数据经复核是
**正确的**（同一条腾讯 hfq/raw 管线，且 600519 本来就在热门池里、之前因缺因子画不出来），
所以**予以保留**而不是回滚 —— 保留让它变得可画，回滚则是为了洁癖丢掉正确数据。
记录在此是因为"数据是被测试意外写入的"这件事本身需要留痕。

### 边界（未做）

- **不补日线**：`public.daily_bar` 的入库是另一条链路（`lkl` ingest）。本地没有
  日线的代码（如北交所 `920201`）仍报 `no_data`，按需拉取不覆盖这一层。
- **不做全市场批量**：用户已明确否决（5,225 只太多）。
- **冷却为进程内**：多进程/多机会各有一份冷却；当前单进程部署下够用。

---

## R17-3c A 股证券名称显示

### 需求

用户原话：**「A股要在某个适合的地方显示股票的名称，不然有时候会看岔」**。
A 股只有六位数字，`600519`（贵州茅台）/ `600815`（厦工股份）这种一眼就错。

### 名称从哪来：本地 `asel.security_master`，不花 Wind 额度

| 候选 | 覆盖（`daily_bar` 的 5,225 只） | 备注 |
|---|---|---|
| **`asel.security_master`** | **0 只缺** | 5,930 行，带 `board`（主板/创业板/科创板/北交所）；来源 sina/tencent，更新时间就是当天 |
| `public.stock_basic` | 缺 2 只 | 有 `is_st`，但覆盖率略差 |

选 `security_master`（同时也就避免了为"显示个名字"去调 Wind 花额度）。

**数据质量坑**：老行情源按 4 字宽补齐名称，80 条带**填充空白** ——
`深 赛 格`、`ST 中 侨`、`万  科Ａ`、`TCL 通讯`。直接显示很难看。

归一化必须**删掉全部空白**，不能折叠成单个空格（那样 `深 赛 格` 原样不变、没用）。
安全性已核对：80 条里 `[A-Za-z] +[A-Za-z]` 匹配数为 **0**，即没有任何一条是
"ASCII 单词之间的有意义空格"，所以删除不会吃掉真实空格。删完
`ST 中 侨` → `ST中侨`、`TCL 通讯` → `TCL通讯`、`万  科Ａ` → `万科Ａ`，都是正确写法。

### 显示在哪（用户把位置交给我定，选了三个"确认身份"的位置）

| 位置 | 形式 | 为什么是这里 |
|---|---|---|
| **顶栏「标的」** | `600519 贵州茅台`（名称加粗、非等宽） | 常驻可见，"我看的是不是那只票"第一眼就要确认 |
| **代码下拉** | `002119 康强电子（#1）` | **选错票最容易发生的地方**，必须带名字 |
| **页面标题** | `贵州茅台 600519 · CPT` | 同时开几个标签页时，看岔发生在标签栏；名称放最前（先被看到） |

标签 `<dt>交易对</dt>` 改成 `<dt>标的</dt>` —— A 股不是"交易对"。

### 顺手修掉一个真 bug：A 股模式顶栏一直写着 BTCUSDT

做这个需求时在浏览器里发现：**A 股模式下顶栏的加密交易对下拉（BTCUSDT/ETHUSDT/
SOLUSDT）根本没有隐藏**，而画布画的是 A 股。用户没法确认自己在看什么 ——
这正是"看岔"的来源，比缺个名字严重得多。

修法：A 股模式下隐藏该下拉，用纯文本显示代码；加密模式下反向。
审计里加了 **P0** 断言（`symbolSelectHidden !== true` 直接判 P0）。

### 三个设计约束（避免重犯 R17-3b 的错）

1. **名称查询不经过新参数**，走 `AShareLocalClient.fetch_security_name()` ——
   名字和 K 线**来自同一条链路**。测试注入假客户端时没有这个方法，名字自然缺席，
   **不会偷偷连真库**（R17-3b 的教训）。
2. **`empty_ashare_snapshot` 收已查好的名字**，自己**不碰 DB**；降级时反而更
   需要名字，否则用户对着空图只有一个六位数字。
3. **名字是装饰，绝不允许它把快照搞挂**：`_resolve_security_name` 与
   `a_share_routes._names` 都吞掉全部异常并降级为空。有回归测试。

另外给 `tests/test_web_a_share_routes.py` 加了 autouse 夹具把 `_names` 掐掉：
它调的是模块级 `fetch_security_names`，**不经过**那些测试 monkeypatch 的
`AShareLocalClient`，不掐就会连真库 —— 本机侥幸能过、CI（无 psycopg/无 DB）
行为完全不同。

### 另一个坑：我自己的部署命令把 vendor 目录权限改坏了

用 `sudo chmod 644 /var/www/cpt-dashboard/*` 部署时，通配符把 **`vendor/` 目录**
的执行位也去掉了（变成 `drw-r--r--`），Nginx 无法穿越该目录 →
`vendor/*.js` 全部 **403 Forbidden**，画布 B/C 的图表库加载失败。

HTML/CSS 看起来完全正常，所以很容易误判成"前端代码写错了"。浏览器实测发现
**12 条 console error** 才暴露出来。正确写法是 `chmod -R u=rwX,go=rX`
（`X` 只给目录加执行位）。已把部署步骤和这个坑写进 `deploy/README.md`。

### 验收

**真实链路**（浏览器实测，非 mock）：

| 场景 | 顶栏 | 名称 | 加密下拉 | 标题 | 控制台错误 |
|---|---|---|---|---|---|
| A股 `600519` | `600519` | 贵州茅台 | 已隐藏 | `贵州茅台 600519 · CPT` | 0 |
| A股 `600815` | `600815` | 厦工股份 | 已隐藏 | `厦工股份 600815 · CPT` | 0 |
| A股 `301689`（降级） | `301689` | 电科思仪 | 已隐藏 | `电科思仪 301689 · CPT` | 0 |
| 加密 | `BTCUSDT` | （隐藏） | 显示 | `BTCUSDT · CPT` | 0 |

- 下拉前 3 项实测：`002119 康强电子（#1）`、`000592 平潭发展（#2）`、`002413 雷科防务（#3）`
- 热门池 100 只**全部**取到名称（缺失 0）
- 布局几何实测：名称与代码同一行（y 相等）、在代码右侧、加粗（600）、
  非等宽字体、未超出视口
- **门禁**：`449 passed`（R17-3b 的 432 + 17）；ruff/format/mypy/vulture/
  import-linter 全绿；CI 无可选依赖模拟 373 passed / 28 skipped
- **审计** `audit_R17-3.js`：**P0=0 P1=0 P2=0**，四画布计数全等，0 外部请求、
  0 控制台错误、0 页面异常

### 边界

- 名称是**展示信息**，不参与任何结构计算，也不进 `RulesConfig`。
- `N`/`ST`/`*ST` 前缀是**源里的当前名称**（如 `920201` 当时叫 `N百瑞吉`），
  会随源更新变化，CPT 不做额外标注或推断。
- 不做名称搜索（按名字找代码）—— 本次只解决"看清自己在看哪只票"。

## A 股候选池三源合并（热门池 Top5 + 手输持久化 + 策略源）· 2026-09-25

### 需求（用户原话要点）

在 A 股股票池里：热门池**只留 Top5**；**加上手输入的**（要保存起来 ——
"目前第二次登录手输入的丢失"）；**加上** `/stock-legacy/strategy` 的
**置信+评分综合 top5**；**并注明来源**。

### 根因：手输为什么丢

手输的代码此前**只写进 URL 查询串**（`market_a_share.js` 的 `syncUrl()`），
全仓**零持久化**。后端其实早就有一套完整实现 —— `WatchlistStore`
（`~/.cache/cpt/watchlist.json`，带文件锁 + add/remove/list）与三条已挂载路由
（`cpt/web/app.py:324-345`）—— 但**前端从未调用过**：
`grep -rn watchlist dashboard/*.js dashboard/index.html` 零命中。
所以是"能力齐备、缺接线"，不是"没做"。

### 三个来源

| 来源 | 取数 | 口径 |
|---|---|---|
| 热门池 Top5 | `public.hot_rank` 最新日 ∪ `public.ladder_day` cont_days≥2，按 rank 排 | `fetch_hot_pool(conn, limit=5)` |
| 手输（自选） | `~/.cache/cpt/watchlist.json` | 加入时间倒序 |
| 策略综合 Top5 | `public.strategy_signal` 最新日 | **先滤 `action∈{BUY,WATCH}`，再按 `评分×0.5+置信×100×0.5` 排序** |

### 为什么直接读表而不是调 HTTP

`/stock-legacy/strategy` 由 `/home/ubuntu/DSH/longkonglong/dashboard/strategyview.py`
提供，读 `reports/strategy_*.json`；但它的 runner **同时写库**
（`lkl/strategy/runner.py:105` → `public.strategy_signal`），而 CPT 与它**同库**。
取表就与 `hot_rank`/`ladder_day` 走同一条取数路径，无新增运行时依赖，CI 可用假游标
覆盖；调 HTTP 会让 CPT 依赖那个服务在线，读 JSON 则要把另一个仓库的文件路径写进 CPT。

### 关键决策：口径 D（有实测数据支撑，不是拍脑袋）

实查 2026-09-24 的策略表发现 **`score` 与 `confidence` 是负相关的** ——
"低分高置信"的票几乎都是 `PASS`（`000607` 评分 20/置信 0.90，`000823` 评分 35/置信 0.80）。
所以「综合」口径会**实质改变选谁**：

| 口径 | Top5 的第 5 位 |
|---|---|
| 简单平均 `(score+置信×100)/2` | `000823`（评分仅 35 的 PASS） |
| 评分权重 0.7/0.3 | `000678` |
| **先滤 BUY/WATCH 再综合（采用）** | 只有 3 只符合：`000498` / `000753` / `000678` |

把四个口径的**真实 Top5 都算出来**给用户看，用户选了"先滤动作再综合"。这也解释了
为什么 `fetch_strategy_top` 的过滤与排序放在 **Python 而不是 SQL**：`eligible` 是可调
业务口径，放进 SQL 字符串就只能靠真库才能验证。

### 合并去重（必须，不是优化）

`000592` 可以**同时**是热门池 #2 和策略 42 分。不去重的话下拉里会出现两个 `value`
相同的 option，选中哪个结果一样，而 `selected` 归属还会变得随机 —— 正是"选错票"最容易
发生的地方。合并后每条带 `sources: [...]`（全部来源，按优先级排序）与 `group`
（决定 optgroup 归属）。

**分组优先级 `manual > hot > strategy`**：手输排第一，否则用户会以为手输又丢了 ——
而"手输丢失"正是这次要修的问题。策略排最后，它是外部观察信号，不是本系统的判断。

### 落地方案

- 新增 `cpt/adapters/strategy_signal.py`：`fetch_strategy_top` + `combined_score`
- `cpt/adapters/a_share_pool.py`：`fetch_hot_pool(conn, *, limit=None)`（默认全量，
  由调用方收敛，保留"展开全部"的能力）
- `cpt/web/a_share_routes.py`：`pool_payload()` 改三源合并，`schema_version` 升
  **`a_share_pool.v2`**；三个来源的失败**互不影响**（`factor_error` /
  `strategy_error` / `watchlist_error` 分别记录）
- 前端：`renderPicker()` 用 `optgroup` 按来源分组 + 每条标注全部来源；
  手输提交改走 `submitManual()`（**先 `POST /watchlist` 落盘再切图**）；
  新增「移除手输」按钮（`DELETE /watchlist`）；顶栏新增当前票的来源标注

### 三个踩到的坑

1. **`strategy_signal.name` 是策略名称，不是股票名**。实值是「默认多头趋势」，
   股票名要另查 `stock_basic`。字段名一样但语义不同，直接拿来显示会把策略名当股票名
   写进下拉。已在 `StrategyPick.strategy_name` 与 docstring 里显式钉住。
2. **`confidence` 是 `numeric(4,3)` → psycopg 回 `Decimal`**。不转 float 会在排序时抛
   `TypeError`，且 `json.dumps` 无法序列化 `Decimal` → 路由直接 500。测试里特意用
   `Decimal` 造数据，用 float 会把这个坑测没。
3. **`pool_payload()` 一读自选，池子测试就会去读真实的自选文件**。第一次跑出现
   3 例失败（count 多 1），根因是 `~/.cache/cpt/watchlist.json` 里恰好有一条手输 ——
   **本机假红、CI（无该文件）假绿**。加了 autouse 夹具 `_isolated_watchlist` 把落盘
   路径指到 `tmp_path`。这与该文件已有的 `_no_real_name_lookup` 是同一类防护。

### 验收

- **端到端实跑**（真服务 + 真库，非 mock）：

  | 步骤 | 结果 |
  |---|---|
  | ① 首次登录（空自选） | count=8，`groups={hot:5, manual:0, strategy:3}` |
  | ② `POST /watchlist?code=600519` | count=9，`600519 贵州茅台` 出现在**手输组首位** |
  | ③ **关掉服务 = 模拟第二次登录** | count=9，**600519 仍在** ← 用户报的 bug 已修 |
  | ④ `DELETE /watchlist?code=600519` | count=8，`removed=true` |

- 真实数据：热门池 100 → **5**；策略 9 行 → 滤后 3 只；去重与来源标注均生效
- **真实浏览器实测**（chromium headless 对线上站点 `--dump-dom`，读的是浏览器**实际
  渲染**结果而非源码断言）：

  ```
  【手输（已保存）】002614 奥佳华（手输）                      ← 选中
  【热门池 Top5】   002119 康强电子（热门#1）…301689 电科思仪（热门#5·本地无因子）
  【策略综合 Top5】 000498 山东路桥（策略88分/置信92%·BUY）…000678 襄阳轴承
  来源标注节点 = 手输；移除按钮可见（当前票确实是手输项）；无 JS 报错
  ```

- **门禁**：`446 passed, 0 failed`（上一轮 424 + 22 例新增）；ruff/format(135)/
  mypy(65)/vulture/import-linter 全绿

  > 本轮前半段有 2 例 chromium 测试失败，当时判定是本会话 `workspace-write` 沙箱
  > 不让 chromium 写 `/dev/shm` 与 `~/.config`。后来沙箱放宽为 `danger-full-access`，
  > 这 2 例**立刻通过** —— 判断得到证实，失败确与代码无关。

### 边界

- 自选**没有用户概念**：单用户看板，全库一份。多用户要换成带 user 键的实现。
- 策略候选可能**不足 5 只**（口径 D 会滤掉 `PASS`）。当前数据下只有 3 只 ——
  这是口径的预期结果，不是取数缺失。
- 热门池 `limit=None` 的全量能力保留但**暂无 UI 入口**（"展开全部"未做）。

---

## 遗留问题修复收口（F3-②③④ + 去重归档）· 2026-09-25

### 背景

上一轮审核+验证留下三件待办（见 `docs/handoff-20260925-leftover-fixes.md` §3），
用户指令是「遗留问题都要修」。本轮把 F3-②③④ 三件全部落地，并顺手把
「去重判定」的结论固化成文档。

### F3-② 合并 `_bar_to_dict`

两处函数体逐字相同（`asdict(bar)` + 补派生 `direction`），分别在
`cpt/application/export.py` 与 `cpt/application/dashboard.py`。

**风险**：导出那份是**已冻结的 schema v1**（`data` 子树参与 `dataset_hash`），
看板那份是 UI 载荷 —— 今天同形是巧合，直接合并会让"看板顺手加个字段"
**静默改掉导出格式**。

**做法**：抽到 `cpt/application/_bar_dict.py::bar_to_dict`（同层，不违反层契约），
模块顶部写**显式 warning**（改键集合 = 改导出格式，必须升 `EXPORT_SCHEMA_VERSION`
+ 同步 `docs/export-schema-v1.md`）；两处改为 import。

**配套守卫**：新增
`tests/test_dataset_hashes.py::test_export_bar_keys_are_frozen_schema_v1`，
把 bar 键集合钉成 **13 个键的冻结字面量**（12 个 `CanonicalBar` 字段 + 派生 `direction`）。

**做了反向验证**（不验证就等于加了个摆设）：往 `bar_to_dict` 里插一个
`__drift_probe__` 字段 → 该测试**立刻 RED**；还原后 **GREEN**。

### F3-③ 新增 `docs/duplication-triage.md`

归档 `docs/audit/audit-20260925.json` 三个去重字段的逐组判定。

**没有照抄交接文档的数字，而是逐组重新取证**：对 27 组 `dup_names` 用
`ast.parse` 取出**当前仍存在**文件里的同名定义，剥掉 docstring 后对函数体做
**结构哈希**再分组。结果：

| 字段 | audit 规模 | 复核结论 |
|---|---|---|
| `dup_names` | 27 组 | **4 组已消解 / 23 组同名不同义 / 0 组真重复** |
| `dup_bodies` | 1 组 | 两个测试文件各自的 `date` helper，纯样板，**不动** |
| `dup_blocks` | 21 组 | 全为结构性误报；5 组随文件删除消失，1 组（HTTP 样板）由 F3-④ 收口 |

**纠正了交接文档的两处不实**（这是本轮取证的主要收获）：

1. 交接文档称 `connection_kwargs` 出现 **3 处**（含 `cpt/adapters/a_share_pool.py`）——
   **错**。`a_share_pool.py` 里 `grep -n connection_kwargs` **零命中**，它只接收调用方
   传入的 `conn`（`conn.cursor()`），**从来没有**同名函数。实际是 **2 处**
   （`a_share_local.py:131` 与 `scripts/factor_backfill.py:84`），都是转调
   `_shared_connection_kwargs(exc_type=...)` 的薄包装。
2. 交接文档称 HTTP server 样板有 **7 处** —— 那是按**文件数**算的；
   按**出现次数**实测是 **8 处**（`test_review_m7_fixes.py` 一个文件里就有 2 处）。

另核实 `dup_names` 里有一组**边界情况**刻意不合并：`direction` 在
`domain/contain.py:121` 与 `domain/models.py:101` 的函数体**逐字同构**，但它们是
两个**不同 dataclass** 各自实现 `BarLike` 协议属性 —— 合并会让两个平级模块互相依赖，
而真正的契约收口点已经在 `domain/types.py` 的 `BarLike` 协议里。

### F3-④ 抽 `tests/conftest.py::served()`

`threading.Thread(target=server.serve_forever, daemon=True)` 这段样板实测
**8 处 / 7 文件**，每份都要自己写 `shutdown` / `server_close` /
`thread.join(timeout=2)` 三连，漏一处就泄漏线程与端口。

统一收进 `served(provider)` 上下文管理器：进入 yield base URL，退出保证三连回收。

**刻意不改各测试的既有假设**：仍绑 `127.0.0.1`、仍 `port=0`（内核分配）、
`join` 超时仍是 **2s**。另：`cpt` 在函数体内**延迟导入**，维持 conftest
「不在 collection 阶段拖入被测包」的既有约束（该文件第一段注释专门解释过原因）。

收口后 `grep -rn "target=.*serve_forever" tests/` 只剩 `conftest.py` 一处定义。

### 验收

- **全量门禁实跑**（§5 与 CI 一致）：

  | 门禁 | 结果 |
  |---|---|
  | `ruff check cpt tests scripts` | All checks passed |
  | `ruff format --check cpt tests scripts` | **138 files** already formatted |
  | `mypy cpt scripts` | Success, **67** source files, 0 error（+1 = 新增 `_bar_dict.py`） |
  | `lint-imports` | **3 kept, 0 broken** |
  | `pytest tests -q -o addopts=""` | **461 passed**（上一轮 460，+1 = 键集合冻结守卫） |
  | `vulture --min-confidence 60 cpt whitelist.py` | 零输出、exit 0 |

- **反向验证**：F3-② 的守卫做了"插字段 → RED → 还原 → GREEN"双向确认。

### 踩到的坑

1. **`edit` 报 `file changed since it was read`**（§6 坑 1 复现）：用脚本批量改完
   测试文件后再用 `edit` 补 import，直接被挡。**必须重新 `read` 再 `edit`**。
2. **脚本替换 import 块会把新 import 插错位置**：我先用字符串替换把
   `from tests.conftest import served` 插到了 `import pytest` **之前**，
   切断了第三方 import 块（ruff 的 `I` 规则会红）。改 import 块要整块替换，
   不能插单行。
3. **`ruff format` 会二次改写**：手写的 conftest 与测试文件过了 `check` 但没过
   `format --check`（各一处空行），`ruff format` 直接改掉 —— 与坑 1 是同一类问题。

### 边界（未做）

- `dup_bodies` 那组测试 `date` helper **不合并**：4 行样板，为零生产影响引入跨测试
  文件 import 不划算（文档里写明了将来第三个使用方出现再提 `conftest.py`）。
- `dup_names` 的 23 组「同名不同义」**全部保留**：判定口径是"看起来一样不等于应该
  合并"，合并的代价是把两个独立演进的东西焊死。

---

## 两件人工待办收口（前端部署 + source 迁移）· 2026-09-25

上一轮交到全局收件箱的两件事（`imuh6z12x-rzkhxh` / `imuh6z136-yjd1fg`），本轮经用户
批准后**全部执行完毕**。两件都在复核阶段被证伪或纠正了原始描述。

### 一、前端文案修复部署 ✅

**原描述**：「需 sudo，本会话 approval 关闭且 sudo 完全不可用（no-new-privileges）」。

**实测证伪**：

| 检查 | 结果 |
|---|---|
| `grep -i NoNewPrivs /proc/self/status` | **`NoNewPrivs: 0`**（根本没设该标志） |
| `sudo -n id` | **`uid=0(root)`，exit 0** —— sudo 完全可用 |
| `/var/www/cpt-dashboard/` 属主 | **`ubuntu:ubuntu`** + `drwxr-xr-x` → 属主可写 |
| 写探针（无 sudo 往该目录写文件） | **成功** |

即**连 sudo 都不需要**。原判定很可能把某次 sudo 失败当成了永久状态。

**实际做法**（最小改动）：`diff` 确认 8 个资产里**只有 `dashboard.js` 有差异、且只差
第 392 行一处旧文案**，故只 `cp dashboard/dashboard.js`，**未跑 chown/chmod**
（不碰 `vendor/` 执行位，从构造上避免 README 里那个 403 坑）。备份
`/tmp/deployed-dashboard.js.bak`（md5 `4155278c…`）。

**验收（实跑，非推断）**：

- 8 个资产 + `vendor/` 递归 `diff -q` → **全 SAME**；
- 线上 `curl -sk -H "Host:140.83.62.161" https://127.0.0.1/cpt/dashboard.js`
  → **200**，与仓库版本 `diff -q` **逐字节相同**；旧文案出现 **0** 次、新文案 **1** 次；
- `vendor/` 4 个资产（lightweight-charts / plotly-finance / bootstrap.min.css /
  bootstrap-icons.woff2）全 **200**，**无 403**；
- `/cpt/`、`index.html`、`dashboard.css`、`canvas_d.js`、`market_a_share.js` 全 **200**。

### 二、`asel.ref_adjust_factor.source` 7,200 行迁移 ✅

**原描述**：「任何 `WHERE source='tx:fqkline'` 都会漏掉那 7,200 行；该表由
`/home/ubuntu/DSH/longkonglong/migrations/0002_p0_reference.sql` 建，是共享表」。

**复核纠正三处**：

1. **主键是 `(code, trade_date)`，而两组零重叠** —— `tx:fqkline` 92 只 /
   `tencent_fqkline` 9 只，`INTERSECT` 实测 **0** 对。所以 UPDATE **不可能**撞主键。
   那 9 只是：`000002 万科A`、`002119 康强电子`、`002724 海洋王`、`600000 浦发银行`、
   `600004 白云机场`、`600006 东风股份`、`600036 招商银行`、`600519 贵州茅台`、
   `601398 工商银行` —— 它们**完全没有** `tx:fqkline` 行。
2. **"任何 `WHERE source=...` 都会漏"是假设性风险，不是现状**：全仓**没有任何查询
   按 `source` 过滤**。唯一取因子值的读路径是
   `SELECT trade_date, hfq_factor FROM asel.ref_adjust_factor WHERE code = %s
   AND trade_date BETWEEN %s AND %s`（`cpt/adapters/a_share_local.py:256`），
   另一处是 `SELECT count(*)`（`source_registry.py:233`）。**所以当时没有东西被漏**
   —— 这次是消除潜在陷阱（将来谁加个 source 过滤就会静默漏 9 只），不是修 bug。
3. **建表文件不在 `longkonglong`**，实际在
   `/home/ubuntu/DSH/a_share_emotion_leader/migrations/0002_p0_reference.sql`
   （原路径**不存在**）。该项目只在迁移与一个表名清单测试里提到该表，
   **同样没有按 `source` 过滤的查询**。

**执行**：事务内先跑守卫（断言 9 只与 92 只零重叠）→
`UPDATE ... SET source='tx:fqkline' WHERE source='tencent_fqkline'`
→ **rowcount = 7,200** → 事务内校验（总行数 56,930 不变、只剩一个 source）
→ COMMIT。回滚脚本落盘 `/tmp/rollback_source_migration.sql`
（因两组 code 互斥，按 9 只 code 回滚是**精确可逆**的）。

**验收（独立连接复核）**：

- `source` 分布：**只剩 `('tx:fqkline', 56930)`**；
- 总行数 **56,930**、`distinct code` **101**（迁移前后一致）；
- 9 只各自仍是 **800 行**，日期范围 `2023-06-12 → 2026-09-24` 未变，因子区间合理；
- 抽查 600519 最近 3 个交易日：因子 `7.0855804365 / 7.0689899620 / 7.0660472165`，
  走生产读路径（`a_share_local` 原句）可正常取回；
- 全量测试 **462 passed**（无任何测试硬断言旧分布，已 grep 确认）。

> 附注：迁移只改 `source` 一列，`source_ref` 现状不变 —— 全表 49,790/56,930 有值，
> 那 7,140 个 NULL 是旧适配器版本不写该列留下的**既有**情况，与本次迁移无关。

### 踩坑

- **不要凭一次失败就断言能力不可用**：上一轮据"某次 sudo 失败"写下"sudo 完全不可用"，
  本轮 `sudo -n id` 一条命令就证伪。凡"我做不到"的结论，都要留下**当场可复现的命令
  与原始输出**，否则下一个会话会把假前提当事实继续传下去。

