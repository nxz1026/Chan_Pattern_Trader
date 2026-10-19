# rebuild 计划评审（chanlun-pro-replica → CPT 融合）

> 状态：**待拍板**。本文只做事实核实与依赖分析，不下结论性改动。
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
| native | `cpt/domain/bi.py:183` | `int(end.merged_index) - int(start.merged_index) + 1 >= min_bi_len` | **merged 跨度** |
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

### 5.1 A1 排在第一位，理由不成立

审计写 A1 是「`first_buy.py` 内部口径」。实际：

- `cpt/domain/first_buy.py` 是**从 czsc `crates/czsc-signals/src/utils/cxt.rs` 逐行移植**的
  （`docs/reference-audit.md` §5 明载来源与许可证）。换背驰判据 = 主动放弃该模块的对照基础。
- `divergence_compare` 目前是**死字段**：全仓仅 `cpt/domain/config.py:151`（声明）
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
| P0-d | `cpt/domain/bi.py:104` 的 `min_bi_len: int \| None = None` 改为必填或对 `None` 显式告警 | `bi.py` + 调用点 |

> P0-d 是审计 §9「CPT 建议 1」，**至今未做**。现行默认 `None` 会放出
> 326/356/352 笔（§4.2），正是审计 §7.4 用来否决 chanlun-pro 的那种形状。

### Phase 1 —— 先能测：C2 回测层（提前）

接**现有** `first_buy` 信号，不依赖 A1/A2。产出后可量化判定后续每一项改动。

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

## 8. 待拍板（决策记录）

| # | 问题 | 选项 | 决定 |
|---|---|---|---|
| 1 | 是否先做 Phase 0？ | (a) 先做　(b) 直接进 A1 | 待定 |
| 2 | native 门槛口径 | (a) 老笔 `G=6`（108 课）　(b) 新笔 `G=7` | 待定 |
| 3 | `build_bis` 的 `min_bi_len=None` | (a) 改必填　(b) 保留但显式告警 | 待定 |
| 4 | 施工顺序 | (a) 本文 §6　(b) 审计 §11.8 原顺序 | 待定 |
| 5 | `divergence_compare` 死字段 | (a) A1 接线　(b) 删除字段 | 待定 |

> 决定后**就地写入上表并删除本行**，不新建文件（文档纪律）。
