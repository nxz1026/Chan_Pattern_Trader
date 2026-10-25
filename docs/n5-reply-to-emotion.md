# 回复 emotion-core N5 清单（CPT → emotion-core）

> 日期：2026-10-23 盘前
> CPT commit：`2c273cecd`
> 契约文档：`/home/ubuntu/DSH/Chan_Pattern_Trader/docs/intraday-seal-contract.md`

## 一句话结论

**要的 4 列，CPT 给了 3 列 + 1 个布尔。`chg_pct` 和 `name` 被换掉了，理由在下面第 3 节。**
数据库已改完，明天开盘就在写 9 列。

---

## 1. 先说一个事实：这四列的原料 CPT 一直在采

N5 说「本仓接常驻采集器还缺 4 列」。准确说是**缺存，不缺采**。实跑一轮抓到的原始记录：

```json
{"code":"000530","name":"冰山冷热","prev_close":8.63,"last":9.49,
 "high":9.49,"low":8.84,"chg_pct":9.965,"bid1_vol":4679804.0,...}
```

`price`=`last`、`prev_close`、`chg_pct` 可现算、`name` 全在。所以这次不是新增采集能力，
是**把已经在手里的东西存下来**。

---

## 2. 最终表结构（9 列）

```sql
public.intraday_seal_snapshot (
    at           timestamptz      NOT NULL,  -- 这一轮采集时刻（UTC 存储、UTC 输出）
    code         text             NOT NULL,  -- 6 位纯代码
    sealed       boolean          NOT NULL,  -- 这一轮是否封在板上
    bid1_lots    double precision,           -- 买一挂单量（手，1 手 = 100 股）；判不了 NULL
    limit_price  double precision,           -- 本轮用的涨停价；判不了 NULL
    price        double precision,           -- 这一轮现价；判不了 NULL          ┐
    prev_close   double precision,           -- 昨收；判不了 NULL                │ R93
    distance_pct double precision,           -- 距板百分比；负数=已在板上         │ 新增
    is_st        boolean,                    -- ST 判定结果；NULL=没判           ┘
    PRIMARY KEY (at, code)
);
```

---

## 3. 两处换法（重点看这节）

### 3.1 `chg_pct` → `distance_pct`

**不给 `chg_pct` 的理由**：

```
chg_pct = (price - prev_close) / prev_close
```

三个成分你都会有。存成列就是**同一事实的第二份拷贝**，两份迟早不一致 ——
CPT 在 R76 已经踩过一次：`median` 在调用方和被调方各算一遍，结果口径打架。

**改给 `distance_pct` 的理由**：

```
distance_pct = (limit_price - price) / limit_price * 100
```

这个你**推不出来**。`limit_price` 用的是 CPT 的 `Decimal` + `ROUND_HALF_UP` 到分口径
（浮点 `10.0 * 1.1 = 11.000000000000002`），你自己除会差一分钱。

语义：

| 值 | 含义 |
|---|---|
| `0.0` | 现价 = 涨停价，封在板上 |
| 负数（如 `-1.23`） | 已在板上，涨超了涨停价 1.23%（不该发生，说明口径有偏差，值得查） |
| 正数（如 `0.35`） | 距板还差 0.35% |
| `NULL` | 算不出（缺价或缺涨停价） |

**注意单位是百分数**（`0.35` = 0.35%），不是小数。

### 3.2 `name` → `is_st`

**不给 `name` 的理由**：

`name` 会变。`600xxx` 改名成 `ST某某` 之后，同一条 `at` 上的历史行就自相矛盾 ——
那行记录的是「当时叫什么」，读方却会当成「现在叫什么」。

**改给 `is_st` 的理由**：

N5 原文说得很清楚：「判 ST 必需（ST 板比 5%），只有 code 判不出」。那就只存判定结果。

判据留在 CPT（`emotion_cpt.structure.intraday_rules::is_st_name`），emotion 不再自己解析字符串 ——
这符合 ADR 0003 §E2「判据只许一处」。你们已经吃过一次这个亏：`board_pct_limit`
的 ST 判定顺序放错，北交所 ST 落到 20%，算出偏低涨停价，「已封板」被误判成「未到板」。

---

## 4. 你另外问的两件事

### 4.1 `bid1_lots` 单位是手

**确认。** 契约 §2 早就写了「单位：手，1 手 = 100 股」。R93 又加了一张列名对照表，
专门提醒「emotion 侧阈值是股，读时必须 × 100」。

你们已经按 ×100 处理了，**保持这个做法**。

### 4.2 「保证哨兵行永远不进生产表」

**这句保证做不到，我也没这么说。** 说清楚现在到底有什么：

