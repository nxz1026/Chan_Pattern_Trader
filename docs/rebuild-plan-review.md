# rebuild 计划评审（chanlun-pro-replica → CPT 融合）

> 状态：**分歧记录·未收敛**。本文只做事实核实与依赖分析；不下结论性改动，也不代替拍板。
> 来源：`/home/ubuntu/DSH/chanlun-pro-replica-审计.md`（2026-10-17，612 行）§11「融合计划」。
> 本文所有数字均为本机实跑（复现命令见 §7），行号引用对应当前 HEAD。

---

## 0. 一句话

审计的技术结论成立，但 **§11 的施工顺序建在一个未经验证的门槛依据上**——
`min_bi_len = 6` 的两条理由（「对齐 czsc」「4–7 区间不敏感」）经复量**两条都不成立**。
口径未定就往上盖 A1/A2/B1，返工面覆盖全部信号层。

---

## 1. 授权边界（不受本文影响）

- `chanlun-pro-replica` 的 `src/chanlun/cl.py` 是商业黑盒（PyArmor 9.2.7）的**反编译产物**，
  仓库无 LICENSE，上游 `chanlun-pro/README.md:9-23` 明令禁止 decrypt / reverse-engineer /
  reimplement / redistribute。
- ⇒ **不搬代码**。允许的用法只有「读行为预期 + 读缠论文本」，实现必须独立。
- ⇒ 本文引用的 `cl.py` 行号只作为**行为参照**，不作为实现依据。

---

## 2. 现状核实：§11 六项全部未开工

grep `cpt/` 全量源码：

| §11 子项 | 关键标识符 | 命中 |
|---|---|---:|
| A1 面积背驰 | `divergence_compare == "area"` 分支 | **0** |
| A2 二买/三买 | `second_buy` / `third_buy` | **0** |
| B1 缺口分型 | `gap_fx` | **0** |
| B2 增量 API | `run_incremental` | **0** |
| C2 回测层 | `cpt/backtest/` | **0** |
| B3 多中枢 | — | 暂不接（已拍板） |

`docs/reference-audit.md` §4.1 的 chanlun-pro 对照列**仍是原文**（327/326/43），
未被后续更动。

---

## 3. 审计需更正的三处

| 位置 | 原文 | 实测 |
|---|---|---|
| `docs/rules.md:174`<br>`docs/architecture.md:434` | chanlun-pro「笔端点中位跨度仅 2 根原始K线，66–74% 不足 4 根」 | 该数字**从未由原版引擎产出**：`docs/archive/progress-log-至R12-2026-09-24.md:335` 明载 chanlun-pro 固定版**因缺 PyArmor 许可无法执行、未绕过授权**。审计 §7.4 另证该形状 = CPT 自己无门槛时的输出 |
| `docs/reference-audit.md` §4.1 | 「czsc 列可复现（202/50），该列可信」 | 202/50 是 `max_bi_num=0` → 内核回落 **50** 的截断伪影，真值 **322**。同文件已加 R68 更正，**但 `rules.md:174` / `architecture.md:434` 未同步** |
| `docs/rules.md` §9.8 | 「与 czsc `check_bi` 门槛对齐，取 6」 | **两侧量纲不同，差 2**（见 §4），对齐关系不存在 |

---

## 4. 门槛量纲：决定性证据（本机实跑）

### 4.1 两侧判据的源码位置

| 侧 | 位置 | 判据 | 量纲 |
|---|---|---|---|
| native | `emotion_cpt.structure.bi:183` | `int(end.merged_index) - int(start.merged_index) + 1 >= min_bi_len` | **merged 跨度** |
| czsc | `crates/czsc-core/src/analyze/utils.rs:413-417` | `!ab_include && bars_a.len() >= min_bi_len`，`bars_a` 从 `fx_a.elements[0].dt` 量到 `fx_b.elements[2].dt` | **merged 跨度 + 2** |

换算到缠论原文的「独立 K 线数」：

```
中心距 = merged跨度 − 1
独立K  = 中心距 − 3 = merged跨度 − 4
```

### 4.2 三份 oracle fixture 实测

`scripts/audit_gate_dimension.py`，1000 根 5m BTCUSDT。跨度全部折算到 native `merged_index` 空间：

| fixture | 侧 | g=4 | g=5 | g=6 | g=7 | g=8 |
|---|---|---:|---:|---:|---:|---:|
| 2024-02-01 | native 笔 | 144 | 112 | 98 | 82 | 78 |
| 2024-02-01 | **czsc 笔（放开截断）** | **326** | **130** | **88** | **56** | **44** |
| 2024-09-01 | native 笔 | 144 | 124 | 104 | 90 | 79 |
| 2024-09-01 | **czsc 笔（放开截断）** | **356** | **124** | **90** | **60** | **52** |
| 2025-04-01 | native 笔 | 142 | 128 | 110 | 93 | 83 |
| 2025-04-01 | **czsc 笔（放开截断）** | **352** | **142** | **74** | **62** | **40** |

min 跨度（= 门槛实际放行的最小值，三份 fixture 一致）：

| 门槛 g | native min 跨度 | czsc min 跨度 | native 保证独立K ≥ | czsc 保证独立K ≥ |
|---:|---:|---:|---:|---:|
| 4 | 4 | 2 | 0 | **−2** |
| 5 | 5 | 3 | 1 | −1 |
| 6 | **6** | **4** | **2** | **0** |
| 7 | 7 | 5 | 3 | 1 |
| 8 | 8 | 6 | 4 | 2 |

### 4.3 三条结论

1. **量纲关系是精确的**：native `min跨度 == g`；czsc `min跨度 == g − 2`。
   ⇒ **czsc `g=6` ≡ native `G=4`**（跨度口径）。`rules.md` §9.8 声称的「对齐」若成立，
   native 应取 **4** 而非 6。
2. **「4–7 区间笔数几乎不敏感（恒 ~50 笔）」是截断伪影**。
   czsc 真值随门槛陡降：326 → 130 → 88 → 56 → 44（fixture1）。
   那个恒定的「~50」正是 `max_bi_num` 的回落默认值
   （`crates/czsc-core/src/analyze/mod.rs:88`，详见 `docs/reference-audit.md` §4.1 的 R68 更正）。
3. **两套参照在缠论判据下不等价**：
   - 老笔（第 62 课：顶底间 ≥1 根独立 K 线）⇒ 需 `独立K ≥ 1` ⇒ native `G≥5`、czsc `g≥7`
   - 新笔（2007-09-18 博文：不含端点 ≥3 根）⇒ 需 `独立K ≥ 3` ⇒ native `G≥7`、czsc `g≥9`
   - **现行 native `G=6` ⇒ 独立K ≥ 2 ⇒ 满足老笔、不满足新笔**
   - **现行 czsc `g=6` ⇒ 独立K ≥ 0 ⇒ 连老笔都不满足**

---

## 5. §11 施工顺序的三个结构性问题

### 5.0 R69 实测：D3 的「缺什么」已经有答案了（2026-10-19）

> 本节是 **§8.2 D3 的判决条件**，先于分歧表更新。结论：`divergence_compare` 之争
> （area vs power_price）在**当前实现下无法被评判**，因为那个谓词在生产上
> **从未触发过**。先修调用错位，再谈口径。

**证据链（三处互相矛盾，实跑复现）**

| 口径 | `divergence_status=detected` | 说明 |
|---|---|---|
| 生产表 `cpt_signal_event`（累计） | 19,256 / 705,456 = **2.7%** | 历史数据由更早代码/口径写入 |
| **改动前**代码，400 只 A 股全笔序列 | **0 / 400** | 400 只逐闸门实跑 |
| **改动后**代码，同一批 400 只 | **9 / 400** | R69 修完调用侧后 |

**逐闸门分解（400 只，`check_first_buy` 的四条前提）**

```
笔数奇数          207 / 400
末笔向下           45 / 400
段内最高=首笔 high  0 / 45   ★ 第一次失败点
段内最低=末笔 low    2 / 400
```

`g3` 命中 0 的原因在 `cpt/application/first_buy_bridge.py` 旧 `_divergence_status`：
它把「首个中枢开始之后的**全部**笔」整段传给判定器。整段里前面必然出现过反弹高点，
`max(high) == bis[0].high` 恒不成立。**这是调用侧错位，不是判定器缺陷。**

**权威判据**：czsc 官方信号模板 `cxt_first_{buy,sell}_V221126` 的判法是**从末尾切片**
（`bis[-n:]`，n ∈ [21,19,17,15,13,11,9,7,5]，见 `tests/test_first_buy.py`（具体行号随 R96 修改变化）、
`:285` 对账用例）。R69 与之一致：新增 `_divergence_segment()` 从末笔向前锚定，
规则为「末笔方向对 + 末笔创全段极值 + 首笔为段内反向极值 + 奇数笔 + 长度 ≤ 21」。

**判定器一行未动**。`emotion_cpt.structure.first_buy::check_first_buy/check_first_sell` 保持
czsc 逐行移植原样，`test_matches_czsc_signal_template` 的 oracle 对照仍成立；
改的只是 CPT 自己该传哪一段。

**对 D3 的影响**：area vs power_price 的争论此前默认「两套口径都能跑，比谁准」。
实测表明**当前口径根本没跑起来**，所以任何关于口径优劣的结论（含审计的「漂移」说）
在此之前都不成立。收敛顺序改为：

1. ~~先定 area 还是 power_price~~ → **先让谓词能触发**（本 R69，已完成）
2. 再用 C2 回测层比两套口径的收益 —— 这一步才轮到 D3 本身

**新增回归测试**（`tests/test_first_buy_bridge.py`）：
`test_divergence_segment_anchors_from_the_tail_not_the_first_center` /
`test_divergence_status_detected_with_history_prefix`（带史前高点的反例）/
`test_divergence_segment_is_empty_when_last_bi_is_not_the_extreme` /
`test_divergence_status_sell_is_the_mirror`。

> **判定顺序纪律**：`_divergence_status` 先查 `length<=0` 再判段落。
> 反过来会让「数据缺」被「凑不出段落」掩盖、谎报成 `not_detected`，
> 把面板的「没算」显示成「算了不背驰」。

### 5.0.1 R70 回测：修好判定器之后，它依然没有择时能力（2026-10-20）

> 本节是 **§5.0 的后续**，也是 **§6 Phase 1 的输入**。R69 让谓词能触发；
> R70 量它触发之后的钱。**结论：口径修好了，但信号不带 alpha。**

**样本与口径**

- 5,131 只 A 股（`daily_bar` ≥ 250 根），窗口 2024-08-10 → 2026-10-19（527 根），
  `NativeChanlunBackend(min_bi_len=6)`、`level=0`、warmup 250 根，逐 K 前视回放，
  共 688,496 次评估。同码冷却 20 根，A/B 配对后冻结 2,704 个样本。
- A = `structure_ready` 首次成立的当日收盘买；B = 同一结构 `confirmed` 当日收盘买。

**结果一：A 组原始收益极高，且不是重叠样本放大出来的**

| h | n | A(就绪日) 均值 | A 中位 | A 胜率 | B(确认日) 均值 | B 胜率 |
|---|---|---|---|---|---|---|
| 1 | 2,703 | +3.44% | +2.29% | 80.0% | +0.04% | 45.0% |
| 5 | 2,692 | +11.53% | +8.54% | 92.5% | +0.03% | 43.2% |
| 10 | 2,677 | +13.33% | +9.20% | 86.0% | −0.03% | 43.6% |
| 20 | 2,641 | +13.37% | +7.80% | 74.3% | +0.21% | 40.3% |

两种去重（同码窗口内不重叠 / 同码冷却 20 根）结果一致；A 与 B 落在同一交易日的样本
只有 **1 / 4,129**，配对没有退化。按 A 信号日当根收益 r0 分桶，剔除「当日已涨 ≥2%」
的样本后 A 仍是 **+14.40%（n=954，胜率 81.4%）**；按自然年拆 2025 +14.03% / 2026 +13.09%。
扣同日全市场等权收益后超额 **+12.59%（t=+27.82，n=2,660）**，截尾 1%/1% 后 +11.81%。

**结果二：这个超额是「已经涨完」的标记，不可交易**

| 事实 | 数值 | 含义 |
|---|---|---|
| ready 日之前 20 日涨幅 pre20 | **+15.87%**（中位 +11.91%） | 信号出现时已经涨过一大段 |
| ready→confirmed 之间涨幅 mid | **+13.56%**（中位 +9.58%） | 与 A 组 20 日收益 +13.37% 几乎相等 |
| ready 日收盘 / 前 20 日最高 | 中位 **0.98** | 就在阶段高点附近，不在底部 |
| gap = conf_t − ready_t | 均值 7.86 根，中位 7，最少 2 | 「确认」永远滞后 |
| A 次日买入 | +9.77%（胜率 65.2%） | 收益的 27% 就在信号当日 |

`pre20 > 0` 不是普涨造成的：同期全样本 20 日涨幅均值只有 +1.89%（中位 **−0.36%**）。
所以 A 组挑中的是**已经在上涨的强势股**，加上确认动作再滞后 7.9 根，20 日 +13% 正是
**把已经发生的上涨记到了信号头上**。

**结果三：纯价格对照证明「一买结构」不是收益来源**

同批票、同窗口、`daily_bar` 原始价、h=20：

| 信号 | n | 均值 | 中位 | 胜率 |
|---|---|---|---|---|
| BASE 任意交易日 | 1,301,564 | +0.479% | −1.117% | 45.4% |
| 跌 5 天 3% 后收阳 | 188,221 | +0.578% | −0.997% | 46.2% |
| 20 日新低后收阳 | 81,111 | +0.808% | −0.357% | 48.3% |
| 破 MA20 收阳 | 268,623 | +0.852% | −0.500% | 47.8% |
| **MOM20（前 20 日涨 >10%）** | 244,096 | **−0.265%** | **−3.054%** | 40.8% |
| **MOM20 + 还在高位（≤0.99）** | 216,532 | **−0.299%** | **−3.068%** | 40.9% |

最后两行是关键：如果 A 组的 13% 来自「已经涨过」，那么**任何普通交易日买进一只已经
涨 10% 的股票**都该拿到类似结果。实测它们拿到的是 **−0.3%**。反过来，真正的低位形态
（新低收阳 / 破 MA20 收阳）只有 +0.8%，与 A 组的 13% 差两个数量级。

⇒ 两套口径都对不上。唯一同时解释三组数字的机制是：**A 组样本是在「同一结构最终会
confirmed」的条件下被选出来的**。要 confirmed，必须在 ready 之后出现一根反向笔；
在一个上涨段里反向笔本来就是短暂回调，于是这些结构的 ready 日，事后看全部落在上涨途中。
这是**用未来信息筛样本**（survivorship look-ahead），不是可执行的信号。

**结论（可直接引用）**

1. `confirmed` 组 5/10/20 日 +0.11/+0.18/+0.02%，胜率 45–46%，**与全体基线无差**
   （R69 前后一致，见 §5.0）。
2. `structure_ready` 组的高收益**来自选择偏差**，不可当作买点运营。
3. 因此 **§6 Phase 1.5（说人话）不能承诺「历史收益」，P2 不成立**；
   产品侧只能说清状态与结构条件，不能给收益预期。

**复现命令**（探针在 `.tmp_audit/`，未提交）：
`probe10_ab.py`（配对与分层）、`probe10b_win.py`（冻结样本）、`probe10c_inspect.py`
（gap 与 K 线形态）、`probe10d_clean.py`（次日买入）、`probe10e_regime.py`（市场同期）、
`probe10f_lag.py`（r0 分桶）、`probe10g_abnormal.py`（同日横截面）、
`probe10h_close.py` / `probe10j_final.py`（纯价格对照）、`probe10i_dup.py`（pre20 分解）。

### 5.1 A1 排在第一位，理由不成立

审计写 A1 是「`first_buy.py` 内部口径」。实际：

- `emotion_cpt.structure.first_buy` 是**从 czsc `crates/czsc-signals/src/utils/cxt.rs` 逐行移植**的
  （`docs/reference-audit.md` §5 明载来源与许可证）。换背驰判据 = 主动放弃该模块的对照基础。
- `divergence_compare` 目前是**死字段**：全仓仅 `emotion_cpt.structure.config:151`（声明）
  与 `tests/test_domain_frozen_contract.py:60`（冻结契约）两处引用，
  `first_buy.py:104-111 _is_divergent` **从不读它** —— 它用 `power_price` / `power_volume` / `length`。
  MACD 只在展示层 `cpt/application/dashboard_indicators.py:12 macd_series` 计算，域内没有。
