# CPT 导出格式 schema v1

版本：v1（**已冻结**）· 2026-09-25
状态：本文是 `cpt/application/export.py` 里 `EXPORT_SCHEMA_URL` 指向的目标文档。

> **为什么补这份文档。** `EXPORT_SCHEMA_URL` 的值是
> `https://github.com/nxz1026/Chan_Pattern_Trader/blob/main/docs/export-schema-v1.md`，
> 而且它被**写进每一个导出产物**的 `schema_url` 字段（`export.py:98`）——
> 但这份文件**一直不存在**。也就是说所有导出物都在指向一个 404。
> schema 已冻结，所以正确的修法是**把文档写出来**，而不是改 URL。
> 本文所有字段表都是从代码与真实产物里取的（`dataclasses.fields()` + 实跑
> `export_dataset()`），不是照抄设计稿。

---

## 1. 顶层结构

```json
{
  "schema_version": "v1",
  "schema_url": "https://github.com/nxz1026/Chan_Pattern_Trader/blob/main/docs/export-schema-v1.md",
  "config": { "...": "见 §2" },
  "metadata": {},
  "data": {
    "bars": [],
    "fractals": [],
    "bis": [],
    "zhongshus": [],
    "events": [],
    "signals": []
  }
}
```

| 键 | 类型 | 说明 |
|---|---|---|
| `schema_version` | `"v1"` | 常量 `EXPORT_SCHEMA_VERSION`。**格式变更必须改这个值** |
| `schema_url` | `str` | 常量 `EXPORT_SCHEMA_URL`，即本文档 |
| `config` | `object` | `RulesConfig.to_dict()`，见 §2 |
| `metadata` | `object` | 调用方自由字典；不传则为 `{}`（**不是** `null`） |
| `data` | `object` | 六个数组，见 §3。**哈希只覆盖这棵子树**（见 §5） |

`data` 的六个键**恒定存在**，没有数据时是空数组 `[]` 而不是缺键或 `null`。

---

## 2. `config`（`RulesConfig.to_dict()`，14 个键）

| 键 | 类型 | v0 默认值 | 含义 |
|---|---|---|---|
| `contain_direction` | `str` | `"forward"` | K 线包含合并方向 |
| `fx_qy_middle` | `bool` | `true` | 分型是否取中间元素 |
| `fx_qj_ck` | `bool` | `true` | 分型区间是否用 K 线区间 |
| `bi_type_new` | `bool` | `true` | 新笔口径 |
| `zs_wzgx` | `str` | `"zgd"` | 中枢构成规则 |
| `zs_level_count` | `int` | `1` | 中枢所需级别数 |
| `min_elements_for_higher_bi` | `int` | `5` | 高级别笔最少元素数 |
| `min_bi_len` | `int` | `6` | 笔的最短 K 线数 |
| `macd_fast` | `int` | `12` | MACD 快线 |
| `macd_slow` | `int` | `26` | MACD 慢线 |
| `macd_signal` | `int` | `9` | MACD 信号线 |
| `divergence_compare` | `str` | `"area"` | 背驰比较口径（面积 / 力度） |
| `levels` | `int[]` | `[5, 30]` | 参与的级别（分钟） |
| `config_version` | `str` | `"v0"` | 规则版本。**与 `schema_version` 是两件事**：前者是算法口径版本，后者是文件格式版本 |

口径的权威定义在 `docs/rules.md`；本文只描述字段形状。

---

## 3. `data` 各数组的字段

所有时间戳都是**毫秒 Unix 时间**（`int`），不是 ISO 字符串。价格/量都是 `float`。

### 3.1 `bars[]` — 12 个 dataclass 字段 + 1 个派生字段

`CanonicalBar` 的 `asdict()` 结果，另加一个派生属性 `direction`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `open_time` | `int` | 开盘时间（ms） |
| `open` / `high` / `low` / `close` | `float` | OHLC |
| `volume` | `float` | 成交量 |
| `close_time` | `int` | 收盘时间（ms） |
| `quote_volume` | `float` | 成交额 |
| `trade_count` | `int` | 成交笔数 |
| `taker_buy_base_volume` | `float` | 主动买基础量 |
| `taker_buy_quote_volume` | `float` | 主动买报价量 |
| `is_closed` | `bool` | 该 bar 是否已收盘 |
| `direction` | `int` | **派生**（涨跌方向），由 `_bar_to_dict` 显式补上，排在最后 |

> `direction` 不在 `CanonicalBar` 的 dataclass 字段里，`asdict()` 不会带它 ——
> 它是 `_bar_to_dict()` 手工补的（`export.py:51-59`）。消费方若自己用
> `asdict(bar)` 重建会**少这个键**。

### 3.2 `fractals[]` — 8 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | `"top"` \| `"bottom"` | 顶/底分型 |
| `level` | `int` | 级别（分钟） |
| `bar_index` | `int` | 在原 bar 序列里的下标 |
| `start_time` / `end_time` | `int` | 区间（ms） |
| `high` / `low` | `float` | 区间高低点 |
| `source_ids` | `str[]` | 来源对象 id（递归映射用） |

### 3.3 `bis[]` — 10 字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `level` | `int` | — | 级别 |
| `direction` | `int` | — | 方向（+1 / -1） |
| `start_time` / `end_time` | `int` | — | 区间（ms） |
| `high` / `low` | `float` | — | 区间高低点 |
| `source_ids` | `str[]` | — | 来源分型 id |
| `power_price` | `float` | `0.0` | 价格力度 |
| `power_volume` | `float` | `0.0` | 成交量力度 |
| `length` | `int` | `0` | 笔长度（K 线数） |

