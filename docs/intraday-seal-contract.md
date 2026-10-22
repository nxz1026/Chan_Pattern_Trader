# 盘中封板快照契约（CPT 写 → emotion-core 只读）

> **跨仓库契约。** 一份文档管两个项目，因此它不受任一方的门禁保护 —— 所以真在
> 起作用的不是这份文档，是**两侧各自的测试**：CPT 侧
> `tests/test_intraday_seal_store.py` 钉写入形状与三条读表口径；emotion-core 侧
> `tests/unit/test_cpt_intraday.py` 钉列清单与 `at = max(at)`。
>
> ⚠️ **改这张表时必须同步三处**：本文件 → `cpt/storage/intraday_seal_store.py::
> _INSERT_SQL` → emotion-core 的 `_COLUMNS` / `_SELECT_SQL`。漏任何一处，
> 读方会在没有任何报错的情况下看到 `None` 列。

## 1. 方向与边界

```
CPT (scripts/intraday_collectd.py)  ──写──▶  public.intraday_seal_snapshot
                                                    ──只读──▶  emotion-core
```

- **单向**：CPT 写，emotion-core 只读。与 `docs/shared-tables-contract.md` 那批
  7 张表方向**相反**（那批 emotion-core 写、CPT 读），所以单独成文。
- **判据只在 CPT 一处**：哪只票封在板上由
  `cpt/domain/intraday_rules.py::seal_strength` 判。emotion-core **不要**再拿
  `limit_price` / `is_at_limit` 判一遍 —— ADR 0003 §F.2 北交所 ST 那 40 条不一致
  就是"两套口径"的实测代价。
- **不存原始五档**。存的是每轮每只票的**结论**（封没封 / 封单多厚 / 当时用的涨停价）。
  原始五档只写 CPT 本地日志 `~/logs/intraday-collect.log`（只有汇总，不含代码）。

## 2. 表结构（生产已建）

```sql
public.intraday_seal_snapshot (
    at          timestamptz      NOT NULL,  -- 这一轮采集时刻（UTC 存储、UTC 输出）
    code        text             NOT NULL,  -- 6 位纯代码，如 '600293'
    sealed      boolean          NOT NULL,  -- 这一轮是否封在板上
    bid1_lots   double precision,           -- 封单量（单位：手，1 手 = 100 股）；判不了为 NULL
    limit_price double precision,           -- 本轮用的涨停价（存下来自证口径）
    PRIMARY KEY (at, code)

CREATE INDEX idx_intraday_seal_snapshot_code_at ON public.intraday_seal_snapshot (code, at);
CREATE INDEX idx_intraday_seal_snapshot_at ON public.intraday_seal_snapshot (at DESC);
```

迁移脚本：`scripts/migrations/2026-10-22_r90_intraday_seal_snapshot.sql`。

**为什么只有 5 列**：每一列都是采集端已经算出来、读方拿不走也推不出来的量。
`sealed` 推不出来（要涨停价 + 板块规则）；`limit_price` 推不出来（要昨收 + 板块阈值，
且它是"当时用的是哪个阈值"的自证）；`bid1_lots` 是十档原始数据。OHLC / 涨跌幅 / 名称
**不存** —— 那是 `daily_bar` 的职责，重复存会造出第二套口径。

## 3. 三条读表口径（错了会造出并不存在的状态）

1. **没行 = 判不了**，不是「没封板」。候选之外、停牌、缺档位的票不会有行。
2. **`sealed=False`** = 「采到了，这一轮没封在板上」。这是**开板的证据**，
   与「这一轮没采到这只票」必须分开。
3. **按 `at` 分组取最新一轮**，不要按 `code` 单点查最新。
   跨轮混算会把"上一轮封着、这一轮开板"读成两只不同的票。

## 4. 推荐查询（emotion-core 照抄即可）

取某一天的全部行，在内存里按 code 分组：

```sql
SELECT at, code, sealed, bid1_lots, limit_price
  FROM public.intraday_seal_snapshot
 WHERE at >= %s AND at < %s      -- 半开区间；注意换成北京日期边界
 ORDER BY at, code
```

只取最新一轮：

```sql
SELECT at, code, sealed, bid1_lots, limit_price
  FROM public.intraday_seal_snapshot
 WHERE at = (SELECT max(at) FROM public.intraday_seal_snapshot)
 ORDER BY code
```

CPT 侧的参考实现：`cpt/application/intraday_review.py`（`by_code` / `track_summary` /
`session_summary`），命令行 `scripts/intraday_review.py --date YYYY-MM-DD --codes`。
读方自己实现也不冲突，**但「开板」「封住」的定义必须与这两条对齐**：

- **开过板** = 某个 `sealed=True` **之后**出现过 `False`。不是「没封着」，
  也不是「出现过 False」（封板之前的 False 是常态）。
- **最后封住** = 当天**最后一轮**是 `True`。不是「出现过 True」。
- **封住率** = 最后一轮还封着 / 当天冲到过板附近。空样本给 `None`，不填 0。