- ⇒ A1 的第一步不是「加面积分支」，而是**决定这个字段接线还是删除**；且它改的是信号，要升 `SCHEMA_VERSION`。

### 5.2 B1 的风险被低估

审计给 B1「前置条件：无」、风险排第 2。实际它动**分型层**，
级联路径 `分型 → 笔 → 中枢 → 背驰 → 信号 → 存储`，是本清单里级联最广的一项。
`§11.0 原则 5` 自己就写「量纲统一」是前提——而分型层的量纲（`merged_index`）
正是 §4 里出问题的那一层。

### 5.3 C2 被排到最后，但它是唯一能判定 A1/A2 好坏的工具

审计的依赖链写成 `A1 → A2 → B2 → C2`。但回测消费的是**信号**，
现行 `first_buy` 信号**已经存在**，C2 不依赖 A1/A2 即可落地。
把它排到第 6，等于让 A1/A2 的「验收」只能靠数笔数、人工看形态 ——
回到「凭应该对」，与 §11.0 原则 2「验证优先」自相矛盾。

---

## 6. 建议顺序

### Phase 0 —— 口径先冻结（唯一「不先做就返工」的事）

| 项 | 动作 | 涉及 |
|---|---|---|
| P0-a | 用**未截断** czsc 重跑 §4.1 三方对照；`rules.md:174` / `architecture.md:434` 改成可复现值，或标「来源不可复现」 | docs ×3 |
| P0-b | 重写 §9.8 的取值依据：删掉两条不成立的理由，改记 §4 的量纲实测表 + R56 那份 merged 量纲 90 天定档 | `rules.md` §9.8 |
| P0-c | 定 native 走 **老笔 `G=6`**（现状，独立K≥2）还是 **新笔 `G=7`**（独立K≥3，需升 v2 + 重建 fixture） | `SCHEMA_VERSION` |
| P0-d | `emotion_cpt.structure.bi:104` 的 `min_bi_len: int \| None = None` 改为必填或对 `None` 显式告警 | `bi.py` + 调用点 |

> P0-d 是审计 §9「CPT 建议 1」，**至今未做**。现行默认 `None` 会放出
> 326/356/352 笔（§4.2），正是审计 §7.4 用来否决 chanlun-pro 的那种形状。

### Phase 1 —— 先能测：C2 回测层（提前）

接**现有** `first_buy` 信号，不依赖 A1/A2。产出后可量化判定后续每一项改动。

> **2026-10-19 前置已就绪**：R69 修复前，`divergence_status` 在 A 股生产路径上
> **从不触发**（400 只实测 0 命中），此时建回测层，量的是「一个永不成立的谓词」。
> R69 修好调用侧后（9/400），回测层第一次有了可比的输入。
> 但结论仍是：**回测层必须先于任何口径之争**（含 D3）。
>
> **2026-10-20 R70 回测已给出第一批答案**：修好后的 `confirmed` 组 5/10/20 日
> 收益 +0.111/+0.181/+0.021%，胜率 45–46%，与全体基线一致；`structure_ready`
> 组的高收益经查是**未来信息筛样本**造成的假象（详见 §5.0.1）。⇒ 回测层建立的
> 第一件事不是「证明策略好」，而是**证明它不好**，从而把 Phase 2/3 的方向从
> 「磨判定」改成「换判据或换标的」。这一步必须先于任何参数调优。

### Phase 1.5 —— 「说人话」：把判定翻译成用户能拍板的句子（2026-10-19 新增）

> **目标（owner 原话）**：效果更好、更准、给出更「说人话」的建议、使用者更方便做决策。
> 前两句是准确率问题（Phase 0–3），后两句是**翻译层**问题，与本文件的信号口径
> **正交**，但同样是「效果」的一部分。现状缺口：

| 现状（R71 前） | 用户拿到的是什么 | 缺什么 |
|---|---|---|
| `/api/dashboard/signal-stats` | 状态跃迁的 status / divergence **分布**（是「事件计数」，自报 `basis=signal_event_transitions`） | ~~前视收益~~ → **已由 R70 证伪，不能用**（见下） |
| `divergence_status` 三态 | `detected` / `not_detected` / `not_checked` | 人话：「底背驰成立，力度比上一段弱 38%」 |
| 结构三条件 | `has_two_centers` / `has_divergence_leg` / `has_reversal_bi` | 「两个中枢已成、离开段已走完、反向笔已出现 → 可以准备买」 |
| 按状态 | `structure_ready` / `invalidated` | 「还差一步」/「已失效，别追」，并**给失效理由** |

**纪律**：翻译层只做**有据可查的复述**，不引入新判定。每一句人话必须能指回
一个域内字段，否则就是编造 —— 与全仓「结论必引出处」一致。

> **2026-10-20 R70 追加约束**：§5.0.1 已证明一买信号**无前视收益**，所以「前视收益
> 那一列」不能作为卖点，也不能给收益预期。翻译层允许说的只有三样：
> (a) 现在处在哪一步（结构条件达没达，指回 `has_two_centers` / `has_divergence_leg` /
> `has_reversal_bi`）；(b) 背驰判了没有、结果是什么（指回 `divergence_status` 三态）；
> (c) 结构失效时给失效理由（指回 `assess_first_buy` 的分支）。
> 禁止出现「历史上 20 日涨 X%」「可以准备买」这类带方向承诺的句子。

#### Phase 1.5 已落地（R71，2026-10-21）

owner 拍板（m01525）：**接受一买无 alpha，换用途为「风险/位置提示」，不做买点。**
m01562 补充：横幅要**有数字、也要有建议**（自用项目，不写「仅供参考」的废话）。

| 落点 | 内容 | 位置 |
|---|---|---|
| 实测横幅 | 5,131 只 / 688,496 次回放的 5/10/20 日收益与胜率 + 一句话「它不预测涨跌」+ 一句「怎么用」 | `dashboard/index.html` `[data-testid=signal-evidence]` |
| 状态词改名 | `structure_ready` → 「位置已到（不是买点）」；`confirmed` → 「反抽确认了一笔（不是买点）」；面板标题 → 「下跌位置提示（原一买）」 | `dashboard/dash-core.js`（另 5 个 dash-*.js 为同步死副本） |
| 三步进度 | ① 两个中枢 ② 背驰段 ③ 反向笔，各带 `done/failed/pending`；只报**首个**失败，其后一律 `pending` | `cpt/application/signal_progress.py` |
| 进度接线 | 生产路径算出并挂 `snapshot.summary.signal_progress`，前端 `[data-testid=signal-progress]` 渲染；无数据整块 `hidden` | `a_share_snapshot.py`、`dashboard_snapshot_v2.py`、`dashboard/dash-signal.js`、`dashboard/dash-structure.js` |
| 失效归因 | 失效时给**具体原因码**（不是「以前失效过」）：`not_in_downtrend` / `not_enough_centers` / `no_divergence_leg` / `previous_invalidated`；历史行补原因，同原因不刷屏 | `cpt/application/signal_invalidation.py` |
| 失效呈现 | 信号历史行下加 `[data-testid=signal-history-reason]`；统计面板加失效原因分布，`unknown` 单列「原因未记录（R71 之前的行）」 | `dashboard/dash-signal.js`、`cpt/application/dashboard_stats.py` |
| 全景视图（R73） | `/api/dashboard/signal-overview`：每只票取**最新一条**跃迁，出「覆盖票数 / 站在位置上 / 已作废 / 结构不足 / 距最后跃迁 ≥ N 天」+ 站住的票明细；正文只列 `structure_ready`/`confirmed`，其余走汇总 | `cpt/application/signal_overview.py`、`cpt/web/app.py`、`dashboard/dash-signal.js` |
| 步骤分布（R78） | 每行加 `step_index`（0~3 或 **None**）+ `blocked_at`；全景出「走完 0/1/2/3 步各多少只」与「卡住的票卡在哪一步、因为什么」两块；`unknown` 桶单独成句 | `cpt/application/signal_overview.py`、`dashboard/dash-signal.js` |

**设计约束（必须保留）**：

- 进度走 `summary.signal_progress` **独立键**，不进 `Signal` dataclass —— Signal 是受契约
  钉住的领域对象，加字段会动 `config_hash` 与信号历史行。
- 翻译层**不读行情、不算收益、不给目标价**（`signal_progress.py` 模块 docstring 已锁）。
- 措辞红线：`action` 只允许「别追空 / 别割肉 / 别加仓」，不许出现买入指令
  （`tests/test_signal_progress.py::test_action_never_gives_a_buy_instruction` 钉住）。
- 生产实跑验证（2026-10-21）：000006 走到「卡在第二步」、000014「卡在第一步」、
  000001/600519「向上走势不评估」—— 四个分支都出了真实输出。
- **失效归因的判定顺序不能照抄状态机**：`classify_invalidation` 里 `previous_status
  == "invalidated"` 必须排在**结构原因之后**。照抄状态机（它关心「结果是什么」）会让
  所有失效票都退化成 `previous_invalidated` =「以前失效过」，等于没回答「这次为什么
  不成立」。实测 000006 / 000014 第一版就是这样被打错的，`tests/test_signal_invalidation.py::
  test_structural_reason_beats_previous_invalidated` 钉住这个顺序。
- **「没记录」不许猜成「中枢不足」**：统计层把无原因的历史行单独计入 `unknown`，
  归因层在 `facts is None` 时返回 `None` 而不是默认码。`evidence` 里 facts 缺失填
  `None` 不填 `False` —— 「没数据」和「算出来是否」是两个事实。
- **`age_days` 是下界，不是「卡了几天」（R73）**：`cpt_signal_event` 是状态跃迁表，
  状态不变就不写行，所以「距最后一次跃迁 1 天」的票可能已经卡了 180 天。响应里
  `age_is_lower_bound` 恒 `True`，前端文案写「距最后跃迁」，不写「卡了 N 天」
  （`tests/test_signal_overview_wiring.py::test_frontend_never_claims_stuck_days` 钉住）。
  要回答真实停滞时长需要每轮落一份步骤快照（方案 A2，需新建表，本轮未做）。
- **全景与统计是两个统计单元，不许混讲（R73）**：`signal_statistics` 数**事件条数**
  （同票跃迁 5 次算 5 条），`signal_overview` 数**票**（每票只取最新一条）。二者数值
  必然不同，故各开一条路由、`basis` 各自自报，前端标签分开写。
- **兜底 `structure_id` 是盲区，必须单列**：`level5:empty:<code>` 表示结构不足两个
  中枢，连第一步都没走到。它 `status` 恒为 `invalidated`，混进「已作废」会虚高该数
  （实测 202 只里有 21 只）。`blind_spots` 单独计。
- **上界推断不许当分布读（R78）**：失效行没记原因时，`step_index` 返回 `None`，不是
  「保守取 2」。第一版取 2 的语义是「最多走到第二步」—— 这是上界；但分布一旦把上界
  当实际值加总，生产实测 117 只无原因作废票全挤进第 2 步，与 55 只真 `structure_ready`
  混成一桶，页面读成「173 只走到第二步、市场普遍就差临门一脚」，而真相是 117 只**根本
  不知道走到哪**。教训：凡是「不确定」的量，宁可单列一个 `unknown` 桶，也不要塞进
  最接近的已知桶 —— 前者读者看得见不确定，后者会被读成事实。
  `tests/test_signal_overview.py::test_missing_reason_yields_none_step_index_not_a_guess`
  钉住。

#### R84（2026-10-21）：`Restart=on-failure` 不覆盖 SIGTERM

R83 让巡检能在 2 小时内知道「服务停了」。但**通知不等于自愈** ——
凌晨 3 点收到飞书，还是得有人手动 `systemctl start`。所以补上真正的那一环。

### 洞在哪

`cpt-dashboard.service` 原本是 `Restart=on-failure`。这个名字听起来够用，
实际上 **on-failure 不覆盖 SIGTERM 正常退出** —— 而系统里所有正常的
shutdown / kill / 断电前的停止**都是 SIGTERM**。2026-10-19 03:05 那 55 小时
停机就是这个形状：进程「正常退出」了，systemd 认为这是预期行为，不拉起。

### 为什么是 always，而不是补一个 `RestartForceExitStatus=SIGTERM`

后者表达的是「**只把** SIGTERM 当失败」，仍是白名单思路 —— 下次被别的方式
停掉（`exit 0`、SIGINT）又会静默死。CPT 是自用常驻服务，**没有一种「正常停
一会儿」的场景**：要停就是维护，维护该 `stop` 而不是 `kill`。`always` 与这个
事实一致，白名单与它不一致。

代价明说：`kill <pid>` 现在会被 10 秒后拉起。**真停服务必须
`systemctl stop cpt-dashboard`**（stop 的作业不触发 Restart）。
`RestartSec=10s` 保证即使有外部脚本反复 kill 也不会打满启动风暴（默认 100ms 会）。

### 实测（两个方向都要验，只验一个等于没验）

| 动作 | 结果 | 说明 |
|---|---|---|
| `kill -TERM 667030` | `MainPID 667030 → 667417`，`NRestarts 0 → 1`，仍 `active` | **洞堵上了** |
| `systemctl stop` 后等 15s | `inactive`，`NRestarts` 不变 | 不空转，stop 仍然是真的停 |
| 再 `systemctl start` | `active`，`health=200` | 恢复正常 |

### 回归钉子

新建 `tests/test_systemd_unit_policy.py`（**6 passed**），最核心的一条：

```python
assert _directives().get("Restart") == "always"
```

反向注入实测：把 `Restart=always` 改回 `on-failure` → 该条**红**；还原后绿。
另外五条钉住 `RestartSec` 有限（防启动风暴）、以及 R58 的沙箱加固
（`ProtectSystem=strict` / `ProtectHome=read-only` / `ReadWritePaths` 不含
`/home/ubuntu`）没有在改 restart 时被顺手删掉。

### 这条为什么值得写进文档

这类 bug 的特征是**配置看起来是对的**。「有 Restart 啊」—— 光读文件确认不了，
必须用一条能失败的断言钉住。R83 的巡检只能缩短**发现**时间，
R84 才是消除**故障本身**。

#### R83（2026-10-21）：观察期不是状态，是「要有东西在看」

R82 之后 CPT 被安排进观察期。但**观察期本身有前提**：得先有人能告诉你「它坏了」。
`run_inspection.py` 只回答「K 线/因子/结构算得对不对」，它对下面四样**全都不看**，
而这四样恰好是 CPT 历史上真实出事的地方：

| 出事过什么 | run_inspection.py | 现在 |
|---|---|---|
| `cpt-dashboard.service` 停 55 小时无人知（`Restart=on-failure` 不覆盖 SIGTERM） | ✗ | ✅ R83 |
| bigserial 序列落后四次，只留一行 warning 没人看 | ✗ | ✅ R83 |
| `watchlist-daily-summary` 连两天 `ModuleNotFoundError`（editable 指向已删目录） | ✗ | ✅ R83（批次行龄） |

`scripts/watch_period_check.py` + `deploy/cron/watch-period-check.sh`（**每 2 小时**），
四道判据：**服务**（unit active **且** health `ok:true`，两个都要）、**序列**
（`last_value >= max(id)`，**超前不是病**，nextval 跳号是正常的）、**批次**
（`cpt_run_metric` 最近一轮行龄，工作日 30h / 周末 78h）、**盘口**
（新浪五档拿得到 ≥1 档，ADR 0003 整条路线吊在这上面）。

告警策略复刻 `run_inspection.py`：**只在状态变了时发**，首次建基线不发、
但首次就有问题要发、恢复也发。

### 四条踩出来的约束

1. **别用 `ss` 判端口** —— 容器里 `ss -ltn` 输出**全空**（无权限），会把人骗成
   「服务没监听」。判端口一律走 `curl`。R83 实测：`ss` 空、`curl` 200。
2. **`cpt_run_metric.market` 的 A 股取值是 `'cn'`，不是 `'ashare'`**。第一版写
   `'ashare'`，一行都查不到 ⇒ 脚本报「批次从没跑成」—— **一个错的口径造出一条
   假停产告警**。这条现在有测试读真库钉住。
3. **日历是判据的一部分**：只用一个 30h 阈值会在**每个周末**误报两次；
   只用一个 78h 会在工作日停摆两天才响。
4. **盘口探活必须异常隔离**：它要出网、最不可靠。崩了整个巡检就收不到消息，
   而「新浪抖了一下」与「服务挂了」在告警上长得一模一样。

### 验收

- `tests/test_watch_period_check.py` 21 passed。
- **五次反向注入全部验红**：批次阈值改 0.1h / 序列判据反写 / 盘口探针指向
  不存在的票 / unit 名不存在 / health 指向死端口。
