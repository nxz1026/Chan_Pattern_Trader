# 复盘：`run_metric_store.ensure_table` 该不该删（R45）

> 起因：R45 扫 storage/ 死代码时，AST 核实 `ensure_table` **全仓零调用方**，
> 与 `find_by_id` 并列。`find_by_id` 已删（纯孤儿），
> `ensure_table` **暂留待决** —— 本文给出裁决依据。
>
> 状态：**待 owner 决定**。倾向：**保留**（见 §4）。

---

## 1. 为什么它不能像 `find_by_id` 那样直接删

`cpt/storage/run_metric_store.py::_DDL` 是 **`public.cpt_run_metric` 唯一的建表来源** ——
`scripts/migrations/` 里**没有**这张表的迁移文件。

删掉它 ⇒ 表一旦被 drop / 换库，**代码里没有任何东西能重建它**，
而 `append_metrics` / `prune` / `latest_inspection` / `waterline_trend` /
`recent_metrics` / `latest_run_fingerprint` 全部依赖它。这不是「删个死代码」，
是「删掉唯一的 schema 定义」。

`find_by_id` 则是纯 CRUD 出口，零调用方、零 schema 含义 —— 已删。

---

## 2. R45 实测：`_DDL` 与真库**零漂移**

这是决定「能不能信它」的关键。逐列比对 `information_schema` vs 代码 DDL
（`emotion_core` 真库，2026-10-03）：

```
代码 _DDL 声明: 22 列
库里实际      : 22 列
代码有、库里没有: （无）
库里有、代码没有: （无）

3 个索引全部存在:
  ✅ cpt_run_metric_observed_idx
  ✅ cpt_run_metric_kind_idx
  ✅ cpt_run_metric_symbol_idx

21 条 NOT NULL 约束全部对得上
kind 实际取值 ['inspection', 'run'] == 代码常量 KIND_RUN / KIND_INSPECTION
```

比对脚本一度报了两条"不符"，**核实后都是我脚本的口径问题，不是真漂移**：

| 报的"不符" | 真相 |
|---|---|
| `id: bigserial != bigint` | `information_schema` 就是把 serial 列报成 `bigint` + `default=nextval('cpt_run_metric_id_seq')`。`pg_attribute` 权威源确认 `NOT NULL=True`，与 DDL 的 `PRIMARY KEY` 一致 |
| `observed_at: timestamptz != timestamp with time zone` | **同一个类型的两种写法**。`pg_attribute` 确认 `NOT NULL=True`，两边一致 |

⇒ **它是可信的 schema 源**，不是一份腐烂的副本。

---

## 3. 那它为什么零调用方

R45 实测：表是**手工**建出来的（`deploy/README.md:269` 提到"手工验证时那 6 行"），
建完就没再建过 —— 幂等的 DDL 在稳定期自然零调用。

这不是缺陷，是**部署姿势**的遗留：别的表走 `scripts/migrations/*.sql`，
这张表走"跑一次 Python 函数"。

---

## 4. 三个选项

| | 做法 | 优点 | 代价 |
|---|---|---|---|
| **A. 保持现状**（倾向） | 函数继续当 schema 源，补一句 docstring 说明"迁移请手动跑一次" | 零改动、零风险；DDL 已核实与真库一致 | 与仓里其他表的 schema 管理方式不一致；新人不看代码就不知道表是怎么来的 |
| B. 抽成迁移文件 | 建 `scripts/migrations/xxxx_cpt_run_metric.sql`，`ensure_table` 改为读并执行它 | 与其他表统一；schema 变更可走 review | 要改部署路径；`ensure_table` 变成文件读取器，多一层间接 |
| C. 删掉 | —— | —— | ❌ **表无法重建** —— 不考虑 |

**我的建议是 A**，理由：DDL 已核实与真库零漂移，函数本身是幂等且正确的；
为了「与别的表风格统一」而引入文件读取间接层，收益小于风险。
**但 B 的诉求是真实的**（schema 变更应该走 review），所以这件事值得记下来
而不是忘掉 —— 若将来这张表要加列，届时再顺势转 B。

---

## 5. 真正的债（不是「函数死」）

> **`cpt_run_metric` 的 schema 没有迁移文件。**

这是本条复盘的核心结论。`ensure_table` 死不死是表象；
「schema 变更无法走 review」才是问题 —— 将来给这张表加列时，
大概率会有人直接改 `_DDL` 然后忘记在真机上跑，导致**代码与库漂移**。

**建议**：在 `ensure_table` 的 docstring 里写死一句
「本函数是 schema 的**唯一**来源；改列后必须在真机执行一次，否则代码与库会漂移」，
并把它列进部署清单。转移成迁移文件（B）时删掉这句。

---

## 6. 判定命令（想复核时用）

```sql
-- 库里实际的列 / 索引 / 约束
SELECT column_name, data_type, is_nullable, column_default
  FROM information_schema.columns WHERE table_name='cpt_run_metric'
 ORDER BY ordinal_position;
SELECT indexname FROM pg_indexes WHERE tablename='cpt_run_metric';
SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
 WHERE conrelid='public.cpt_run_metric'::regclass;
```

⚠️ 比对时**别信 `information_schema.data_type` 的字面拼写**：
`timestamptz` 与 `timestamp with time zone` 是同一个类型，
`bigserial` 会被报成 `bigint`。要判类型是否真的漂移，用 `pg_attribute` 的
`format_type(atttypid, atttypmod)`，可空性用 `attnotnull`。
（R45 在这里被自己的脚本骗了一次。）