| 层 | 做法 | 保证的是 |
|---|---|---|
| 夹具 | `_no_real_database` autouse 关掉 `DB_WRITE_ENABLED` | 产品测试不会写 |
| 读取端 | `_REAL_ROWS` 过滤 `code NOT LIKE '\x01%'` | **写进去了也读不到** |
| 真库测试 | 哨兵行走事务 + 必回滚 | 过滤器真的生效 |

**保证的是「即使写进去也读不到」，不是「永远写不进去」。** 你们说已在读侧过滤，
那就三比零。

---

## 5. 存量 99 行怎么办

这 4 列对 10-22 那 99 行**全是 NULL**，**不回填**。

它们当时确实没采，补出来的是推算值不是采集值。你们读历史数据时：

- `10-22` 那批：`price` / `prev_close` / `distance_pct` / `is_st` 全是 NULL
- `10-23` 起：正常有值

**判据里遇到 NULL 要当「不知道」，不能当「False」或「0」。**

---

## 6. 你们要改的代码

### 6.1 `_COLUMNS` / `_SELECT_SQL`

`src/emotion_core/data/providers/cpt_intraday.py`：

```python
_COLUMNS: Final[tuple[str, ...]] = (
    "at", "code", "sealed", "bid1_lots", "limit_price",
    "price", "prev_close", "distance_pct", "is_st",
)
```

### 6.2 `seal_weaken` 现在可以真判了

N5 原文：「没有就只能判『过去了』判不了『松了』」。现在：

```python
def seal_weaken(record: dict, seal_bid1_vol: float) -> bool | None:
    """封住之后有没有松。

    None = 判不了（缺 price 或缺 previous 快照）。
    """
    price = record.get("price")
    limit = record.get("limit_price")
    if price is None or limit is None:
        return None
    if price < limit - 1e-9:      # 掉出涨停价 = 松了
        return True
    lots = record.get("bid1_lots")
    if lots is None:
        return None
    return lots * SHARES_PER_LOT < seal_bid1_vol * SEAL_WEAKEN_RATIO
```

`distance_pct > 0` 也能当判据，但**直接用 `price` 和 `limit_price` 更稳** ——
`distance_pct` 是 CPT 算的，你自己比一遍不依赖它。

### 6.3 ST 判据

```python
if record.get("is_st") is True:
    pct_limit = 5.0
```

`is_st` 为 `None`（老数据）时**不要当 False**，要么跳过，要么按 code + 自己的 name 兜底并标记。

---

## 7. 时间线

| 时间 | 状态 |
|---|---|
| 10-22 17:20 | R90 落库上线，5 列，当天落 3 轮 |
| 10-22 晚 | 清理 19 行测试假数据（117 → 99 行） |
| 10-23 00:30 | **R93 加 4 列完成**，实跑一轮验证 |
| 10-23 09:30 | 开盘，timer 每 2 分钟一轮，**从这一轮起写 9 列** |
| 10-29 前后 | 观察期满 5 个交易日 |

---

## 8. 你们那边的两件事

**① 领先远程 7 个提交没推**

```
$ git log --oneline origin/main..HEAD
475c91c docs(adr): 0003 §H.4 按 CPT 封板契约定稿
11d3fc4 fix(intraday): provider 按 CPT 封板契约改写——三态判定 + 手→股换算
391fbee refactor(intraday): provider 改读 CPT 实际交付表 intraday_seal_snapshot
06d525a feat(lab): CPT 观察期可观测化
c3cc2e7 docs(adr): 0003 补 §G 给 CPT 的对接说明 + DDL 对齐 provider 读列
c01b707 feat(intraday): 盘中 provider 第一层——CPT 接入 + 跨仓对账 + 板比口径修正
931f43d feat(lab): P2 上线——参数化回放 + 单点变体搜索 + 七道门禁 + 提案
```

**② 关于那个「CPT 有 13 项未提交」的判断**

那是**你们自己仓**的状态：`git status` 显示 `?? _audit/` 50 个未跟踪 CSV 文件，
按目录归并正好 13 项。CPT 侧 `git status --short` 是**空输出**，工作树干净。

---

## 9. 一句提醒：轮询频率

10-22 夜里你们连发 10 次「CPT 无变化」。那个判断没错，但**频率弄错了轮询对象**：

| 时段 | 建议 |
|---|---|
| 交易日 09:25–11:35、12:55–15:05 | 每 15 分钟 |
| 其余（夜间、盘前、午休、周末） | 每 2 小时或不轮询 |

采集器只在交易时段产生数据，其余时段表在**结构上不可能变化**。

真要不要盯的是：**09:40 之后表里有没有新轮次**。没有才是有事。