- 生产实跑：**全绿**（服务 active + `ok:true`、序列 910682/910570 与 898/892、
  批次 0.7h 前、盘口 600519 10/10 档）；`--force` 实测飞书**送达**。
- 线上 crontab 已装 `0 */2 * * *`，与仓内模板 **9 条作业逐条一致**。

#### R82（2026-10-21）：补齐新浪批量 + 五档，把「谁采盘中」变成口径题

emotion-core 的 ADR 0003（盘中触发）在风险栏里点名了「与 CPT 冲突，本仓再做
一套采集器存在重复建设风险」。这轮把它答掉了：**同一份响应里五档一直都在**，
只是 `SinaQuoteClient` 只解析了前 9 个字段。

| ADR 0003 要做的事 | CPT 现状 |
|---|---|
| 主板前缀 / ST 剔除 / 北交所过滤 | ✅ `CONFIG.BOARD_PREFIXES` |
| 涨停价算法（禁止 `*1.1`） | ✅ `limit_up_price_cents` |
| 昨日板数 `cont_days` | ✅ `derived_bar.cont_days` |
| 收盘判「封住 / 炸板」 | ✅ `derived_bar.is_limit_up` / `is_bomb` |
| **采五档** | ❌ **R82 补上**：`fetch_quotes(codes)` 批量 + 十档 |

⇒ 分工：**原始采集归 CPT**（口径全在这边），**触发判据与产品面归 emotion-core**。
ADR 0003 已同步改写（见 emotion-core 仓）。

**本轮只做零副作用部分**：解析 + 单测，不建表、不起进程、不动 `fetch_quote`。
ADR 0003 的 D10 重启条件第一条「CPT 自身稳定」尚未满足（R69→R81 刚修完
一轮口径与数据），所以落库与常驻进程仍押后。

### 三条踩出来的约束

1. **`fetch_quote` 的返回字典一个字都不能改** —— `a_share_snapshot._attach_dual_compare`
   在读它。批量走另一个方法 `fetch_quotes`。
2. **键必须是 6 位纯代码**，不是 `sh600519`。第一版给的是前缀键，实跑 120 只时
   `missing` 列出全部 120 —— 调用方手上是 `daily_bar.code`。
3. **缺档位给 `None` 不给 `0`**。停牌时新浪把价和量一起填 0；把「没有这一档」
   记成「挂着 0 手」方向正好相反。但**量本身是 0 要保留 0.0**（集合竞价时
   买一量可以是货真价实的 0）。

### 真实形态（2026-10-21 实测，120 只）

- 停牌 `000016 *ST康佳A`：`last=0`、十档全空 ⇒ 全部 `None`。
- 涨停 `000518 四环生物`：现价 3.63、**买五档齐全、卖五档全空** ⇒ 这正是 ADR 要的「封死」形态。
- 五档齐全 118/120；全量无「买一价 > 卖一价」、无价格离谱值。
- 120 只走 **3 次 HTTP**（50/50/20）。

#### R81（2026-10-21）：序列反复落后的真源头不在生产代码，在测试

R72 加了运行时自愈后，序列**第四次**落后（`last_value=900007 < max(id)=910525`）。
这轮把源头查清了，同时**推翻了我自己 R72 时段的一个判断**。

| 假设 | 结论 |
|---|---|
| 生产代码在带 id 写 | ❌ 两个 store 的 `INSERT` 列清单里**都没有 `id`**（R81 复核） |
| 迁移脚本的 `INSERT ... SELECT id` | ❌ 一次性迁移，不会每轮跑 |
| **测试夹具的 `setval`** | ✅ 真凶 |

`tests/test_migration_r67_symbol_in_id.py::_install_fake_pre_migration` 为了给
合成行（id 900001~900007）让路，在事务里 `setval` 到 900009，注释写「随回滚消失」。
**那条注释是错的** —— PostgreSQL 的 `setval` 不受 `ROLLBACK` 撤销。实测：

```
序列 = 910570 → setval(900009) → rollback() → 序列 = 900009
```

⇒ 每跑一次该测试文件，序列就被永久拉回 900009，而真实行在 91 万级，
下一次跑批必撞。**这就是「修了又坏」的全部原因。**

**修法**：合成行放到 `max(max(id), last_value)` **之后**，完全不动序列；
起点仍低于序列时 `pytest.skip` 并给原因，不硬插。

**新门禁⑯ R5**：`tests/` 里不许出现 `setval` 调用。判据踩了两个坑才定下来：

1. 只查行文本 → 被**自己的 docstring** 骗（里面写着「原实现有一行 `setval(...)`」）；
2. 只查 AST 调用点 → 被**真实形态**骗（`cur.execute("SELECT setval(...)")`，
   `setval` 在字符串里，AST 没有这个调用节点），反向注入实测**静默放行**。

最终：**字符串字面量 + AST 调用点两样都查**。

**教训**：测试的 `rollback` 不能证明它没留下副作用。`setval` / `nextval` /
会话级 `pg_advisory_lock` 都不在事务保护范围内 —— 污染只体现在一个不被事务
保护的全局对象上，而爆炸点在离真凶几百行之外的生产跑批里。

#### R80（2026-10-21）：新加的关注要多久才出现在全景里

用户问：「如果我有新的关注，会自动跑数据吗？」答案是**会，但要等一天** —— 这不够。

**改前的三条路径**（实测）：

| 路径 | 频率 | 结果落库？ |
|---|---|---|
| `cpt-dashboard-ashare.timer` | 每天 08:00 UTC（北京 16:00） | ✅ |
| 点开看板某只票 | 打开后 60 秒内 | ❌ `GET` 只读（R59 审计 M1） |
| （无） | — | — |

⇒ 新加一只关注，要等**第二天**那次 timer 才进 `cpt_signal_event`；点开看板虽然会算一次，
但结果不落库，全景里仍然看不见它。用户 m04507 选了方案 B：单独起一条只跑关注列表的高频作业。

**为什么不直接把主批次调频**：主批次含热池 97 只，× 每 15 分钟 = 一天近万次缠论计算
（单只 0.5~2.6 秒）。关注列表通常个位数，一次一两秒。**分开**才对。

**落地**：

| 层 | 改动 |
|---|---|
| 脚本 | `scripts/snapshot_a_share_batch.py` 加 `--only watchlist`（不查热池），跑完原子写 `~/.cache/cpt/watchlist_batch_status.json` |
| 调度 | `deploy/cron/watchlist-snapshot.sh`（新建）+ crontab `*/15 * * * *`；`flock` 防重入、`timeout 10m` |
| 后端 | `signal_overview.py::watchlist_status()` → `overview.watchlist = {watched, counts{total/never_run/stale/fresh}, items[], refresh_hours}` |
| 前端 | `dash-signal.js::buildWatchlistAlert()` → 「我关注的票」三行 + 缺数据时给日志路径 |

**三条设计约束**：

1. **「没跑过」和「跑过但旧」必须分开说**。合成一句「没数据」会让用户不知道该等还是该跑
   （等的意思是自动任务会来，跑的意思是得去查日志）。`never_run` 与 `stale` 是两个桶，
   前端也分两行显示。
2. **界面的判据是事件表，不是状态文件**。`watchlist_batch_status.json` 只是调度侧的自述，
   给排障看；`watchlist_status()` 只读 `cpt_signal_event` 里有没有这只票的行。文件丢了或写坏了，
   界面也不会骗人 —— 顶多是把「刚跑过」显示成「还没跑过」，方向是**保守**的。
3. **没关注任何票 ≠ 关注了 0 只全都缺数据**。`watched=False` 时整块 `hidden`，
   不能给一个空列表渲染出「0 只 / 全都缺数据」的红字。

**对外只说「几小时内」不说「每 15 分钟」**：`WATCHLIST_REFRESH_HOURS = 4`。因为一只票跑过
但状态没变就不写行，`age_days` 动得比 cron 慢得多 —— 承诺 15 分钟会变成每天都在问「怎么没更新」。

#### R79（2026-10-21）：一卖的失效归因一直缺着，补数据补不出来

「补数据能降低 unknown 比例」这个假设被实测推翻，换来的是一处代码缺口的定位。

- **假设**：117 只「不知道走到哪」是历史行没记原因，重跑一遍即可补上。
- **实测**：重跑热池 98 只（`ok=98 skip=0 err=0`，126 秒），`unknown` 只从 117 降到 110。
- **拆因**（非盲区失效行，按 `signal_type`）：

  | 类型 | 失效行 | 带原因 |
  |---|---|---|
  | `first_buy` | 99 | 36 |
  | `first_sell` | 85 | **0** |

- **根因**：`invalidation_payload` 只在 `_derive_first_buy_signal` 里被调用；`_derive_first_sell_signal` 一直是裸 `record_signal_event(conn, signal, prev_status, code, event_time)`，**从来没传 `invalidated_reason`**。所以一卖的失效行不可能有原因 —— 这不是数据缺失，是代码缺口，补数补不出来。
- **修法**：`classify_invalidation` / `explain_invalidation` / `invalidation_payload` 加 `kind` 参数（默认 `first_buy`，既有调用点零改动）；`_WANTED_DIRECTION = {"first_buy": -1, "first_sell": 1}` 让方向判据镜像；文案走新表 `REASON_TEXT_SELL`。
- **改完实测**（热池重跑后）：`first_sell` 带原因 0 → 11，`unknown` 110 → 99。

**两条教训**：

- **「补数据」之前先证明缺的是数据**。这里的数据写入逻辑本身就是破的，重跑只会把同样的空缺再写一遍。
- **一卖不是一买的复制品**。原因**码**可以复用（码是稳定标识，前端按码查标签），但**文案**必须换一套：直接把 `REASON_TEXT` 套到一卖上会输出「一卖只对下跌走势有意义」。`_WANTED_DIRECTION` 换成 `!= -1` 的写死判断同样会把**正确的上涨走势**判成方向不对 —— 比没有原因更糟。`tests/test_signal_invalidation.py::test_first_sell_text_never_says_downtrend_or_first_buy` 钉住。

#### Phase 1.5 续（R76，2026-10-21）：买点语气从「后端所有出口」删净

R71 只改了 `dash-core.js` 里的 `STATUS_LABELS`，页面确实显示了「位置已到（不是买点）」。
**R76 盘点发现真正会被看到的地方根本没改到**，因为它们读的不是那张表：

| 出口 | 真正读的地方 | R76 之前用户看到的 |
|---|---|---|
| 看板推荐卡（26px 加粗大字） | `cpt/application/recommendation.py` 的 `ACTIONS` | 「买入结构」「卖出结构」 |
| 追踪页徽章 + 历史表 | 同一份 `ACTIONS`（经 `track_api`） | 「买入结构」、表头 `BUY` / `SELL` |
| 飞书跃迁推送（第一行） | `cpt/application/signal_notify.py::_points_lines` | 「动作: sell（structure_ready）」 |
| 飞书收盘汇总 | `signal_notify.build_watchlist_summary` | 「000002 观望 · 买点 3.56 · 卖点 3.92」 |
| 信号雷达 | `cpt/web/app.py` 的 `source_label` | 「一卖」 |
| LLM 旁白 | `cpt/llm/prompts.py` 的禁用词表 | 只禁「建议」，没禁「买入」 |
| 追踪页 `explain_algorithm` | `cpt/web/track_api.py` | `"status": "structure_ready"`（英文枚举） |

改动后的对外文案：

| 码值（**未改**） | 人话 |
|---|---|
| `buy` | 下跌位置已到 |
| `sell` | 上涨位置已到 |
| `watch` | 位置已到，还没确认 |
| `hold` | 没有位置提示 |
| `confirmed` 的 headline | 「跌势里已经冒出一笔反弹（不是买点 —— R70 实测这类信号之后 20 日平均 +0.02%、胜率 46%，等于瞎猜）」 |
| 参考位 | 「结构区间下沿 / 上沿」（原「参考买点 / 卖点」） |
| 信号类型 | 「下跌位置提示 / 上涨位置提示」（删掉「原一买」后缀） |

**两条容易踩的决策**：

- **码值一个都不许改**。`action`（`buy`/`sell`/`watch`/`hold`）进了
  `cpt_track_snapshot` 的 payload、`/api/trade/*` 的决策批次、
  `a_share_routes._RECOMMENDATION_KEYS`，前端着色读 `data-action`。
  只换 `action_label` / `headline` / `reason` / 推送正文。
  `tests/test_no_buy_language_wiring.py::test_action_codes_are_unchanged` 钉死。
  同理 `cpt/web/trade_api.py` 的 `("BUY", "OPEN_POS")` 是**交易机契约**，留英文是对的。
- **「不是买点」这种否定式必须豁免**。守卫测试先摘掉否定式再查违规词，
  否则「（不是买点）」自己会被判成违规。但「不推荐买入」**不**豁免 ——
  它仍然把「买入」当成了一个选项。

**覆盖的守卫**（`tests/test_recommendation.py` 7 条 + `tests/test_no_buy_language_wiring.py` 17 条）：

- `test_no_buy_language_in_recommendation`：10 组「状态 × 方向 × 中枢数 × 背驰态」，
  用户可见文本（`action_label`/`headline`/`reason`/`disclaimer`）里一个买点词都不许有。
- `test_no_buy_language_in_degraded_paths`：降级路径（没数据 / 没信号 / 未知状态）同样查。
- `test_no_buy_language_in_dashboard_sources`：7 个 `dash-*.js` 剥掉注释后逐个查。
- `test_dashboard_html_has_no_buy_language`：`index.html` 查（术语表条目本身豁免）。
- `test_track_page_status_labels_match_backend`：`dash-track.js` 的副本必须与
  后端 `STATUS_TEXT` 逐条一致（该文件刻意不依赖 `window.CPTDashboard`，所以只能是副本）。
- `test_footer_is_fact_not_disclaimer_boilerplate`：页脚不许出现「不构成投资建议 /
  投资须谨慎」（owner m01562：「自用项目，不用说仅供参考的废话」），必须讲清「不预测涨跌」。
- `test_llm_prompt_forbids_imperative_verbs`：旁白提示词必须显式列「买入 / 卖出 / 加仓」。

**生产实跑**（2026-10-21，四个分支都有真实输出）：

| 票 | 状态 | 输出 |
|---|---|---|
| 001216 | `structure_ready` | 位置已到，还没确认 /「两段下跌的力度没有明显衰减 —— 更像下跌中继里的停顿」 |
| 300115 | `structure_ready` + 背驰 | 位置已到，还没确认 /「两段下跌的力度确实在衰减（背驰）」 |
| 603236 | `confirmed` | 下跌位置已到 /「不是买点 —— R70 实测…+0.02%、胜率 46%」 |
| 000014 | 结构不足 | 结构条件不成立 /「没形成两段重合的价格区间…这里是真的没有」 |

推送正文实跑：`结构位置: 下跌位置已到 · 还没确认` / `结构区间下沿: 248.66` /
`结构区间上沿: 291.94` / `止损参考: 232.00` / `现价: 264.66`。

### Phase 2 —— 低风险增量

| 序 | 项 | 理由 |
|---|---|---|
| 1 | A3 笔自纠错**对照实验**（独立脚本） | 不改产品代码，先看差异 |
| 2 | B1 缺口进分型 | 有了 Phase 1 的回测才能量它的级联影响 |

### Phase 3 —— 信号扩展

A1（面积背驰，先决定字段接线/删除）→ A2（二买/三买，升 `EXPORT_SCHEMA_VERSION`）。

### Phase 4 —— 性能

B2 增量 API。`replay.py:328 replay_incremental` 现为「每个前缀全跑一次」，非真增量。

### 不接

B3 多中枢（审计已拍板）。

---

## 7. 复现命令

```bash
cd /home/ubuntu/DSH/Chan_Pattern_Trader

# §4 门槛量纲对照（本文核心证据）
.venv/bin/python scripts/audit_gate_dimension.py

# 缺口自检：确认脚本对着本仓代码而不是另一个 checkout 运算
.venv/bin/python -c "import cpt,sys;print(cpt.__file__)"
```

---

## 8. 分歧记录（未收敛）

> 本表只记录**分歧本身**：双方主张、各自依据、分歧的性质、以及「缺什么才能收敛」。
> 不代替拍板，也不把没结论的事写成有结论。

### 8.1 已收敛：双方一致，只是没人做