## 5. 时间与时区

- `at` 以 `timestamptz` 存，读回来是带 `+00:00` 的 datetime。
- **按北京日期切日**：`at` 的北京时间（UTC+8）落在哪一天，就算哪一天的数据。
  按 UTC 切会差一天 —— 09:30 北京 = 01:30 UTC，同日；15:00 北京 = 07:00 UTC，同日；
  但 23:00 北京 = 15:00 UTC 同日。真正的分界在 UTC 16:00（= 北京次日 00:00）。
  CPT 的 `session_of()` 就是按北京日期切的。
- 盘中时段：北京 09:30–11:30、13:00–15:00，每 **2 分钟**一轮。

## 6. 谁保证有数据（三处，缺一不可）

| 环节 | 机制 | 位置 |
|---|---|---|
| 定时触发 | `cpt-intraday-collect.timer`，`OnCalendar=Mon..Fri *-*-* 09..11,13..14:00/2:00 Asia/Shanghai` | `deploy/systemd/` |
| 交易日判定 | `cpt.adapters.a_share_local.is_trade_day`（读 `trade_calendar`，**不读 `daily_bar`**） | `scripts/intraday_collectd.py::trading_day` |
| 故障告警 | CPT 自己的巡检每 2 小时查一次，红了发飞书 | `scripts/watch_period_check.py::check_intraday` |

⚠️ **`OnCalendar` 的 `/STEP` 是小时步长，不是分钟**。`09:30/2:00` 会被解成
"每 2 小时的第 0 分"（一天跑 3 次），必须写成"小时写范围、分钟写步长"。
CPT 踩过一次，装完当天看着 timer 是 `active`、`outcome` 也写 `skipped` 而不是
`error`，**一天没真采过**。

## 7. 失败语义（读方要能区分）

| 情况 | 表里表现 | 读方该怎么做 |
|---|---|---|
| 采集器没起 / timer 没装 | 当天无行 | 报"没有数据"，不要报"今天没封板" |
| 盘中在跑但这一轮只有部分票 | 只覆盖候选（涨幅榜顶部 40 只左右） | 不要当成"全市场封板清单" |
| 单轮 HTTP 失败 | **这一轮就是没有行**（不写占位行） | 轮次缺失 ≠ 开板 |
| 写库失败 | 行不落库，但 `outcome` 仍是 `ok`（行情采到了） | 只能从"轮次空了"间接发现 |
| 停牌 / 缺档位 | 该票 `bid1_lots` 为 `NULL` | `sealed=False` 且 `bid1_lots` 为 NULL = 判不了 |

**单轮失败不告警，连续失败才告警**（`intraday_collectd` 默认 `--max-failures 3`）。
读方不要因为"某几轮没有行"就判采集器坏了。

## 8. 已知限制（2026-10-22 实测）

- **覆盖范围是候选，不是全市场**：`fetch_limit_rank` 取新浪涨幅榜前 100，按
  `_CANDIDATE_MARGIN_PCT=0.5` 筛到"距涨停 0.5 个点以内"，最多 40 只。
  涨幅榜之外的股票即使涨停也不会出现（实测当日榜单 100 行已覆盖所有涨停）。
- **`bid1_lots` 是买一档挂单手数**，涨停时它等于封单量；**非涨停时它是普通买一挂单**，
  不代表封单。读方判"封单多厚"必须先判 `sealed`。
- **北交所**：`board_of` 已支持 `920/8/4` 号段（30%），但 `daily_bar` 目前没有
  北交所数据，候选里出现北交所票的概率极低。这条未在真实行情上验证过。
- **重放不可行**：接口是实时快照，收盘后无法补采。缺的轮次就是永久缺。

## 9. 当前数据状况（2026-10-22 收盘后实测）

写这份契约时表里**只有 116 行、1 个交易日**（2026-10-22 北京时间的盘中段）。读方在
判"有没有数据"时必须先看这里，不要拿当天的空表当"采集器坏了"：

| 项 | 实测值 |
|---|---|
| `intraday_seal_snapshot` 行数 | **116** |
| 覆盖的交易日（北京日） | **1**（2026-10-22） |
| 时间跨度（UTC） | 2026-10-22 09:20:36 → 14:28:10（= 北京 17:20 → 22:28） |
| `daily_bar` / `derived_bar` | 3,451,091 / 3,450,870（差 221 = 221 只票的上市首日，正常） |

注意 `at` **是 UTC 存的**：北京 17:20 的一轮，库里是 `09:20Z`。照 `.date()` 直接取值
会拿到 **10-22 UTC**，而按北京日它是 **10-22** —— 播到这里恰好同一天；但一旦某轮落在
北京 00:00–08:00 之间（理论上没有，因为只在盘中采），两边就会差一天。**永远按北京日切。**

## 10. 版本历史

| 日期 | 变更 |
|---|---|
| 2026-10-22 | 建表；采集器开始落库（17:20 之后口径）。当日不算完整观察日 |