### 3.4 `zhongshus[]` — 6 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `level` | `int` | 级别 |
| `start_time` / `end_time` | `int` | 区间（ms） |
| `high` / `low` | `float` | 中枢上下沿 |
| `bi_ids` | `str[]` | 构成该中枢的笔 id |

### 3.5 `events[]` — 5 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_type` | `str` | 见 §4 词表 |
| `structure_id` | `str` | 被事件作用的结构 id |
| `revision` | `int` | 状态版本号（只增） |
| `payload` | `object` | 事件负载，自由结构 |
| `occurred_at` | `int` | 事件发生时间（ms） |

### 3.6 `signals[]` — 13 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `signal_id` | `str` | **稳定 upsert 主键**（见 `cpt/domain/signal.py`） |
| `level` | `int` | 级别 |
| `signal_type` | `"first_buy"` | 当前只实现一买 |
| `status` | `str` | 见 §4 词表 |
| `structure_id` | `str` | 关联结构 id |
| `center_ids` | `str[]` | 关联中枢 id |
| `divergence_status` | `str` | 见 §4 词表 |
| `alert_time` | `int \| null` | 以下五个时间戳可空 |
| `candidate_time` | `int \| null` | |
| `confirmed_time` | `int \| null` | |
| `invalidated_time` | `int \| null` | |
| `price` | `float` | 触发价 |
| `source_revision` | `int` | 所依据的结构版本 |

---

## 4. 取值域（`cpt/domain/models.py` 的 `Literal` 别名）

```text
FractalKind        = "top" | "bottom"
StructureKind      = "fractal" | "bi" | "zhongshu" | "trend_type" | "signal"
StructureStatus    = "forming" | "confirmed" | "invalidated" | "open_end"
EventType          = "created" | "updated" | "confirmed" | "reclassified"
                     | "invalidated" | "closed"
SignalStatus       = "structure_ready" | "alert" | "candidate" | "confirmed"
                     | "invalidated"
DivergenceStatus   = "not_checked" | "not_detected" | "detected"
```

> 这些是**类型别名**（`typing.Literal`），运行时就是普通字符串，**没有 Enum 类可校验**。
> 消费方要自己按上面的词表核对；多一个词不会被 schema v1 拦住。

---

## 5. 序列化与哈希

### 5.1 JSON 序列化参数（`export_to_json_string`）

```python
json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
```

- `sort_keys=True` → **对象键按字母序**，所以文件里的键序不是本文的书写顺序。
- `ensure_ascii=False` → 中文原样输出（不转 `\uXXXX`）。
- `indent=2`。
- `allow_nan=False` → **`NaN` / `Infinity` 会抛异常**，不会写出非法 JSON。
  上游若可能产生 `NaN`（如除零），必须在构造对象前处理掉。

### 5.2 `dataset_hash(payload)`

- 只对 **`payload["data"]` 子树**做 sha256（`config` / `metadata` **不参与**）。
- 规范化：`sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False`。
- **数组顺序参与哈希**：六个数组里元素顺序变了就是不同哈希。想"忽略顺序"，
  调用方须在导出前对序列排序并固定排序键。
- 因此：改 `config` 或 `metadata` 不改哈希；重排 bars 会改哈希。

---

## 6. 占位哨兵（`PLACEHOLDER_TIME = -1`）

`export_dataset()` 会**拒绝导出**任何 `start_time` / `end_time` 等于 `-1` 的对象：

```python
_reject_placeholders("fractals", fractals)
_reject_placeholders("bis", bis)
_reject_placeholders("zhongshus", zhongshus)
```

抛 `ValueError`，消息形如
`fractals[3].start_time 是占位时间戳 (-1); schema v1 不接受占位语义。请在反腐层传入 bars 参数解析。`

反腐层（`adapters/reference_chanlun.py` 等）在缺 bars 时会把时间戳填成 `-1` 占位；
这个检查就是防止占位语义污染已冻结的格式。

> ⚠️ **已知覆盖缺口（照实记录）**：`events` 与 `signals` **没有**跑这个检查
> （`events` 的字段是 `occurred_at`、`signals` 的是 `alert_time` 等，都不在默认的
> `("start_time", "end_time")` 里）。它们目前靠上游自律。要收紧得给这两类对象
> 传自定义 `fields` 元组。

---

## 7. 消费方校验清单

拿到一份 schema v1 文件时，建议按顺序检查：

1. `schema_version == "v1"` —— 不是就按别的版本处理，**别猜**。
2. `data` 六个键都在（空数组是合法的，缺键不是）。
3. 每个 `bars[i]` 有 `direction`（用 `asdict()` 重建过的会缺，见 §3.1）。
4. 字符串字段落在 §4 的词表内。
5. `start_time <= end_time`；`start_time != -1`（`-1` 说明是未解析的占位）。
6. 时间戳单位是**毫秒**，不是秒。

---

## 8. 变更政策

**v1 已冻结。** 任何字段的增删改名、时间戳单位变化、`data` 子树的键变动，都必须：

1. 把 `EXPORT_SCHEMA_VERSION` 改成 `v2`；
2. 新建 `docs/export-schema-v2.md`，**保留**本文档（v1 的消费方还在读）；
3. 更新 `EXPORT_SCHEMA_URL`。

只加 `metadata` 里的自定义键**不需要**升版本（`metadata` 不参与哈希，也不是 schema 的一部分）。