| # | 事项 | 一致的表述 | 出处 |
|---|---|---|---|
| C1 | `build_bis(min_bi_len=None)` 是危险默认 | 改必填，或对 `None` 显式告警 | 审计 §9·CPT-1 ＝ 本文 §6 P0-d |
| C2 | `reference-audit.md` §4.1 的 chanlun-pro 对照列不可复现 | 重测，或标注「来源不可复现」 | 审计 §9·CPT-2 ＝ 本文 §3 |
| C3 | B3 多中枢暂不接 | 缠论无明确的多中枢分类定义 | 审计 §11.6 ＝ 本文 §6 |
| C4 | 不搬复刻版代码 | licence 链断裂（审计 §3） | 审计 §11.0-1 ＝ 本文 §1 |
| C5 | 门槛量纲是「去包含后 K 线根数」 | 不是原始 bar 根数 | 审计 §11.0-5（R66 更正 `config.py:146`）＝ 本文 §4 |

> C1/C2 审计在 §9 里提过，只是没排进 §11.8 的顺序表 —— 所以「未做」不等于「有分歧」。

### 8.2 未收敛：分歧点

#### D1 门槛数值该取几（事实已定，口径未定）

| | 主张 | 依据 |
|---|---|---|
| 审计 §11.0-5 / §9·CPT-4 | 只规定**量纲**（`merged_index`），对数值未表态；并判断「分歧在**端点认定规则**而非门槛数值」 | §9·CPT-4 的三方对比（czsc 44 / CPT@6 71 / 复刻版 47，中位跨度同为 10 根 raw） |
| 本文 §4 | 两侧量纲差恒为 **2**（native `min跨度 == g`，czsc `== g−2`），故 `rules.md` §9.8 的「与 czsc 对齐」不成立；现行 native `G=6` 满足老笔、不满足新笔 | 三份 oracle fixture 实跑（`scripts/audit_gate_dimension.py`） |

**分歧性质：不是事实之争，是口径选择。** 事实侧两边可合流（本文 §4 与审计 §9·CPT-4 都指向「门槛数值不是主因」）。
真正缺依据的是「CPT 该按老笔还是新笔定档」：

- 老笔（第 62 课）：顶底之间 ≥1 根独立 K 线 ⇒ native `G≥5`
- 新笔（2007-09-18 博文，**不在 108 课内**）：不含端点 ≥3 根 ⇒ native `G≥7`
- `docs/rules.md:7-8` 自称「以缠论原文作为理论来源」，但仓库内**没有原文副本**；
  两份外包研究的结论也只到「老笔=5 根 / 新笔=6 根 类换算属社区推断」为止。

**缺什么才能收敛**：权威横向评测，或原文对该情形的明文 —— 当前都没有。
⇒ 这一项**可能长期无解**。可行的落法不是等结论，而是「可配置 + 把取哪档显式记进 `rules.md`」，
让它成为**有记录的选择**，而不是沉默的默认值。

#### D2 施工顺序：C2 到底依不依赖 A1/A2

| | 主张 | 依据 |
|---|---|---|
| 审计 §11.8 | `A1→B1→A3→A2→B2→C2`，C2 前置写「**A2 + B2 落地**」 | 「按风险从低到高，每阶段验收通过才进下一阶段」 |
| 本文 §6 | `Phase 0 口径 → C2 → A3 → B1 → A1/A2 → B2`，C2 提到第 2 | 回测消费的是**信号**，现行 `first_buy` 信号已存在；C2 排第 6 会让 A1/A2 的验收只能靠数笔数、人工看形态，与审计 §11.0-2「验证优先」自相矛盾 |

**分歧性质：依赖判断之争，可判定。** 根子只有一句：C2 的验收是否要求**新信号类别**。
审计 §11.7 自己写的验收是「BTCUSDT 5m × 90 天回测，数字可复现；fee 开关对比」——**没提新信号**。
若确认不要求，C2 可提前，这一项即收敛。

#### D3 `divergence_compare = "area"`：漂移，还是死字段

| | 主张 | 依据 |
|---|---|---|
| 审计 §11.1 | 是「**文档漂移**」；落地 = 给 `first_buy.py` 加面积分支使其名副其实 | `_is_divergent` 实际用 `power_price` |
| 本文 §5.1 | 是**死字段**；第一步该定「接线还是删」 | 全仓仅 `emotion_cpt.structure.config:151`（声明）+ `tests/test_domain_frozen_contract.py:60`（冻结契约）两处引用，`first_buy.py:104-111 _is_divergent` **从不读它** |

**分歧性质：定性之争 —— 且查完发现是三方互相矛盾。**

- `docs/rules.md:574` 明写：“（`power_price` / `power_volume` / `length`）正是一买/一卖**背驰比较的输入**”
- `docs/rules.md` §9.6 只把 `divergence_status` 定为 `{not_checked, not_detected, detected}`，**没有承诺过 area 口径**
- 而 `emotion_cpt.structure.config:151` 写死 `divergence_compare: str = "area"`

⇒ **`rules.md` 站 `power_price` 一边，`config.py` 站 `area` 一边，`_is_divergent` 跟 `rules.md` 走。**
矛盾在「config 声明」与「rules 承诺」之间，**不在实现里**。审计说的「漂移」与本文说的「死字段」，
是同一件事的两面。

**缺什么才能收敛**：明确该字段是**契约**（要保住 area 的承诺 ⇒ 接线 + 升版本）还是**残留**（⇒ 删）。

> **2026-10-19 R69 更新：本条的「缺什么」已部分作答，见 §5.0。**
> 实测表明当前 `power_price` 口径因**调用侧段落错位**在生产上 0/400 命中，
> 即「两套口径比谁准」的前提根本不成立。R69 已修好调用侧（0 → 9/400），
> 但仍**不判定** area / power_price 谁对 —— 那要靠 §6 Phase 1 的 C2 回测层出数字。
> 本项从「定性之争」降级为「**等回测数据**」，不再挡其它项。

#### D4 端点认定规则：两侧都不满足同一不变式

| | 主张 | 依据 |
|---|---|---|
| 审计 §9·CPT-4 | 分歧在**端点认定规则**，值得单独立项 | 笔数差 1.6 倍而中位跨度几乎相同 |
| 本文 §4 ＋ `docs/audit-p2-12-endpoint-extreme.md` | 门槛量纲差 2 是事实；而「笔端点须为区间内同类极值」这一不变式 native 违反 53/57/59，**czsc 侧同样不满足**（放开截断后起点非同类极值 0/1/2 例） | 三份 fixture 实跑 + DP 上界 |

**分歧性质：不冲突，但都不完整。** 审计指向端点规则、本文证了两侧端点都不满足同一不变式
⇒ 真问题变成「哪一侧的端点规则更贴缠论」，而这**没有判据**。
**缺什么才能收敛**：权威判据，或回测收益 —— 后者又指回 D2。⇒ **D4 是 D2 的下游。**

#### D5 `rules.md` §9.8 的两条理由：这是更正，不是分歧

§9.8 原文的取值依据是两条：「与 czsc `check_bi` 的门槛对齐」+「实测在 4–7 区间笔数几乎不敏感
（3 个 oracle fixture 恒 ~50 笔），8 起急降，故直接采用 czsc 上游默认 6」。

两条经复量**都不成立**：对齐关系不存在（量纲差 2）；「恒 ~50」是 `max_bi_num` 回落默认值 50 的
**截断伪影**，放开后真值为 326→130→88→56→44（陡降）。

⇒ **不需要双方拍板，这是可复现的事实更正。** 但结论「取 6」本身是否保留，取决于 D1。

### 8.3 记录纪律

- 有分歧就留分歧，不写成「待定」然后渐渐被当成已定。
- 每条的「**缺什么才能收敛**」是这张表最有用的列：它把「吵不完」和「测一下就能定」分开。
- 决定后**就地改写对应小节**并删掉它的「缺什么」行，不新建文件（文档纪律）。

---

## R86（2026-10-21）：涨跌停标记、复权口径、回测层、首页首屏、盘中采集

用户 2026-10-21 指令（原文）：「A、B、C都要做。另外CPT 这边的分钟或盘口数据、盘中分析等可以先把代码写起来，但是不接进项目。有什么小分歧你自己处理，必须我决定的就等我。」

### 8.4 C：回测层第一次存在，并复现了 R70

`cpt/backtest/` 之前**完全不存在** —— R70 那七轮排除性验证全部是 `/tmp` 下的一次性探针，没有一行进仓。后果是「回测」这件事在仓里不可复现、不可复核、不可被测试钉住。本轮建了四块：

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `cpt/backtest/panel.py` | 面板装配（每票一条 `BarPanel`） | `returns` 存**小数**不是百分数；`return_at(h,i)` 越界返 `None` 不补 0 |
| `cpt/storage/backtest_panel_store.py` | 取数（SQL 只在这一层） | 两条 `LEFT JOIN` 见下 |
| `cpt/backtest/stats.py` | 样本 / 汇总 / 配对 / 超额 / 对比 | 结论措辞由代码决定，不由调用方手写 |
| `cpt/backtest/__init__.py` | — | — |

`PANEL_SQL` 的两条 join 各修一个会造出假结论的口径：

1. **`LEFT JOIN public.derived_bar`** —— 用 `INNER JOIN` 会悄悄少 221 行（`daily_bar` 3,445,867 vs `derived_bar` 3,445,646），而这 221 行正好是最近的、还没跑批的日子。缺标的按「未标记涨停」处理（保守方向：宁可多算一个样本，也不悄悄少算）。
2. **`LEFT JOIN asel.ref_adjust_factor`** —— **未复权价在除权日造假跌**。实测 300 只票、76,798 个 h20 样本：后复权 +0.133%/胜率 44.7%，原始价 +0.031%/44.3%，29 个样本差超过 15 个百分点（10 送 10 让股价从 20 掉到 10，按原始价记就是 −50%）。缺因子的位置**不出现在 `returns`** 里，不拿原始价顶替。

**与 R70 逐项吻合**（严格口径：`t` 从 250 起、`t+h` 落在 2026-10-19 前、`tradable_only=False`、`min_bars=250`）：

| h | R70 日志 (`.tmp_audit/probe10.log`) | 本轮 `load_panel` |
|---|---|---|
| 1 | n=1,398,958 / +0.001% / 47.0% | n=1,398,958 / +0.001% / 47.0% |
| 5 | n=1,378,445 / +0.044% / 46.4% | n=1,378,445 / +0.043% / 46.4% |
| 10 | n=1,352,815 / +0.196% / 46.2% | n=1,352,815 / +0.195% / 46.2% |
| 20 | n=1,301,564 / +0.479% / 45.4% | n=1,301,564 / +0.477% / 45.4% |

四个窗口样本数逐一相同，均值差在小数第三位（复权因子的微小差异）。⇒ **R70 的数字是可信的，`review` 里那批结论不用改。**

顺带查清了一件容易误读的事：同一批票、不卡预热期时 h20 是 **+2.52%**，卡到 `rn>250` 之后只有 **+0.25%**。差别不在数据，在「**第一年的样本全是高位**」—— 窗口从 2024-08-10 起，前 250 根恰是 2024 下半年的反弹段。任何「全市场平均涨多少」的数字都必须报它的预热期约定，否则读的人会以为是同一个东西。

年度收益（h20，含涨停、复权、`min_bars=250`、2024-08-10~2026-09-20）：2024 **+6.437%**（胜率 58.2%）、2025 **+3.493%**（58.2%）、2026 **−1.008%**（41.6%）。**同一个策略在不同年份能差 7 个百分点**，这是后面每次给收益数字都要配年份的原因。

### 8.5 C：`stats.compare()` 把 R70 最贵的教训写进了代码

R70 那个「structure_ready 20 日 +13.37%」的假结论，根源是非配对差值在任何样本单元下都能算出来，而那根本不是差异。`compare()` 的 `verdict` 判四级，顺序不许调：

1. 任一组 `n == 0` → 「样本不足」
2. 配对 `n == 0` → 「**配对比较不成立**，不能当差异读」
3. `|t| < 2` → 「与 0 分不开」
4. 否则报方向

第 2 级是 R70 的直接产物。另外 `_mean_vs_win_rate_divergence` 会在「均值正、胜率 <50%」时补一句 —— R70 的 confirmed 组就是均值 +0.02%、胜率 45.5%，只报均值会让人以为它赚钱。

**本轮修掉的一个真 bug**：初版把 `sd == 0` 判成 `indistinguishable_from_zero=True`（写法是 `t = None if sd == 0`，再按 `t is None` 判噪声）。于是「50 个样本全是 +5%」被说成与 0 分不开 —— **零方差是确定性，不是无信息**，逻辑反得很彻底。改为 `t = math.inf if mean != 0`。

### 8.6 C：涨停标记到底改变了多少

| 口径 | h20 均值 | 胜率 | 样本数 |
|---|---|---|---|
| 含涨停日 | +2.469% | 52.3% | 2,571,234 |
| 排除涨停日 | +2.469%（同值，见下） | 52.3% | 2,571,234 |

**结论：在「全体基线」这个样本单元上，剔除涨停日几乎没有影响**（+2.451% → +2.469%）。原因是涨停日占全体样本的比例很低，均值被稀释。

但对**事件组**影响不同：涨停次日 mean **+2.001%/胜率 58.1%**，非涨停次日 +0.022%/48.0%。如果一个信号恰好落在涨停日，它那一笔收益是**买不进去的**。所以 `tradable_only=True` 是事件分析的默认值，而全体基线可以用 `False` 报——两者**必须分开报**，混在一起就成了「用不可成交的样本抬高基线」。

### 8.7 B：首页第一屏「今天该不该看盘」

`dashboard/index.html` 在 `dashboard-body` **之前**加 `[data-testid=market-step-banner]`，默认 `hidden`。内容三段：

1. **一句话结论** —— `全市场 202 只里，66 只站在位置上（三步走完或差最后一步）`
2. **一行数字** —— `三步全走完 10 只 · 差最后一步 56 只 · 卡在第二步 19 只 · 不确定走到哪 117 只`
3. **一句该做什么** —— 带 R70 的 `+0.02%` / `46%` / 「不是买点」三处硬数字

三条设计约束：

- **复用 `remoteOps.signalOverview`，不另开接口**（同一份数据，两个渲染点；测试钉住源文件只许一处 `/signal-overview` 请求）。
- **无票时说「什么都不用做」**，不是隐藏 —— 「今天没位置」本身就是结论。
- **不许出现买卖词**，且必须带 R70 数字（`tests/test_market_step_banner.py` 有反向注入验证）。

### 8.8 D：盘中采集与分析（**不接进项目**）

用户原话是「先把代码写起来，但是不接进项目」。三块按层放：

| 文件 | 层 | 职责 |
|---|---|---|
| `cpt/adapters/intraday_probe.py` | adapters | 拉涨幅榜 + 并到五档行情，产出候选 |
| `emotion_cpt.structure.intraday_rules` | domain | 涨跌停价、板别、封单强度（纯函数零 IO） |
| `cpt/application/intraday_analysis.py` | application | 封板结构 + 强度分位数 |

**「不接进项目」的可验证形式**：`tests/test_intraday_probe.py::test_nothing_imports_the_probe_in_production_paths` 用 AST 扫全仓，任何 `Import`/`ImportFrom` 指向 `intraday_probe` 就红。注意判据**只认 import 语句**——初版搜整份文件文本，于是 `intraday_analysis.py` 的 docstring 里提了一句「见 `cpt.adapters.intraday_probe`」就被判成「接线了」。注释里提一句不是接线。

**两个实跑才能发现的真 bug**（都已修 + 回归钉子）：

1. **920 号段漏判**。920 是北交所 2026 新号段，既不以 8/4 开头也不在主板的 `600/601/603/605/000/001/002/003` 里，掉进兜底的「主板按 10%」分支。实测 920296 昨收 11.45：10% 涨停价算成 12.60，30% 才是 14.89 —— **差 2.29 元，足以把「还差两块」误判成「已封板」**。
2. **负距离被当候选**。初版只判 `distance > margin` 就跳过，于是涨了 217% 的票（现价 36.30 > 涨停价 14.89）算出 `distance = -144%`，落在阈值内被排成「最接近涨停」第一名。修法：`if distance < 0 or distance > _CANDIDATE_MARGIN_PCT: continue`。

**2026-10-21 收盘的真实形态**：涨幅榜 100 行 → 候选 40 只全主板 → 五档 40/40 采到 → 封板 40 只。封单 `600293 三峡新材 1,050,299 手`、`002076 星光股份 565,081`、`603278 大业股份 303,669`、`002724 海洋王 196,355`。原始形态 `last=4.75 prev_close=4.32 bid1_vol=105,029,924 ask1_vol=0.0`，`bids` 五档齐全而 **`asks` 全 `None`** —— 这就是封死的样子。

与 emotion-core 的 `algorithms/intraday.py` 是**有意的两份**：CPT 这份是**口径**（采集与分析用），emotion-core 那份是**判据**（触发用）。理由见 ADR 0003 §E2 的分工表。差异由 `test_intraday_rules.py::test_matches_emotion_core_implementation` 逐条对账钉住。920 那个 bug 两份都犯过，两边都改了。

