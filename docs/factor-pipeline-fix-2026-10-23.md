# 因子链路修复记录（2026-10-23）

> 起因：合并勘察时发现 **CPT 看板的 A 股结构位全部退化为「不复权」**。
> 三个独立缺陷，逐个实测确认、修掉、验证。

---

## 0. 症状

```
INFO cpt.adapters.a_share_local 600519 2026-04-26~2026-10-23：
    尾部 2026-10-23 缺复权因子，整段退到不复权（上次有因子 2026-10-22）
```

⇒ `price_ratio = adj ÷ raw` 取不出 ⇒ **结构位（买/卖点）退化为百分比兜底**，
看板上看到的不是真实结构位。

真机实测：`price_ratio = None`，`basis` 从 `cpt_structure` 变成 `pct_fallback`。

---

## 1. 缺陷① · 时序：全市场重算跑在当天 K 线入库之前

| 作业 | 时刻（UTC） | 北京 | 说明 |
|---|---|---|---|
| `factor-recompute-daily.sh` | **02:20** | 10:20 | 全市场按公司行动重算因子 |
| `emotion-core-daily.timer` | 09:20（+120s 随机延迟） | 17:20 | 落当天 `daily_bar`，实测跑 **11.5 分钟** |
| ⇒ 当天 K 线可用 | ~**09:34** | 17:34 | |

重算的 `target` 取自 `public.daily_bar` 的 `max(date)`
（`scripts/factor_recompute.py::coverage_target` — 「能算到哪一天由本地 K 线决定」）。
⇒ **02:20 跑的时候当天 K 线还不存在 ⇒ target 永远停在前一天。**

真机验证：
- 暂存表 `asel.ref_adjust_factor_v2`：**10-23 有 0 行**，10-22 有 5,209 行
- 生产表 `asel.ref_adjust_factor`：**10-23 只有 1 行**（`000002`，`source=tx:fqkline`，按需拉取）
- ⇒ 其余 5,210 只票的当天因子**从来没被算出来过**

**修法**：
- 全市场重算挪到 **09:50 UTC**（daily 之后）
- 新增「因子沿用」作业（见 §3），**09:45 / 11:30** 跑

---

## 2. 缺陷② · 连接被掐后不重连 ⇒ 后半程静默失败

日志首条错误就是铁证：

```
000002 失败：AdminShutdown: terminating connection due to administrator command
000032 失败：OperationalError: the connection is closed
000066 失败：OperationalError: the connection is closed     ← 之后全是这个
```

- `AShareLocalClient._conn` 是**缓存**的 ⇒ 脚本握**一条**连接跑满 2 小时
- RDS 一重启/故障转移 ⇒ 连接被终止 ⇒ 之后每只票都以同样方式失败
- 10-23 那轮：**已覆盖 5,211 只 / 失败 5,227 只**（一半）

**修法**（`scripts/factor_recompute.py`）：
- 新增 `_is_conn_error(exc)`：按类名（`OperationalError` / `InterfaceError` / `AdminShutdown` …）
  + 特征串（`the connection is closed` / `terminating connection` / …）判定
- 新增 `_reset_conn(client)`：关掉并清空缓存连接
- per-code 异常处理里：**连接级错误 → 换连接重试该票一次**

判据函数的单测（用假异常）：

```
✓ AdminShutdown      连接级=True
✓ OperationalError   连接级=True
✓ InterfaceError     连接级=True
✓ OperationalError   连接级=True   （特征串兜底）
✓ ValueError         连接级=False  （普通错误不该重连）
✓ TimeoutError       连接级=False
```

---

## 3. 缺陷③ · 断了还 `rc=0` ⇒ 外部完全看不出来

- `stopped_reason` 只在 `source.fatal_errors`（**额度/通道类**）时置位
- 连接被掐断**不属于**那一类 ⇒ 掉进 `except Exception` 被当成单票失败吞掉
  ⇒ 循环照跑完 ⇒ **最后 `return 0`** ⇒ cron 日志记成「结束 rc=0」
- 顺带：`promote_recompute_factors(conn, ...)` 用的是**同一条死连接**
  ⇒ **暂存表算好的新数据永远搬不进生产表**（这是 10-23 因子缺失的第二层原因）

**修法**：
- 连接级失败计数 > 0 ⇒ 复用已有的「未完成」语义 **rc=3**
- `promote` 抛异常 ⇒ 同样 **rc=3**（不再静默）

---

## 4. 新增：因子沿用（`scripts/factor_carry_forward.py`）

### 为什么「沿用」是**精确的**，不是近似

因子是**公司行动的台阶函数**：无除权事件的日期，因子值与前一日**逐位相同**。
真机证据 —— 600519 的因子连续多日一模一样：

```
2026-10-22 | 6.39414671 | eastmoney:events steps=13
2026-10-21 | 6.39414671 | eastmoney:events steps=13
2026-10-20 | 6.39414671 | eastmoney:events steps=13
```

⇒ 把前一日的值沿用到当日，与重算的结果**逐位相同**（同一套事件源、同一套算法）。
全市场重算做的其实只是「确认今天没有新事件」。

### 但有事件的当天，因子会变

```
000002 | 2026-10-23 | 363.05786350 | tx:fqkline
000002 | 2026-10-22 | 367.84259819 | eastmoney:events steps=13
```