### 8.9 门禁：SQL 分层门禁一直漏掉新层

`scripts/check_sql_layering.py` 的 `FORBIDDEN_DIRS` 原为 `("domain","application","web","llm")` —— **`cpt/backtest/` 不在里面**，新建的层天然不受管。更糟的是 `_scan_file` 只在 `execute(` 后 300 字符内找 SQL 关键字，`cur.execute(sql)` 这种**把 SQL 放变量里**的写法**完全漏报**（实测对 `panel.py` 检出 `[]`）。

修法两条：① `FORBIDDEN_DIRS` 加 `"backtest"`；② 加兜底——文件里既有 `execute(` 又有 `SELECT…FROM`/`INSERT INTO`/`DELETE FROM`/`UPDATE…SET` 字面量就报在第一个 `execute` 上。修后确实抓到 `cpt/backtest/panel.py:148`。

**这条值得单独记**：门禁覆盖不到新目录时，不是「新目录很干净」，是「没人看」。新建一层就得先问一句「哪个门禁管它」。

## R87（2026-10-22）：把 R70 的 A/B 探针搬进仓里

用户 2026-10-22 指令（原文）：「要 继续」—— 承接 R86 的 C 项遗留：R86 建好了**面板与统计**，但「怎么从面板算出信号」还留在 `/tmp/probe10_ab.py` 里。

### 8.10 为什么搬

R86 之后 `cpt/backtest/` 能算「给定信号收益多少」，但**信号本身算不出来** —— `probe10_ab.py` 里那段「逐根切 `bars[:t+1]` → 算结构 → 判一买」没有任何测试、没有前视约束、用的是自己按 `0.098/0.198` 算的涨停阈值（违反 `emotion_cpt.structure.a_share_rules` 模块头「CPT 不重新计算涨跌停」）。搬到仓里的三块：

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `cpt/backtest/signals.py` | `structure_signals()` 逐根算结构信号 | **`sub = list(bars[:t+1])`**，第 t 根只看得到 t 及以前 |
| `cpt/backtest/runner.py` | `ab_pairs()` / `collect_status_samples()` / `run_backtest()` | 配对只认同一结构、两端都要能成交、不许用未来筛样本 |
| `scripts/backtest_run.py` | 命令行，输出报告 / JSON | 涨停口径走 `--include-limit-up` 显式开关，不静默改 |

### 8.11 三条纪律为什么各配一条测试

R70 的 +13.37% 假结论只犯了**第三条**（用未来筛样本），但前两条同样能造出假结论，所以各自钉死：

| 纪律 | 破法 | 测试 | 实跑注入结果 |
|---|---|---|---|
| 前视不变式 | `sub = list(bars)` | `test_signals_never_see_future_bars` | 注入后 1 failed ✅ |
| 配对两端都要能成交 | 只判 A 端 | `test_ab_pairs_drops_when_b_side_is_limit_up` | 注入后 1 failed ✅ |
| 不许用未来筛样本 | 只收后来 confirmed 的 | `test_status_samples_take_every_ready_regardless_of_future` | 注入后 2 failed ✅ |

`test_signals_never_see_future_bars` 的写法值得留下：假后端每次记下 `bars[-1].close` 与 `len(bars)`，断言 `seen_len == list(range(21,31))` —— 它证的不是「结果看起来对」，是「喂进去的长度逐根对得上」。

### 8.12 两个口径坑（本轮实跑撞出来的）

**坑一：`status_days` 不是「跃迁次数」。**
60 只票实跑给出 `status_days` 合计 8,390、`transitions` 合计 646，第一版 CLI 把前者标成「跃迁次数」，差 12 倍。真相：`structure_signals` 每根都重算一遍结构，`status_days` 记的是「状态不变也被重算过的天」，`transitions` 只在状态或背驰态变化时 +1。**样本量必须用 `transitions`。**

**坑二：配对里「没配到就绪日」被静默吞掉。**
`ab_pairs` 里 `confirmed` 找不到前序 `structure_ready` 时原本直接 `continue`。R71 之前不落 `structure_ready`，这类行在历史数据里是成批存在的，沉默丢掉会让配对样本量看起来比实际大。已加 `PairSet.unpaired` 计数并进 CLI 输出，配 `test_ab_pairs_counts_confirmed_without_a_ready` 钉住。

### 8.13 顺带补上的门禁漏洞

`scripts/check_doc_counts.py` 的 `CHECKS` 清单里**没有 `cpt/backtest`** —— R86 建了这个层，但「N 文件 / M 行」这类断言从没被核过（R86 那条教训「门禁覆盖不到新目录时不是干净，是没人看」当场又应验一次）。已补，并给 `architecture.md` §2.1 加了 `backtest/ | 5 | 1,075 |` 一行。

### 8.14 小规模实跑（60 只，0.8 分钟，验证脚本能跑通）

60 只票 / 31,680 根 / 646 个事件 / 配对 n=34（h20 n=31）。四个 horizon 的配对结论**全部「与 0 分不开」**（t 值 -1.18 / +0.20 / +0.84 / +0.01）。样本太小，不作为结论 —— 用途只有一个：证明 CLI 端到端能跑、口径输出正确。

全市场（5,131 只）实跑结果见下一节。

### 8.15 全市场实跑（5,131 只，72 分钟，dropped=0）

`PYTHONPATH=. .venv/bin/python scripts/backtest_run.py --json /tmp/bt_full2.json`：

信号计数：状态变化合计 **56,624**（structure_ready 26,428 / invalidated 25,125 / confirmed 5,071）；
逐日状态 711,692（后者比前者大 12 倍，见 §8.12 坑一）；算不出结构的 bar 702,639 根。

**各状态单独当买点（不依赖未来，每个事件都收）**：

| 状态 | h1 | h5 | h10 | h20 |
|---|---|---|---|---|
| `structure_ready` | −0.144% / 46.4% | −0.128% / 46.4% | +0.017% / 45.6% | +0.559% / 45.6% |
| `confirmed` | +0.061% / 47.4% | +0.029% / 44.7% | +0.423% / 44.9% | +0.569% / 45.4% |
| `invalidated` | −0.156% / 46.9% | −0.041% / 47.5% | +0.084% / 45.6% | +0.045% / 43.9% |

**配对（同一结构，A=就绪日买 vs B=确认日买）**：

| h | n | A | B | 差 | t | 结论 |
|---|---:|---|---|---|---:|---|
| 1 | 4,077 | −0.044% | +0.007% | −0.051% | −0.53 | 与 0 分不开 |
| 5 | 4,034 | +0.157% | +0.115% | +0.042% | +0.18 | 与 0 分不开 |
| 10 | 3,985 | +1.202% | +0.592% | +0.610% | +1.75 | 与 0 分不开 |
| **20** | **3,876** | **+3.117%** | **+0.592%** | **+2.524%** | **+5.69** | **见下，不可采信** |

### 8.16 h20 的 +2.524% 又一次是选择偏差（决定性证据）

`runner.ab_pairs` 已经修了 R70 那三条纪律中的两条半，但**第三条没修干净**：配对样本仍然只包含「最终走到 confirmed 的结构」，而 `structure_ready` 想走到 confirmed 就必须在后面出现反向笔 —— 在下跌段里能等来反向笔的，本身就已经是反转的那一批。

800 只票抽样复核（`/tmp/check_h20.py`，配对 n=563）：

| 指标 | 均值 | 中位数 | 判读 |
|---|---|---|---|
| A 的 h20 | +0.884% | **−3.411%** | 中位数为负 —— 均值被少数样本拉高 |
| B 的 h20 | +0.264% | −1.325% | 同向 |
| `pre20`（ready 前 20 日涨幅） | **+4.714%** | +4.369% | 信号日**之前**已经涨过 |
| `lowpos`（ready 收盘 ÷ 前 20 日最高） | 91.3% | 93.5% | 站在阶段高点，**不是低位** |

**按 pre20 分桶，A 的 h20 单调递增**：

| pre20 区间 | n | A 的 h20 |
|---|---:|---|
| [−100%, −10%) | 92 | **−3.679%** |
| [−10%, −3%) | 85 | −2.030% |
| [−3%, +3%) | 78 | +0.052% |
| [+3%, +10%) | 139 | +3.335% |
| [+10%, +100%) | 168 | +2.921% |

**结论**：涨跌幅是动量在起作用，不是结构信号。这与 R70 那次 +13.37% 的机制完全同源，只是量级被 `ab_pairs` 的配对纪律压到了 +2.5%。

**`gap` 那个异常已查清，是我的复核脚本写错，不是产品 bug。**
首版 `/tmp/check_h20.py` 用 `stat()`（按百分数格式化）打印 gap，把 `20` 交易日渲染成 `+1981.705%`，我误读成「1981 交易日」。改正后 300 只票抽样：`gap min=10 / p25=17 / median=20 / p75=23 / max=34`，`structure_signals` 的 `index` 确实是窗口内 bar 下标（000002 实测 `idx=252…399`，窗口 529 根）。

**教训**：复核脚本的输出格式本身也会骗人 —— 把带单位的量丢进「按百分比格式化」的打印函数，读出来的数是假的。凡是报告里的数字，先确认打印格式与量纲匹配。

### 8.17 待办（**R88 已收敛**：配对口径按用户决定废弃，只保留横截面）

> **2026-10-22 决定：走「废弃」那条，不补「不依赖未来」。**
> `ab_pairs` / `PairSet` / `BacktestResult.pairs` / `BacktestResult.comparison()` 全部删除，
> `scripts/backtest_run.py` 不再有配对段，JSON 里不再有 `pairs` 键。
> 理由：R70 与 R87 两轮配对都得出过表面很强的差，两轮都查出是「样本被未来信息筛过」；
> 真正修好的做法要换样本来源（参考日 = 信号日 + 5，只要求参考日之后有 confirmed），
> 那是另一个研究，不是改一个过滤条件。**在那之前，配对数字一律不进产品文案。**
> `tests/test_backtest_runner.py::test_no_pairing_api_survives_r88` 钉住「不许悄悄复活」。

下面两条是当时记的待办，保留原文以便追溯：



1. **配对口径补上「不依赖未来」**：`ab_pairs` 要用作买点比较，结构必须来自不依赖后续 confirmed 的筛选；否则只能当「R70 假结论的复现」用。**这一条没解决之前，h20 的 +2.524% 不许进任何产品文案**（首页横幅、信号卡片都只能引用 `status_samples` 那组横截面数，即 `structure_ready` h20 +0.559% / 胜率 45.6%）。
2. **`confirmation` 是否真有价值**：`status_samples` 里 `confirmed` 的 h20（+0.569% / 45.4%）与 `structure_ready`（+0.559% / 45.6%）几乎一样，说明「等反向笔确认」这个动作**没有任何增益**。这是可以进产品文案的结论（与 R70 一致），但结论的边界要写清：它比较的是两组**不配对**的事件样本。

### 8.18 可复核清单（R87 交付，复跑这一段就够了）

拿到这份文档，按下面四条就能独立复现上面的数字，不需要翻历史探针。

| 要复核什么 | 命令 | 期望看到的 |
|---|---|---|
| 导出面完整 | `PYTHONPATH=. .venv/bin/python -c "import cpt.backtest as b; print(len(b.__all__))"` | `23` |
| 脚本能跑通（不用全市场） | `PYTHONPATH=. .venv/bin/python scripts/backtest_run.py --limit 5 --days 400 --quiet` | 「各状态单独当买点」与「配对」两段都在，两段都把 n 与丢样原因打印出来 |
| 全市场结论 | `PYTHONPATH=. .venv/bin/python scripts/backtest_run.py --json /tmp/bt.json` | 约 72 分钟；`structure_ready` h20 **+0.559% / 45.6%**、`confirmed` **+0.569% / 45.4%**、配对 h20 **+2.524% / t=+5.69** |
| 那 2.524% 为什么不能用 | 见 §8.16、§8.17 | `pre20` 均值 +4.714%、`lowpos` 中位 93.5%、按 `pre20` 分桶单调递增 → 动量，不是结构信号 |

**三个数字不许混用**，写文案按这张表取：

| 数字 | 来源 | 能否进产品文案 |
|---|---|---|
| `structure_ready` h20 +0.559% / 45.6% | `status_samples`，不依赖未来 | **能** —— 「位置提示」档位的唯一合法依据 |
| `confirmed` h20 +0.569% / 45.4% | 同上 | **能** —— 用来证明「等确认没有增益」 |
| 配对 h20 +2.524% / t=+5.69 | `ab_pairs`，样本被未来筛过 | **不能**，除非 §8.17 待办 1 解决 |

CLI 同时打「状态天数」与「状态变化（真正的事件，本脚本的样本量）」两列，两者差约 12 倍。**报样本量只用事件数**：`status_days` 是同一状态持续天数的累计，不是独立样本。

### 8.19 产品文案换数（R88，2026-10-22）

R87 以后页面还挂着 R70 那组数（`+0.11% / +0.18% / +0.02%`、胜率 46%），且 `signal_progress.py` 里还有一句「见底后会大涨：20 日新低后收阳 +0.81%、胜率 48%」。

**这句话是错的，实测反向。** 用同一批票、同一预热约定（`rn>250`、h20、`daily_bar` 原始价 + `derived_bar` 标签）复跑：

| 形状 | n | 20 日均值 | 胜率 |
|---|---:|---:|---:|
| 基线（随便挑一天） | 1,308,385 | +0.224% | 44.8% |
| 收阳（任意收阳） | 630,667 | +0.204% | 44.9% |
| 20 日新低后收阳 | 10,873 | **−0.248%** | **43.5%** |
| 19 日新低后收阳 | 47,339 | +0.491% | 47.1% |
| 20 日新低 2% 以内后收阳 | 94,874 | +0.019% | 45.1% |
| **跌超 4%（任意收盘）** | 20,189 | **+4.047%** | **58.4%** |

- 「20 日新低后收阳」比基线**更差**。R70 文档里那个 +0.808% 复现不出来，而且它没写预热约定 —— 老数字不可用。
- 「跌超 4%」这一条复现稳定（+4.05% / 58.4%），是**唯一**能拿出去说「比它更有差别」的形状。
- 注意「新低」的定义敏感：`cl[i] <= min(cl[i-20:i])` 与 `<= min(cl[i-19:i])` 差 5 倍样本、差 0.74 个百分点。**任何「新低」类形状必须在文案或文档里写清窗口含不含当天。**

**换了什么**（`git show` 可核对）：

| 文件 | 老数字 | 新数字 |
|---|---|---|
| `dashboard/index.html` 实测横幅 | `688,496 次回放` / `+0.11% / +0.18% / +0.02%` / `46%` / 新低后收阳 `+0.81%` | `位置已到 +0.56% / 45.6%`、`反抽确认 +0.57% / 45.4%`、`随便挑一天 +0.22% / 44.8%`、`跌超 4% 之后 +4.05% / 58.4%` |
| `dashboard/dash-core.js` `STATUS_EDUCATION` | `structure_ready` 说 `+0.02%` 比基线 `+0.48%` 低；`confirmed` 说 `+0.21%`、胜率 40% | 两档都换成 R87 数，并明说「与位置已到几乎一模一样，等确认拿不到额外收益」 |
| `dashboard/dash-signal.js` 首屏动作句 | `+0.02% / 46%` | `+0.56% / 45.6%` vs 随机 `+0.22% / 44.8%` |
| `cpt/application/recommendation.py` 的 `confirmed` headline | `+0.02% / 46%` | `+0.57% / 45.4%`，并与 `+0.56% / 45.6%` 并列 |
| `cpt/application/signal_progress.py` | 模块 docstring + 「20 日新低后收阳 +0.81%」 | R87 三档数 + 「跌超 4% 之后 +4.05% / 58.4%」 |

**为什么必须换，而不是留着两套数**：`+0.02%` 是 confirmed **事件**组，`+0.48%` 是**全体 K 线**基线（§8 开头那个样本单元错配）。同一张卡片上一句用事件组、一句用全体基线，读起来就是「信号比随机还差」，而 R87 对同一批票测出来的是「几乎一样」。**两个数的样本单元不一致，放在一起就是错的。**

**测试也跟着改，不是放宽**：`tests/test_signal_progress_wiring.py` 从「断言 `688,496` 在场」改成「断言 R87 三个数在场 + 老数字不在场」（`for stale in ("688,496", "+0.11% / +0.18% / +0.02%", "+0.81%")`）；`test_recommendation.py`、`test_market_step_banner.py`、`test_signal_progress.py` 各自把断言改成新数。**老数字一旦被谁抄回去，测试就红。**

复跑命令：

```
PYTHONPATH=. .venv/bin/python scripts/backtest_run.py --limit 300 --days 800   # 面板层三档
PYTHONPATH=. .venv/bin/python .tmp_audit/shape_probe.py                        # 8.19 那张形状表（一次性探针，未入仓）
```

## R89（2026-10-22）：盘中采集守护 —— 把 emotion-core 的观察期从「无对象」变成「有对象」

### 9.1 这条为什么是主线，不是支线

emotion-core 2026-10-22 的原话：

> 质量闸门先过再建表。§D10 观察期条件②（盘中采集任务无失败）现在**无对象
> 可观测**——CPT 那边采集器还没接线。等它落地并跑起来，才开始计 5 个交易日。

也就是说：**emotion-core 卡在这一步，等的不是判据，是一个真实在跑的采集任务。**
CPT 侧 R86 写好了 `IntradayCollector`，但当时**故意不接线**（测试
`test_nothing_imports_the_probe_in_production_paths` 钉着），代价就是没有对象。

R89 把这个对象补上，但**只补采集、不补判定**。

### 9.2 边界（写在 `scripts/intraday_collectd.py` 模块头，改之前先看）

| 做 | 不做 |
|---|---|
| 按 timer 拉起、采涨幅榜 + 五档 | 判「该不该触发买点」 |
| 每轮写一行 JSON 日志 | **建表**（`intraday_quote` 归 emotion-core） |
| 只读 DB 判今天是不是交易日 | 写任何数据库 |
| 连续失败 3 轮才报警 | 单轮失败就报警 |

**为什么不做判定**：同一个「封板」两个仓各判一次，口径迟早分叉 —— R88 里
`board_of` 顺序之争就是前车之鉴。CPT 只出**原始材料**。

### 9.3 「无失败」的可观测口径（对 emotion-core 交代清楚）

- 一轮 = 一次 `IntradayCollector.scan()`，结果只有三态：
  - `ok` —— 拿到候选与报价；
  - `empty` —— 榜单空（非交易时段或真没票接近涨停）。**不算失败**，午休与竞价前都会 empty；
  - `error` —— 抛异常。**连续 3 轮**才算采集挂了，进程 rc=1。
- 运行总结字段固定：`{"rounds","ok","empty","errors","first_error"}`。
- 日志：`/home/ubuntu/logs/intraday-collect.log`，一轮一行 JSON。

### 9.4 部署形态：systemd timer，不是 cron

| | cron | systemd timer（选它） |
|---|---|---|
| 跨午休停 | `*/2 1,2,5,6 * * 1-5` 会多跑 11:32~13:00、15:00 后一堆空轮 | `OnCalendar=*-*-* 09:30/2:00 Asia/Shanghai` 把时段写进表达式 |
| 可预演 | 否 | `systemd-analyze calendar` 能打出未来几次触发 |
| 时区 | 靠机器本地时区（本机是 UTC） | 表达式里显式带 `Asia/Shanghai` |

**踩到的坑**：把一个 OnCalendar 里塞两个区间
（`01:30/2,5:00/2:00 Asia/Shanghai`）**解析不了**，`systemd-analyze calendar`
报 `Invalid argument`。必须拆成两行 `OnCalendar`。已写进 timer 注释并被
`tests/test_intraday_collectd.py::TestDeployment` 钉住。

实跑预演：上午行 `Next elapse: Fri 2026-10-23 01:30:00 UTC`（= 北京 09:30），
下午行 `Next elapse: Thu 2026-10-22 05:00:00 UTC`（= 北京 13:00）。

### 9.5 验收实跑

| 项 | 结果 |
|---|---|
| 手跑一轮（`--once --ignore-session`） | `{"rounds":1,"ok":1,"empty":0,"errors":0}`，2.49 秒 |
| 那一轮的真实内容 | `rank_size 100 → 候选 27 → 已封 26 · 逼近 1 · 判不了 0`，封单中位 188,616 手，`board_label=厚` |
| `systemctl start cpt-intraday-collect.service` | `Result=success` `ExecMainStatus=0`；因本机库里最新交易日是 10-21（今天 10-22 行还没进库），正确输出 `skipped`：`库里的最新交易日比今天旧 —— 今天休市` |
| timer 已启用 | `systemctl list-timers` 显示 `Thu 2026-10-22 05:00:00 UTC` 下次触发 |
| 单元与仓内副本 | `diff` 逐字节一致（service + timer） |
| 新测试 | `tests/test_intraday_collectd.py` **19 passed**；反向注入「error 伪报成 ok」→ 1 条红 |
| 全量 pytest | **2238 passed / 5 skipped / 0 failed** |
| 11 道门禁 | 全绿；`ruff check .` All checks passed；mypy Success |

### 9.6 对 emotion-core 的两句回话（他们问的）

1. **「判据别改回去」** —— CPT 侧顺序**本来就是对的**（`growth → bse → st_main → main`），
   不需要改。R88 做了反向的保护：模块头写「判据锁」段，`TestBoardOrderingIsLocked`
   12 条（含源码级断言 `_BSE_PREFIXES` 必须在 `return "st_main"` 之前）。
   反向注入把 bse 挪到 st_main 之后 → 4 条红。跨仓对账复跑 **44 组，差异 0 处**。
2. **「先接线才有观察对象」** —— R89 已接线：`cpt-intraday-collect.timer` 交易时段每
   2 分钟拉起一次，只采集不判定，连续 3 轮失败才 rc=1。**从 2026-10-22 起可以开始
   计那 5 个交易日**，判据见 §9.3。

### 9.7 遗留（不阻塞，但要记）

- **北交所按用户决定不接**（2026-10-22）。后果：`board_of` 的 `bse` 分支有代码、
  有对账测试、有判据锁，但**本地 `daily_bar` 里一只北交所票都没有**（实测
  `920/8/4` 号段 count = 0）。它是「写了但无生产对象」的分支 —— 这正是跨仓对账
  只能靠人肉跑探针的原因。接数据前，任何北交所结论都不许进产品。

---

## 10. R89 收尾（2026-10-22）：采集状态上首屏、观察期巡检补第五项

R89 本体（§9）只做了「采起来」。采起来之后有两个新问题，这一节是收尾：

### 10.1 B3：首页第一屏能看见「盘中采集活没活」

用户问「如果我有新的关注，会自动跑数据吗」。回答时发现一个坑：**采集在跑，
但界面上没有任何地方能看出来它在跑**。采集中断只能等飞书巡检（每 2 小时）
才发现 —— 中间最多瞎 2 小时。

落点：

| 位置 | 内容 |
|---|---|
| `cpt/application/intraday_status.py`（新） | `parse_log_tail()` 读日志尾部 8 KiB；`collect_status()` 判四态 |
| `cpt/web/app.py` | `GET /api/dashboard/intraday-status` → `{available, basis, log_path, status}` |
| `dashboard/index.html:195` | `[data-testid=intraday-status-line]`，在**第一屏横幅**里、`dashboard-body` 之前 |
| `dashboard/dash-signal.js` | `INTRADAY_STATE_LABELS` + `renderIntradayStatus()` |
| `dashboard/dash-ops.js` | `loadIntradayStatus()`，跟着 `refreshOps` 走，**不进 30s 轮询** |

四条口径（都写进了 docstring）：

1. **四态不许合并**：`running` / `stale` / `failed` / `never`。合成一句「有点问题」
   等于没说 —— 用户需要知道是该等、该查日志、还是该改配置。
2. **`empty` / `skipped` 不算失败**。午休、竞价前都会空；把它们算失败就是
   每周五次假警。
3. **「今天休市」与「采集挂了」必须分开**。判据是同一个 `market_day != today`，
   但一个是正常（周末）、一个是故障（盘中）。`failed` 优先于「休市」——
   连续报错时先说报错。
4. **日志坏行跳过不抛**。写一半断电是常态。

### 10.2 反向注入挠出来的测试漏洞（这一轮最值得记的）

第一版 `test_empty_and_skipped_are_not_failures` 只放 **2 条** empty 记录。
我把 `empty` 注入成「算失败」，跑出来 **21 条全绿没一条抓住**。
原因是连续失败阈值是 3 —— 2 条 empty 就算全算错也到不了阈值，状态还是
`running`。

**光看「测试全绿」不能证明测试有效。** 改法是凑够 `max_failures` 条（3 条），
注入后立刻 **1 failed**，抓到了。这条教训写进了测试的 docstring。

### 10.3 B4：观察期巡检补第五项

`scripts/watch_period_check.py` 加 `check_intraday()`：

- **只在盘中（北京 09:30–15:00）判**。收不了盘后当然没有新记录，全天候判
  就是每天 15:00 之后必红一次 —— 人开始忽略告警时，告警就死了。
- **「最新一条是 error」不算问题**。单轮失败是常态；连续失败由
  `intraday_collectd` 按 `max_failures` 判 rc=1，那是它自己的职责。
- 放在**网络段之外**（`--skip-network` 也跑）。它恰好就是在弱网下最该判的东西。

手动跑巡检那次四加一项全绿：

```
✅ 服务    cpt-dashboard.service active（health ok）
✅ 序列    structure 910xxx / signal 9xx（都没有落后）
✅ 批次    0.9 小时前
✅ 盘口    600519 10/10 档
✅ 盘中采集  age_minutes=0.9（盘中）
```

### 10.4 B1：形状扫描基线也入仓了

`cpt/backtest/shapes.py` + `scripts/shape_scan.py`：把 R88 那句
「跌超 4% 之后 +4.05% / 58.4%」变成可复跑的 `drop_4pct`，并且**同批票同窗口
同预热**量基线。全市场复跑结果（5,131 只 / 预热 250 / h20 / 剔涨停）：

| 形状 | n | 20 日均值 | 胜率 |
|---|---:|---:|---:|
| `drop_4pct`（收盘 ≤ 前 20 日最低 × 96%） | 19,592 | **+4.37%** | **59.4%** |
| 累计跌 4%（前 20 日跌幅，不管位置） | 476,252 | +1.07% | 49.4% |
| 累计跌 4% 且收阳 | 228,660 | +1.04% | 49.5% |
| `low_20_up`（20 日新低后收阳） | 10,435 | **−0.02%** | 44.2% |
| `near_low_20_up` | 93,989 | +0.17% | 45.8% |
| `below_ma20_up` | 270,051 | +0.74% | 47.6% |
| **基线（随便挑一天）** | 1,289,208 | **+0.35%** | — |

R88 的 +4.05% / 58.4% 复现成立（差 0.3pp 来自窗口终点从 10-19 推到 10-22）。
最值得注意的是**三个近义「跌 4%」差 23 倍样本**：说「跌了 4%」有 47 万样本、
只多 0.7pp；说「跌到前 20 日最低的 96% 以下」只有 1.9 万样本、多 4pp。
产品文案已经改成后者并在旁边写明了这个对照 —— **一个词的差别就是 23 倍样本**。

### 10.5 验收

| 项 | 结果 |
|---|---|
| 全量 pytest | **2285 passed / 5 skipped / 0 failed**（145.56s） |
| 新增测试 | `tests/test_intraday_status.py` 21 条、`tests/test_backtest_shapes.py` 26 条、`tests/test_watch_period_check.py` +3 条（共 24） |
| 反向注入 | ①empty 算失败（测试写弱时漏报 → 改强后抓到）②去掉盘中判断（抓到）③`drop_in_window` 加收阳（抓到） |
| 11 道门禁 | 全绿 |
| 门禁修的一处真问题 | SQL 分层：`app.py` 里写 `SELECT max(date) FROM daily_bar` 被门禁抓到，**下沉到 `cpt/storage/backtest_panel_store.latest_market_day()`** |
| 线上 | `/api/dashboard/intraday-status` → `available:true, state:running`；`/cpt/` → 200，页面含 `intraday-status-line` |
| `ruff check .` | All checks passed |

### 10.6 装完才发现的两个真 bug（这一节比 10.1~10.5 更值钱）

R89 第一次提交（`ac776748e`）后，我在交易时段实跑巡检，`check_intraday`
立刻报「12 分钟没有新记录」。顺着查下去，露出两个**上线才算出来**的 bug。
两个都通过了代码审查、通过了 21+26 条测试、通过了 CLI 手动跑 —— 因为它们
都不在「被测试覆盖的那一半」里。

#### bug ① timer 的 `/STEP` 是小时步长，不是分钟

```ini
# 本意：从 9:30 起，每 2 分钟一轮
OnCalendar=*-*-* 09:30/2:00 Asia/Shanghai     # ❌ 实为「每 2 小时的第 0 分」
```

`systemd-analyze calendar` 给出 `Next elapse: 明天 01:30 UTC` —— 一天只跑
3 次（09:30 / 11:30 / 13:30）。`systemctl list-timers` 显示 active，日志
显示 `outcome` 正常，**没有任何一处报错**。

正确写法（小时写范围，分钟写步长）：

```ini
OnCalendar=Mon..Fri *-*-* 09..11,13..14:00/2:00 Asia/Shanghai   # ✅ 每 2 分钟
```

顺带作废了 R89 首版的一条注释：「一个 OnCalendar 塞两个时间点解析不了，必须
拆两行」。失败的不是「两个区间」，是把 `/2:00` 当分钟用。见 known-traps #64。

#### bug ② 拿 `daily_bar` 判交易日，盘中恒为假

```python
# 本意：库里最新日线是今天就开市，否则休市
SELECT max(date) FROM public.daily_bar   # ❌ 盘中永远返回昨天
```

`daily_bar` 存的是**收盘后**才落的日线。于是这个定时器每 2 分钟如实报
`skipped: 库里的最新交易日比今天旧 —— 今天休市`，**一次都没真正采过**，
而 `outcome` 不是 `error`、巡检在修好前也没报（修好后才加的第五项）。

判据要选「事先就有答案」的表 —— `public.trade_calendar` 年初就排好了。
改用 `is_trade_day(conn, today)` 之后 10:52:02 立刻采到第一轮真实数据。

#### 两个 bug 的共同点

**「跑起来了」和「跑对了」是两件事，而日志默认只证明前者。** 这两处都有日志、
都有非 `error` 的 outcome、timer 都 active。真正的证据只有两条：
①`systemd-analyze calendar` 的 Next elapse ②日志的 `outcome` 字段到底是
`ok` 还是 `skipped`。

修好后的实证（同一份日志前后对照）：

```
10:46:37  skipped  库里的最新交易日比今天旧 —— 今天休市   ← 旧判据
10:48:13  skipped  同上
10:50:01  skipped  同上
10:52:02  ok  rank_size=100 candidates=24 sealed=24 median_bid_lots=168744.76  ← 新判据第一轮
10:54:14  ok  rank_size=100 candidates=26 sealed=26 median_bid_lots=165810.76
```

巡检五项全绿：服务 / 序列 / 批次 / **intraday** / 盘口。

#### 补的测试（两条都做了反向注入）

| 测试 | 守什么 | 注入 | 结果 |
|---|---|---|---|
| `test_timer_does_not_use_the_hour_step_trap` | 小时字段必须是范围 | 退回 `09:30/2:00` | ✅ 抓到 |
| `test_trading_day_uses_the_calendar_not_daily_bar` | 必须**调用** `is_trade_day` | `verdict = None` | ✅ 抓到 |

第二条第一版**没抓到**：判据是「import 里有 `is_trade_day`」，而注入只换了
返回值、没删 import。改成查 AST 的 `ast.Call` 节点才抓到。见 known-traps #65
——「有没有 import」不是判据，「有没有调用」才是。

### R89.8 真跑一次就抓到：位置到了，话却说「位置还没到」

R89 装完当天用**生产同一套路径**（`NativeChanlunBackend` + `map_bi`/`map_zhongshu`
+ `derive_first_buy_facts` + `first_buy_progress`）跑了关注列表，000002 的输出是：

```
buy 三步: two_centers=done divergence_leg=done reversal_bi=failed | div=not_detected
卡在哪: 位置到了，但还没反弹：下跌背驰段走完了，价格停在低位。
       这一步的用处是提醒你别在恐慌低点割肉，不是叫你加仓。
该做:   不用做任何事：位置还没到，等第二步、第三步依次走完再来看。
```

**同一块里两句打架**：`explain` 说「位置到了、价格停在低位」，
`_action` 说「位置还没到」。根因是 `_action` 只判了第三步
（`states[2] != STEP_DONE` 就套同一句话），没区分「第二步都没走完」
和「前两步都走完、只差反弹」。

改法：`_action` 按 (states[1], states[2]) 两档说话 ——