⇒ 沿用对这类票是**错的**（少了台阶）⇒ 由**当天的全市场重算**纠正。
沿用行的 `source` 标成 `carry_forward:...`，**事后可查「这格当时用的是沿用值」**。

### 两条安全阀

1. **只补「生产表当天没有」的票** —— 生产表当天已有的行来自按需拉取（腾讯比值），
   那是**实测更准**的值；沿用绝不能覆盖它（否则 promote 会把对的换成错的）。
   实测：10-23 那天生产表已有 `000002`（按需拉的）⇒ 沿用**跳过它**。
2. **写暂存表，不直接写生产表** —— 搬运一律走既有的 `promote_recompute_factors()`
   （只写与生产表相差 > 1e-8 的行）。

### 顺带修的一个细节

暂存表的 upsert 原来**不更新 `source`**：

```sql
ON CONFLICT (code, trade_date) DO UPDATE
  SET hfq_factor=EXCLUDED.hfq_factor, basis=EXCLUDED.basis, computed_at=now()
```

⇒ 重算纠正了值，那一格却还挂着 `carry_forward:` 标记 ⇒ **事后查不出它准不准**。
已补 `source=EXCLUDED.source`。

---

## 5. 实测效果（2026-10-23）

| | 修复前 | 修复后 |
|---|---|---|
| 生产表当天行数 | **1** / 5,211 | **5,211** ✅ |
| 暂存表当天行数 | 0 | 5,210 ✅ |
| `600519` 的 `price_ratio` | `None` | **6.3941** ✅ |
| `600519` 的 `status` | — | **`structure_ready`** ✅ |
| 日志「缺复权因子」 | 每次请求都报 | **0 次** ✅ |
| emotion 侧 `levels.basis` | `pct_fallback` | **`cpt_structure`** ✅ |

emotion-core 侧同步恢复（`/api/cpt/points`）：

```
basis          'cpt_structure'
buy_reference  1245.74
stop_price     1183.45
target_price   1269.62
```

---

## 6. 改动清单

| 文件 | 改动 |
|---|---|
| `scripts/factor_recompute.py` | `_is_conn_error()` / `_reset_conn()`；连接级错误换连接重试；连接级失败与 promote 失败 ⇒ rc=3 |
| `scripts/factor_carry_forward.py` | **新增**：沿用 + promote（两条安全阀 + 幂等） |
| `deploy/cron/factor-carry-forward.sh` | **新增**：沿用作业的 cron 包装（带 flock 与 rc 留痕） |
| `deploy/cron/crontab` | 重算 02:20 → **09:50**；新增沿用 **09:45 / 11:30** |
| `cpt/adapters/a_share_factor.py` | 暂存表 upsert 补 `source=EXCLUDED.source` |

---

## 7. 遗留与提醒

- **全市场重算仍要 2 小时**（5,206 只 × ~1.45s，东财无额度）。
  沿用把「当天因子可用」从「永远拿不到」压到「秒级」，但**事件票的精确值仍要等重算**
  （当天 09:50 起跑，约 11:50 UTC 完成）。
- 沿用与重算**都写暂存表**；生产表只由 promote 写 —— 这条边界保持不变。
- 若 `daily` 跑得晚，09:45 那轮沿用会**空转**（target 没前进），
  11:30 那轮兜底。脚本会如实打出「没有可沿用的票」，不会假装成功。

---

## 8. 全市场重算的核验结果（2026-10-23 17:48 完成）

| 核验项 | 结果 |
|---|---|
| ① 退出码 | **`rc=0`**（不是 3）—— 无连接级失败、无未完成 |
| ② promote | **成功**：`扫描 3456056 行，改写 44561 行` |
| ③ 覆盖日期 | **2026-10-22 → 2026-10-23** —— target 追上了 |
| ④ 生产表当天行数 | **5,211**（修复前 1） |
| ⑤ 生产表 10-23 值 vs 暂存（重算）值 | **5,211 / 5,211 逐位一致** ⇒ 当天因子全市场正确 |
| ⑥ source 分布 | 5,183 `carry_forward:` · 27 `eastmoney:` · 1 `tx:fqkline` |
| ⑦ 前缀叠加 | **0 行** ✅ |

### 关于那 5,183 行还挂着 `carry_forward:`

**这是诚实的来源标记，不是错标。** 时间戳可证：它们的 `fetched_at = 15:46:28`
⇒ 确实是 15:46 那次沿用写进去的。值也对 —— 与重算结果逐位相同。

其中 **41 只当天确实除权了**（10-22 因子 3.683104 → 10-23 因子 3.701526），
重算算出的值与沿用值恰好相同 ⇒ **沿用在这 41 只上也是对的**。
另 5,140 只无事件，值与前一日相同 ⇒ 沿用精确。

### 附带清掉的历史尾巴

生产表 **10-22 也被本轮 promote 全量纠正**成重算口径（5,209 / 5,209 一致，
source 全为 `eastmoney:events`）⇒ R64 迁移遗留的「生产表停在按需拉取的旧比值」问题一并清掉。

### 一个防御（本次核验想到的边界）

已加：沿用写入前**先剥掉已有的 `carry_forward:` 前缀**。
正常情况不会叠加（重算每天把暂存的 source 改回 `eastmoney:`），
**但那恰恰是重算失败的那天** —— 不能指望它。