| 情况 | 旧 | 新 |
|---|---|---|
| 第 1 步就卡住 | 位置还没到，等第二步、第三步 | 不变 ✓ |
| 前两步走完、只差反弹 | 位置还没到，等第二步、第三步 | 价格已经跌到位了，现在等的是「跌势里冒出一笔反弹」。这一步走完之前，别在恐慌低点割肉，也别在这里继续往下看空。 |

`tests/test_signal_progress.py` 加两条：`test_steps_1_and_2_done_says_waiting_for_the_bounce_not_position_not_reached`
（口径一致性：explain 承认到位置，action 不许否认）与
`test_first_step_failed_still_says_position_not_reached`（防止矫枉过正，
第一步卡住时那句仍必须说「位置还没到」）。删掉新分支反向注入 →
第一条红、第二条仍绿，说明两条各守一边。

**教训（可复用）**：翻译层的两条文案是**同一件事的两种说法**，它们必须
由同一组状态同时决定。分开写、各自判条件，就一定会出现互斥的描述。

### R89.9 关注列表回测（`--only watchlist`，R89 补的参数）

`scripts/backtest_run.py` 加 `--only watchlist`（走 `WatchlistStore` +
`watchlist_path_for()`，与 `/api/dashboard/signal-overview` 的 `watched` 同源；
读到空列表就如实跑 0 只，不偷偷退回全市场）。

**结论：1 只票的收益数字是噪声，没有意义，不要拿它下判断。**

| 状态 | n | h1 | h5 | h10 | h20 |
|---|---:|---|---|---|---|
| structure_ready | 5（丢 4 个窗口不足） | −0.194%/50.0% | −0.056%/25.0% | +0.098%/25.0% | −1.027%/50.0% |
| confirmed | 0 | — | — | — | — |
| invalidated | 7 | −0.398%/28.6% | +0.242%/42.9% | −0.693%/28.6% | −1.762%/14.3% |

同票在 2026-10-22 的定位（生产路径实跑）：下跌走势、前两步 done、
第三步 failed、`divergence_status=not_detected`。**没有任何信号处于可交易状态。**

全市场口径（R89 B1 扫描，5,131 只）才是有统计意义的数：
`drop_4pct` n=19,592 / 20 日 +4.37% / 59.4%；基线 +0.35%。

## 11. R90（2026-10-22）：让 emotion-core 能消费盘中数据 + `derived_bar` 缺口查清

用户 m06969：「A+B 都要做，然后同步文档，让 emotion 修改代码去消费数据」。

### 11.1 A：建表 + 写库 + 读取接口

此前 CPT 的盘中采集器**只写本地日志**，库里一张 `intraday` 表都没有 ⇒
emotion-core 消费不到（只能读没有代码的日志，或自己调 `fetch_quotes()` 自己
判涨停 —— 判据分叉，违反 ADR 0003 §E2）。

新增 `public.intraday_seal_snapshot`（迁移脚本
`scripts/migrations/2026-10-22_r90_intraday_seal_snapshot.sql`）。**只有 5 列**，
每一列都是采集端已经算出来的结论：

| 列 | 含义 |
|---|---|
| `at` | 这一轮采集时刻 |
| `code` | 6 位纯代码，与 `fetch_quotes` 的键同形 |
| `sealed` | `seal_strength()["at_limit"]` |
| `bid1_lots` | 封单量（手），判不了为 NULL |
| `limit_price` | 本轮用的涨停价（存下来自证口径） |

不存 open/high/low/涨跌幅 —— 那是 `daily_bar` 与日志的职责。用户此前明确反对
「为方便查询冗余多列」（叫停过 12 列里 7 列冗余）。

存储层 `cpt/storage/intraday_seal_store.py`：`insert_seal_snapshot`（`ON CONFLICT
DO NOTHING`、**不 commit**）、`load_latest_round`（**只取一轮**）、`load_seal_track`、
`load_session_rows`。三条口径写进模块 docstring：

1. **没行 = 判不了**，不是「没封板」；
2. `sealed=False` = 采到了、这一轮没封；
3. 下游按 `at` 分组取最新一轮，**不要按 code 单点查最新**（混轮次会造出并不
   存在的状态）。

采集器 `scripts/intraday_collectd.py` 加 `_persist()`：写「封住的」+「候选里
没封的」两类行。`CPT_INTRADAY_DB=0` 可关。

**写库失败不改 `outcome`** —— 只记 `db=warn:<异常名>` 并打 warning。行情断了与
库断了是两个不同的红灯，混成一个就没法排查。测试
`test_persist_failure_does_not_change_outcome` 钉住这条（反向注入把
`except Exception` 改成 `except ValueError` → 红）。

**踩坑**：`import psycopg` 也必须在 `try` 里。驱动装不上同样是「库这一侧坏了」，
放 `try` 外会直接打断整轮成 `error`。

回顾层 `cpt/application/intraday_review.py` + `scripts/intraday_review.py`：把一天的
快照翻成人话（ADR 0003 §C3 的「封住率」需要每日真实样本，那个 70.1% 来自一次性
回测）。口径：**「开过板」= 某个 True 之后出现过 False**，不是「没封着」；
**「最后封住」看当天最后一轮**，不是「出现过 True」；没有样本时 `hold_rate`
是 `None` **不是 0**（0 会被读成「一只都没封住」）。

实跑 2026-10-22：`今天 33 只封过板，最后一轮还封着 33 只（封住率 100%），其中
0 只中途开过板。样本：99 轮 · 每只票 3~3 轮`。

### 11.2 B：`derived_bar` 缺 221 行 —— 查清了，不补

| 事实 | 数 |
|---|---|
| `daily_bar` | 3,451,091 |
| `derived_bar` | 3,450,870 |
| 差 | 221 |
| 其中「上市首日」（`date = min(date)` per code） | **221 / 221** |
| 其中「非上市首日」 | **0** |

根因：`derived_bar` 不是 CPT 写的，是 **emotion-core 的
`services/derive_service.py`** 写的。`compute_derived` 要算 `cont_days`（连板数）
与 `amplitude`（振幅），这些量需要**前一根**日线 —— 新股首日没有，跳过是对的。

**所以这 221 行不该补**（补了就是编造），也不影响回测：`cpt/backtest/panel.py`
的 `PANEL_SQL` 是 `LEFT JOIN derived_bar`，缺行时标签读成 `None`；而
`_build_panel` 只在 `rn > 250`（预热期后）取样本 —— 首日永远落在预热期里。

**真正的问题不是缺口，是「差 221 这个数字不带信息」**：同样是 221，可能是首日，
也可能是上游整批断更，人眼分不出。

⇒ 新增**门禁⑱** `scripts/check_derived_gap.py`：断言缺口**全部**满足
`date = 该票 min(date)`，出现任何非首日缺口就红并列出样例。它与
`check_shared_tables.py` 同类（判据是数据事实，CI 的干净库答不了），执行点是
`deploy/cron/cron-daily-report.sh` 那一趟日报，红了发飞书。

实跑：`上市首日缺口 221 条（已知形态，不算问题） / 非上市首日缺口 0 条 / ✅`。
反向注入（假连接喂 `other_day=3`）→ 报红并列出 `600000 2026-10-22` 样例。

补跑：`derive_service.run(date(2026,10,22))` 写入 **5,224 行**，差值仍为 221、
非首日 0 条 —— 印证这 221 就是首日的稳定值，不是「今天没跑」。

### 11.3 一条流程事故（记下来）

`git checkout scripts/check_derived_gap.py` 想撤销反向注入，**但这个文件从没
提交过** ⇒ `git checkout` 报错且**没撤销**，反向注入残留在工作树里。幸好改的是
同一行、脚本内另有还原语句。
**规矩：对未跟踪文件不许用 `git checkout` 撤销，用备份或直接改回。**

---

## R91（2026-10-22）：首屏封板强度 + 生产表里那 18 行假数据

### 12.1 先修事故：产品测试往生产表写了 18 行假数据

设计首屏封板强度时，先盘点 `public.intraday_seal_snapshot`（117 行），发现其中
**19 行是探针**：`code=600519, sealed=True, bid1_lots=50000.0, limit_price=1100.0`，
**19 个独立单票轮**，时间戳全部晚于那 3 个真实轮次（`09:20:36Z` / `09:20:55Z` /
`09:22:13Z`，每轮 33 只）。

后果不是「多几行脏数据」，而是 **`max(at)` 落在探针行上**：盘中面板会显示
「1 只封板、封单 5 万手、强度 1.00 正常」—— 看着像真的，实际是 `test_intraday_collectd.py`
里 `600519 / 50_000.0 / 1100.0` 那个假夹具。

根因：`tests/test_intraday_collectd.py` 的 `_isolated_log` 夹具只隔离了**日志路径**，
没隔离**数据库**；而 `run_round()` 末尾无条件调 `_persist()`，`DB_WRITE_ENABLED`
默认为真。`test_no_candidates_is_empty_not_error` 与 `test_ok_round_carries_counts`
因此把假行情写进了生产表。

三层修法：

| 层 | 做法 |
|---|---|
| 测试 | 新增 autouse 夹具 `_no_real_database`：`monkeypatch.setattr(collectd, "DB_WRITE_ENABLED", False)`；配 `class TestNoRealDatabaseWrites` 两条钉子 |
| 读取 | `intraday_seal_store` 加 `PROBE_CODE_PREFIX = "\x01"` 与 `_REAL_ROWS`，**三处读取全部带过滤**（`load_latest_round` / `load_seal_track` / `load_session_rows`） |
| 验证 | 新建 `tests/test_intraday_seal_live.py`（5 条，**真库 + 必回滚**）：插一条比真实最新轮更新的探针行，断言 `max(at)` 不动、`counts` 一个数不变 |

反向注入实测：夹具改 `pass` → 2 failed；`_REAL_ROWS = "TRUE"` → 4 failed。
生产清理：先按 (code, bid1_lots, limit_price) 三元组精确圈定 19 行、核对「这些
`at` 上的总行数 == 19」（即确实是单票轮）与「真实轮里 600519 从未出现」，
备份到 `~/logs/intraday_seal_probe_rows_backup_20261022.csv` 后 DELETE。
结果：117 → **99 行**，只剩 3 轮真实采集。

**`%%` 坑**：`_REAL_ROWS` 里的 `%` 必须写成 `%%`。SQL 全走 psycopg 参数化路径，
裸写 `%` 报 `only '%s', '%b', '%t' are allowed as placeholders, got '%'`。

### 12.2 首屏「今天的封板情绪」

R89 的首屏横幅说的是**收盘后的日线结构**（三步进度），盘中不动。盘中最想知道的是
另一件事：今天的封板情绪怎么样。R90 落库之后有数据了，但那表是「每只票每轮一行」，
人读不懂。

新增 `cpt/application/seal_board.py::seal_board_summary(latest)`，把
`load_latest_round()` 的结果压成一句话 + 三个数 + 分档：

| 输出 | 来自 |
|---|---|
| 几只封着 | 最新一轮 `sealed=True` 的行数 |
| 封单中位 / 厚薄分档 | `board_health()` 对 `median_bid_lots` 的判据（厚线 5 万手） |
| 几只冲到板附近 | 最新一轮 `sealed=False` 的行数 |

三条纪律（都进了 docstring）：

1. **没数据不许说成「封板少」**：表里没有轮次 → `available=False` + 「今天还没有盘中
   封板快照 —— 采集器没跑过，或今天休市。这不是「今天没有票封板」」。
2. **有轮次但一只没封是另一种情况**：「板附近 N 只，没有一只封住…封板情绪按「没样本」
   处理 —— 不是「封得薄」，是压根没有样本可评」。
3. **不给买卖指令**：`action` 恒为「封板强度是位置描述，不是买入信号…」。

实测四分支（生产真数据 + 构造数据）：

| 输入 | headline |
|---|---|
| 真实最新一轮（33 只封、中位 35,644 手） | `17:22 这一轮：33 只封在板上；封单中位 35,644 手，封板偏薄。` |
| 一只没封 | `10:00 这一轮：板附近 1 只，没有一只封住。封板情绪按「没样本」处理…` |
| 空表 | `今天还没有盘中封板快照 —— 采集器没跑过，或今天休市…` |
| 有开板 | `10:00 这一轮：1 只封在板上、1 只冲到板附近没封住；封单中位 300,000 手，封板厚。` |

接线：`cpt/web/app.py::_seal_board_payload()`（降级不抛，`available=false` +
`seal_board_unavailable`）→ 路由 `/api/dashboard/seal-board` → 前端
`dash-signal.js::renderSealBoard()` → 首屏 `[data-testid=seal-board-line]`。

**与「盘中采集活没活」分工明确、不许合并**：那条读**日志**答「采集器在不在跑」，
这条读**表**答「盘面长什么样」。「采集器在跑但今天空盘」与「采集器挂了」的对策相反。

### 12.3 一个差点蒙混过去的 bug

`base.update({..., "as_of_hms": _hms(at), "headline": f"{base['as_of_hms']} ..."})`
—— **同一个 `dict.update()` 里读 `base[...]` 读到的是旧值**，headline 渲染出
`None 这一轮：33 只封在板上…`。改成先算 `stamp = _hms(at)` 再一起写。
这个 bug 单看代码很难发现，是**实跑真实数据**时打印出来才看到的。

### 12.4 另一处回归：插入函数时把 `_latest_market_day` 覆盖了

插入 `_seal_board_payload` 时按 `def _latest_market_day() -> Any:` 做锚点，
连续命中两处，结果 `_intraday_log_path` 的**函数名**被换成 `_latest_market_day`，
导致 `app.py:630` 出现 `F821 Undefined name '_intraday_log_path'`，
而 `test_payload_reads_the_configured_log` 在全量里红、单跑绿（因为它 monkeypatch
的 `_latest_market_day` 被第二次定义覆盖后语义变了）。
`ruff check` 的 F821 是抓到这个的唯一手段 —— **它不是噪音**。

### 12.5 验收

- 全量 pytest **2366 passed / 5 skipped / 0 failed**（128.87s）
- 12 道门禁全绿（`check_doc_drift` / `check_line_refs` / `check_doc_counts` 首次红，
  分别由新路由未登记、`app.py` 行号漂移 `1369 → 1403`、web 5,729→5,766 与
  dashboard 17,198→17,307 造成，改后转绿）
- 新增测试：`tests/test_seal_board.py`（11）、`tests/test_seal_board_wiring.py`（14）、
  `tests/test_intraday_seal_live.py`（5）
- 部署 + 重启后线上：`/api/dashboard/seal-board` →
  `available:true, 17:22 这一轮：33 只封在板上；封单中位 35,644 手，封板偏薄`；
  `/cpt/` 200；页面含 `seal-board-line`

## §13 R92（2026-10-22 夜）：把「观察期」变成可计算的闸门，并说清 10-22 到底采到了什么

### 13.1 为什么 CPT 自己也要一道闸门

emotion-core 的 `lab/cpt_watch.py` 已经在算「CPT 采集干净了几天」，但它数的是**日志轮次**。
CPT 这边要数的是**落库轮次** —— 两条链路会分开坏，R91 就是实例：10-22 日志 106 轮、
98 个 `ok`，库里只有 3 轮，因为落库是 17:20 才接上的。**日志漂亮不代表数据在。**

所以两边判据**刻意不同，各查各的**：

| | emotion-core `cpt_watch` | CPT `check_seal_coverage` |
|---|---|---|
| 数据源 | `~/logs/intraday-collect.log` | `intraday_seal_snapshot` + 日志（两边都要） |
| 干净日 | error 轮数 = 0 且 ok 轮数 ≥ 10 | 有落库轮次 且 error = 0 且 落库轮数 ≥ 10 |
| 当天算不算 | 算（日志是实时的） | **不算**（库里当天天然不完整） |

现在两边都读得出「0/5」：库里有数据的交易日只有 2026-10-22 一天，且当天只落了 3 轮，
连 `MIN_ROUNDS_PER_DAY=10` 都不到。

### 13.2 实现

`scripts/watch_period_check.py`：

- 常量 `SEAL_OBSERVE_DAYS = 5`、`MIN_ROUNDS_PER_DAY = 10`
- `check_seal_coverage()` —— 只用 `intraday_seal_snapshot` 的 `at` 分布 + 日志 error 计数
- `_log_errors_by_day()` —— 复用 `cpt.application.intraday_status.parse_log_tail`，
  按北京日期聚合 `outcome == "error"`
- **本项永远不判红**（`ok` 恒 True）。观察期没攒够是事实不是故障；判红会让真故障
  被一个长期红项淹掉。红只给「采集器挂了 / 序列落后 / 批次停了」这类有对策的事。

### 13.3 `check_service()` 被插断（这一轮最贵的教训）

用 `def _now() -> datetime:` 当锚点插 `_MARKET_TZ()`，而这个锚点**也出现在
`check_service` 之前** —— 新函数被塞进了 `check_service` 函数体里，后面的
`return result` 全变成死代码，函数返回 `None`：

```
TypeError: 'NoneType' object is not subscriptable
  problems = [p for c in checks for p in c["problems"]]
```

**Python 不报错、ruff 不报错，只有真跑巡检才看得见。** 我单跑了
`check_intraday()` 和 `check_seal_coverage()` 都是 dict，看起来全对 ——
崩的恰好是我没单独跑的那一个。

两处加固：

1. `collect()` 加 None 守卫：任何一项返回非 dict，替换成一个
   `{"name": "broken_<i>", "ok": False, ...}` 而不是让整轮巡检崩掉。
   **巡检崩了等于没有巡检** —— 比某一项判红严重得多。
2. 测试加 `TestBrokenCheckDoesNotKillTheRound`：`None` → 报 problem 不崩；
   并逐项跑一遍真函数断言每个都返回 dict。

规则已写进 `docs/known-traps.md` **#67**：插入代码前 `grep -n` 确认锚点命中数，
≥2 就换更长的锚点；改完跑一次真实路径。

### 13.4 给读方的两句硬话

`docs/intraday-seal-contract.md` §9 已更新（99 行 / 1 个交易日 / 3 轮），并新增两条：

1. **10-22 只落了 3 轮是因为落库 17:20 才上线**，上午 100 多轮只进了日志。
   这些轮次**无法回填** —— 日志只有汇总没有代码级明细，重放抓回来的是第二天数据。
2. **裸 SQL 读方必须自己加 `code NOT LIKE '\x01%'`**。storage 层三个读取函数都带了
   `_REAL_ROWS` 过滤，但绕过它自己写查询的人拿不到这层保护。同时写清了
   19 行假数据当时的实际后果：`SELECT max(at)` **只读到那 1 行假的，33 只真实票被完全遮蔽**。

### 13.5 验收

- `tests/test_watch_period_check.py` **30 passed**（原 24，新增 `TestSealCoverage` 4 条 + `TestBrokenCheckDoesNotKillTheRound` 2 条）
- `tests/test_intraday_seal_contract.py` **11 passed**（原 10，新增「裸 SQL 读方要过滤哨兵行」）
  - 反向验证过：改成检查 `"116" in text` 立刻红 —— 契约里留着被清理掉的旧基数，
    比没有基数更糟
- 两处反向注入全中：去掉「当天不算干净日」→ 1 failed；去掉 None 守卫 → 1 failed
- 实跑 `scripts/watch_period_check.py --skip-network --print`：**六项全绿**
  `✅ 服务 / ✅ 序列（911228/911214，985/958）/ ✅ 批次（7.5h 前，阈值 30h）/ ✅ 盘中采集 / ✅ 封板观察期（还没有完整落库的一天）`

## §14 R93（2026-10-23 盘前）：封板快照从 5 列扩到 9 列

### 14.1 起因：emotion-core 的 N5 清单

它要建常驻采集器（`emotion-intraday.service`，现在是「装了不 enable」），说现有 5 列不够，
要 4 列：`price / prev_close / chg_pct / name`。

**这四列的原料 CPT 一直在采，只是没存。** 实跑一轮就能看到采集记录里全都有：

```json
{"code":"000530","name":"冰山冷热","prev_close":8.63,"last":9.49,"high":9.49,...}
```

所以这不是新增采集能力，是**把已经在手里的东西存下来**。

### 14.2 两处换法（不给原始 4 列）

| 要的 | 给的 | 为什么 |
|---|---|---|
| `price` | `price` | 要。`sealed` 是个布尔，判不出「封住之后松没松」的**程度** |
| `prev_close` | `prev_close` | 要。算涨停价基准 + 交叉校验 CPT 的 `limit_price` |
| `chg_pct` | **`distance_pct`** | `chg_pct=(price-prev_close)/prev_close`，三个成分都在，读方自己除。存成列是同一事实的第二份拷贝（R76 踩过：`median` 算两遍导致口径打架）。而 `distance_pct=(limit_price-price)/limit_price*100` **推不出来** —— 用的是 CPT 的 Decimal 取整口径，读方自己算差一分钱 |
| `name` | **`is_st`** | `name` **会变**（`600xxx` 改名成 `ST某某`），同一条 `at` 上的历史行会自相矛盾。读方要 `name` 只为一个目的：判 ST。那就只存判定结果，判据留在 CPT（ADR 0003 §E2 只许一处） |

净结果：**5 + 4 = 9 列**，其中 `distance_pct` 和 `is_st` 是**判定结果**不是原始字段。

### 14.3 两件它另外问的事

1. **`bid1_lots` 单位是手** —— 确认。契约 §2 已写，R93 又在列名对照表里加了
   「emotion 侧阈值是股，读时必须 × 100」。
2. **「保证哨兵行永远不进生产表」** —— **做不到，也没这么说**。现在有三层：
   夹具关写 + 读取端 `_REAL_ROWS` 过滤 + 真库回滚测试。
   保证的是「**写进去了也读不到**」，不是「永远写不进去」。R91 那次是三层都没建的时候。

### 14.4 实施（盘前 40 分钟）

| 步骤 | 内容 |
|---|---|
| 1 | `scripts/migrations/2026-10-23_r93_seal_snapshot_extra_columns.sql`：`ADD COLUMN IF NOT EXISTS` 4 列，全可空，带 `COMMENT ON COLUMN`。已在生产执行 |
| 2 | `cpt/storage/intraday_seal_store.py`：抽出 `_SELECT_COLS` 常量（9 列一处定义），`_INSERT_SQL` 扩到 9 列，参数顺序同步 |
| 3 | `scripts/intraday_collectd.py::_persist()`：先从 `candidates` 建 `by_code` 索引（`sealed` 那条只带 code/name/distance_pct，**价格要回候选里取**）；封板与未封板两类行**都**填价格四件套 —— emotion 判「封住之后掉出涨停」要现价，不能只有封板那一轮才有 |
| 4 | 契约 §2 建表块改成 9 列 + 新增 §2.1「为什么是这 9 列」+ 列名对照表 |
| 5 | 守卫测试：`EXPECTED_COLUMNS` 5→9；`test_migration_creates_the_same_columns` 拆成「R90 建 5 列」+「R93 加 4 列」+「两份合起来恰好等于 9 列」；新增「契约必须写明单位是手」「契约必须解释两处换法」 |
| 6 | 真库测试 `test_r93_price_columns_round_trip` |

### 14.5 存量 99 行不回填

这 4 列对老行全是 NULL。**不回填** —— 它们当时确实没采，补出来的是推算值不是采集值。
这个决定写进了迁移脚本注释和契约 §2.1。

### 14.6 验收

- 生产 DDL 已执行，`information_schema` 核对 9 列顺序与类型全对
- 实跑一轮 `--once --ignore-session`，落库样本：
  `002249 sealed=True bid1=293674.49 limit=7.06 price=7.06 prev=6.42 dist=0.0 is_st=False`
- 全量 pytest **2378 passed / 5 skipped / 0 failed**
- 门禁全绿；ruff / mypy 通过
- 守卫测试先红后绿：扩列时 `test_contract_lists_exactly_the_five_columns` 与
  `test_insert_sql_writes_exactly_the_contract_columns` 立刻报
  `Left contains 4 more items, first extra item: 'price'` —— **守卫在工作**

### 14.7 一个踩到的坑

`test_r93_price_columns_round_trip` 第一版用 `load_session_rows()` 读回来验，
读到 0 行。原因：那个函数带 `_REAL_ROWS` 过滤，而测试用的正是 `\x01` 哨兵代码 ——
**过滤器把测试自己的行滤掉了**。改成直接 `SELECT` 绕开过滤器：
这条测试验的是「列能不能写进去」，不是「过滤器生不生效」。

---

## 15. R94（2026-10-25）：`_get_conn()` 的临时对象取连接，把 `track` 页面静默打哑

### 15.1 起因

一份第三方核查报告（`2026-10-25-CPT现状核实报告.md`）报 6 条问题。逐条核实后
**5 条成立、1 条根因诊断错**，但错的那条恰好是最要紧的那条 —— 它把症状归给
「RDS 故障转移后不重连」，实际是本地对象生命周期。

### 15.2 真根因

`cpt/adapters/a_share_local.py` 的 `_get_conn()` 是 per-instance 缓存，
而同一个类为修连接泄漏加了 `__del__ → close()`。于是：

```
AShareLocalClient()._get_conn()   ← 实例只在表达式里存活
                                  ← 表达式结束引用计数归零 → __del__ → 连接被关
```

一行判据：

```python
AShareLocalClient()._get_conn()          # status=1 (BAD)
c = AShareLocalClient(); c._get_conn()   # status=0
```

### 15.3 波及面比报告写的大一倍

报告说 6 处，AST 实扫是 **8 处**，而且漏掉了唯一一处生产代码：

| 位置 | 性质 | 症状 |
|---|---|---|
| `cpt/web/track_api.py:142` | **生产** | `GET /api/dashboard/track` 恒返 `{"available": false, "reason": "OperationalError: the connection is closed"}`；该模块 12 处 `_conn()` 共用 |
| `scripts/compare_chanlun_backends.py:131` | 脚本 | 第 132 行 `Traceback` |
| `scripts/verify_public_contracts.py:138` | 脚本 | 崩 + 顺带报出一条**假警** |
| 5 处 `tests/` | 测试 | 26 条红（18 ERROR + 8 FAILED） |

线上实证：

```json
{"available": false, "items": [], "reason": "OperationalError: the connection is closed", "recycle": []}
```

**优雅降级 ⇒ 不报错、不告警、不重启，靠人肉点页面才发现。**

### 15.4 修法（方案 A）

不给调用点打补丁，而是补一个**不经过 client 生命周期**的入口：

```python
def open_conn() -> Any:                       # cpt/adapters/a_share_local.py
    return psycopg.connect(**connection_kwargs())
```

- `_get_conn()` 内部改成 `self._conn = open_conn()`（DRY）
- 8 处「临时对象取连接」全部改成 `open_conn()`，AST 复扫剩余 **0 处**
- `__all__` 加 `open_conn`

**为什么不选「只改调用点让 client 活到用完」**：缺陷在接口不在调用方 ——
`_get_conn()` 既交出连接又保留所有权，这个陷阱会等着下一个临时对象。

### 15.5 顺带修出的第二件事：契约 5 的判据本身是错的

连接修好后 `verify_public_contracts.py` 不再报 `OperationalError`，露出真实数据差：

```
交叉 腾讯 hfq vs 本地因子表
  ⚠️ 两侧后复权价差 8.37%：腾讯 9316.39 vs 本地 8536.19
❌ 1 条契约不再成立 —— 该改适配器
```

深挖：原始价两侧**完全一致**（1329.00 / 1335.00），差异全在复权因子上 ——
腾讯隐含因子 ≈ 6.98，本地 `asel.ref_adjust_factor` = 6.39414671（三天不变，正常）。

第一版结论是「长存的常数级锚点差，对策略无影响」。**这句是错的**，R95 当日自己推翻：

| 比较（600519，40 个交易日） | 结果 |
|---|---|
| 腾讯 `qfq` vs 腾讯 `bfq` 价格 | 逐日**完全相同**，收益差 `0.00e+00` |
| 腾讯 `hfq` vs 腾讯 `bfq` 收益 | **39/39 天全部不一致**，最大差 `5.03e-03` |
| 腾讯 `hfq` 隐含因子 vs 本地因子 | 腾讯逐日漂 **6.978~7.088**（±0.2%），本地**恒定 6.39414671** |

跨股实测「大涨大跌日 `hfq_ret/bfq_ret` 中位」（n≈24~29）：600519=`0.801`、000001=`0.768`、
600036=`0.721`、002249=`0.745` —— 每只票各自的常数，**不是全局常数**。

⇒ 差 8.4% 不是「锚点不同」这么温和：**腾讯 `hfq` 这条序列与它自家的 `qfq`/`bfq` 都对不上**。
拿它当标尺去判本地因子表是伪判据，而且**如果 CPT 用 `hfq/raw` 反推因子，会得到逐日漂移的因子**。

本地侧反而是自洽的：`tencent bfq` == `local daily_bar.close` **最大差 0.0**；
`local_ret` == `bfq_ret` 完全相同；`asel.ref_adjust_factor` 的 `carry_forward` 口径
（只在除权日变，其余日子沿用）意味着**窗口内恒定为设计如此**。

新判据（`c5_hfq_matches_local_factor` 重写，三层）：

1. **硬**：腾讯 `bfq`/`qfq` close == 本地 `daily_bar.close` 逐日零误差（容差 0.005）。
2. **硬**：本地 `hfq_factor` 在无除权窗口内必须恒定（窗口内变了 ⇒ carry_forward 口径失效）。
3. **条件**：只有腾讯 `hfq` 自身与 `bfq` 自洽时才真的拿它交叉校验本地因子；否则只记录比值。

实跑：`✅ 原始价 60/60 天零误差、本地因子 60 天恒定 6.394147；⚠️ 腾讯 hfq 与自家的收益对不上（最大差 7.02e-03），因此不拿它当标尺`。

**遗留**：`asel.ref_adjust_factor` 3,484,374 行里有 **28,569 行（0.8%）** 的 `source` 是
`tx:fqkline` —— 那批因子由腾讯 `hfq/raw` 反推，按上面的实测口径值得复查。
4,237 个代码的因子值在历史上有变化（= 有除权事件，正常）。见 `docs/known-traps.md` #69。

- **不在 CI**（`check_gate_coverage.py` 明写「人工按需跑的对外契约抽查」）⇒ 不是门禁红项。

### 15.6 P0 门禁尾巴

初始红 6 道，全部转绿：

| 门禁 | 红 | 修法 |
|---|---|---|
| `check_all_claims` | 27 处 | `cpt/domain/X.py` → `emotion_cpt.structure.X`（12 份现行文档 50 处；排除 `docs/archive/`）；`deploy/*/README.md` 两个跨仓脚本路径加 `Emotion-CPT/` 前缀 |
| `check_doc_counts` | 3 行 | `docs/architecture.md` §2.1：adapters 7,644→**7,712**、domain 18/3,581→**1/63**、web 5,766→**5,777** |
| `check_line_refs` | 1 处 | 随那处 `config.py` 引用改写消失（该文件本仓不存在，无法也不该校验） |
| `check_doc_drift` | 3 处 | 同上，随 §2.1 对齐 |
| `check_test_mutation --selftest` | 1 崩 | 负控制 `rel` 从已删的 `cpt/domain/config.py` 换成 `cpt/backtest/stats.py`（`_MIN_ABS_T`），并写明「负控制要的是真的无关，不是随便换个文件」 |
| `check_cold_environment` | 1 failed | 随连接修复消失 |

**domain 那一行不是简单改数字**：18 文件/3,581 行 → 1 文件/63 行，语义已变
（判据本体迁往 Emotion-CPT），所以整行改写并保留全部历史段落。

### 15.7 顺带清掉的 ruff 债

`ruff check` 6 个错，**全部是既有的**（`git show HEAD:...` 验证过），一并修：

- `cpt/adapters/a_share_local.py` UP037（`-> "AShareLocalClient"` 去引号）
- `scripts/factor_carry_forward.py` E501
- `tests/test_dashboard_run_store_prune.py:188` / `tests/test_signal_overview_wiring.py:223`
  **F821 `pathlib` 未定义** —— `from __future__ import annotations` 让它不崩，
  但 `typing.get_type_hints` 会炸；改成已导入的 `Path`
- 两个 `I001` 自动修

### 15.8 验收

- 全量 pytest：26 条红 → **0 failed**
- 12 道门禁全绿：`check_all_claims` / `check_doc_counts` / `check_line_refs` / `check_doc_drift` /
  `check_value_plumbing` / `check_gate_coverage`（16 道在 CI、16 条夹具、磁盘 17 个、豁免 4）/
  `check_sql_layering`（65 文件）/ `check_storage_failure_semantics` / `check_ci_workflow` /
  `check_bigserial_sequence` / `check_editable_import` / `check_derived_gap`
- `ruff check .` **All checks passed**
- `build_dashboard_bundle.py --check` 同步
- 生产实证：`cpt.web.track_api._conn()` → `closed=False`、`SELECT 1` → `(1,)`

### 15.9 仍然开放的

- `ruff format --check` **19 个文件待重排**、`mypy cpt scripts` **21 个错** —— 都是既有的，
  本轮未动（与 R94 无关，另案处理）
- `verify_public_contracts.py` 契约 5 的判据要不要从「绝对价」改成「收益率序列」—— 待拍板
