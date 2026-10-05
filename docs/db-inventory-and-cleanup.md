# emotion_core 数据库盘点与清理清单（2026-10-03）

> 只读盘点，**未做任何删改**。所有数字来自 Oracle 真机实测。
> 环境：PostgreSQL 18.6，`emotion_core` **1947 MB**，33 张表 / 129 个索引 / 0 视图。

---

## 0. 先说结论：**能清的很少，而且最大的两块都不能动**

这个库**没有"垃圾"可清**。33 张表**每一张都有归属**（CPT 引用 15 张，emotion-core 引用另外 18 张），
无孤立 schema、无孤立视图（视图数为 0）、无多余扩展（只有 `plpgsql`）、无孤儿序列。

⚠️ **两个最大的"疑似可清"项都是陷阱**，见 §3、§4。

---

## 1. 33 张表归属全景

`pg_stat_*` 的 `stats_reset` 为 NULL、`xact_commit=271681` ⇒ 统计**长期累积未重置**，
所以"有读记录 / 无读记录"这个信号是**可信的**（不是刚重启的空统计）。

### 1.1 CPT 引用、且 CPT 会写（6 张）—— 绝对不能删

| 表 | 大小 | 行数 | 读次数 |
|---|---|---|---|
| `asel.ref_adjust_factor_v2` | **461 MB** | 3,327,508 | 4,410,211 |
| `public.cpt_structure_event` | 3984 kB | 6,721 | 6,242 |
| `public.cpt_dashboard_run` | 2744 kB | 78 | 1,375 |
| `public.cpt_run_metric` | 1888 kB | 2,992 | 249 |
| `public.cpt_llm_call` | 112 kB | 6 | 225 |
| `public.cpt_signal_event` | 96 kB | 41 | 645 |

### 1.2 CPT 只读、跨项目共享（9 张）—— 别删

`asel.ref_adjust_factor` 571 MB(3.4M 行) / `public.daily_bar` 470 MB /
`public.derived_bar` 381 MB / `public.hot_rank` 40 MB / `public.trade_calendar` /
`asel.security_master` / `public.limit_pool_em` / `public.strategy_signal` / `public.ladder_day`

> 注意：**`asel.ref_adjust_factor` 虽然是 CPT 在读，但 emotion-core 也在写**
> （2,797 万次写操作）。它不是 CPT 私产。

### 1.3 CPT 完全没引用、但 emotion-core 在用（17 张）—— 不是清理对象

`review_report` / `stock_basic` / `market_stat` / `signal` / `signal_outcome` /
`pipeline_state` / `llm_call_log` / `eval_result` / `promotion_day` / `alert` /
`position` / `theme_group` / `theme_tag` / `ingest_progress` / `trade_event` /
`data_revision` / `watchlist`

已逐个 grep `/home/ubuntu/DSH/emotion-core/` 确认，**每一张都在 emotion-core 的代码里被引用**。

---

## 2. 真正可清理的（总量约 25 MB，占 1.3%）

| # | 对象 | 大小 | 建议 | 风险 |
|---|---|---|---|---|
| 1 | `asel.ref_adjust_factor_v2` 死元组 | **~20 MB** | `VACUUM FULL` 或 `pg_repack` | 低。死元组 4.5%，FULL 会持锁 ~1min |
| 2 | `public.derived_bar` 死元组 | ~5 MB | 同上（1.3%，可不管） | 低 |
| 3 | 3 个零扫描索引 | 88 kB | 可删 | 极低，但省不到什么 |
| 4 | `public.cpt_structure_event_id_backup_20261001` + `_pkey` | 200 kB | **暂留** | 见 §5 |

零扫描索引明细：

```
asel.security_master.idx_sm_board                                56 kB
public.cpt_dashboard_run.idx_cpt_dashboard_run_dataset_hash       16 kB
public.cpt_llm_call.idx_cpt_llm_call_subject                      16 kB
```

> 索引总占用 **662 MB / 129 个**，零扫描的只有 88 kB ⇒
> **索引没有膨胀问题**，别去动 `*_pkey`（它们各自 800 万+ 次扫描）。

---

## 3. ⚠️ 陷阱一：`ts_snap` / `ts_stg` 表空间**不属于 emotion_core**

第一眼看着像"两个空表空间，白占 86 MB"。**但它们不是 emotion_core 的。**

```
表空间里的关系文件路径：…/tablespaces/ts_snap/PG_18_202506291/16823/17789
                                                          ^^^^^^
                                                    这是【数据库 OID】
```

查 `pg_database`：

| OID | 数据库 | 大小 |
|---|---|---|
| 16823 | **`league`** | **597 MB** |
| 114493 | emotion_core | 1947 MB |

⇒ `ts_snap`(84 MB) / `ts_stg`(2 MB) 是 **`league` 库**在用的。
表空间是 **PG 集群级共享**的：在 `emotion_core` 里查 `pg_tablespace` 看得到、
`pg_class` 里却查不到对象，就是这个原因。

**这两个表空间绝对不能删** —— 会打掉 `league` 项目。
本轮差点把它写进清理清单，靠查了 `pg_database` 才拦住。

---

## 4. ⚠️ 陷阱二：`ref_adjust_factor_v2`(461 MB) **是活的，不能删**

它看着像"切表后的废弃暂存表"，占全库 24%。但：

- 每日 cron `factor-recompute-daily.sh` **就往它写**（`FACTOR_RECOMPUTE_TABLE`）；
- 它是再切表时的数据来源（回滚能力）；
- 切表后仍有 134 只票不在生产口径里，后续补算仍要落这儿。

**删了 = 每日重算直接报错。**

真正该做的是给暂存表加**保留策略**（只保留 N 天内的 `computed_at`），
而不是删表 —— 这需要你先定保留多久。

---

## 5. 备份表 `cpt_structure_event_id_backup_20261001`

- 200 kB（含主键），672 行
- 来源：`scripts/migrations/2026-10-06_r27_market_prefix.sql` 的 id 迁移
- **回滚脚本 `..._rollback.sql` 会读它** ⇒ 迁移回滚能力依赖这张表
- 建议：等 r27 稳定运行一段时间（比如 1 个月）再删，**现在别动**

---

## 6. schema / 扩展 / 视图 / 序列

| 项 | 结果 |
|---|---|
| schema | `asel` 1344 MB(3 表) / `public` 1255 MB(30 表)，**无孤立 schema** |
| 视图 / 物化视图 | **0 个** |
| 扩展 | 只有 `plpgsql` |
| 自定义类型 / 域 | 0 |
| event trigger | 0 |
| 序列 | 10 个，全部有对应表，**无孤儿** |

---

## 7. 建议的执行顺序（如果你要动手）

> ### ✅ 执行状态（R45 回填，2026-10-04，生产库实测）
>
> | 项 | 建议 | 实际 |
> |---|---|---|
> | `asel.idx_sm_board` | 删 | ✅ **已删** |
> | `public.idx_cpt_dashboard_run_dataset_hash` | 删 | ✅ **已删** |
> | `public.idx_cpt_llm_call_subject` | 删 | ⛔ **保留** |
> | `VACUUM FULL asel.ref_adjust_factor_v2` | 做 | ❌ 未做（要低峰窗口） |
> | v2 暂存表保留策略 | 待你定保留天数 | ❌ 未定 |
> | `cpt_structure_event_id_backup_20261001` | 一个月后评估 | ⏳ 未到评估时点 |
>
> **`idx_cpt_llm_call_subject` 保留的理由**：有真实的
> `WHERE subject_id` 查询路径（`llm_call_store` 的单主体回查），
> 属于「反范式化冗余索引」，不是零扫描索引。
> ⇒ 下面 SQL 块里的第 2 步**只删前两条**。

```sql
-- 1) 低风险：回收死元组。VACUUM FULL 会持 ACCESS EXCLUSIVE 锁，
--    期间该表读写全阻塞 —— 要在低峰做。
VACUUM FULL ANALYZE asel.ref_adjust_factor_v2;   -- ~20MB, 锁 ~1min

-- 2) 低风险：删零扫描索引（省 88 kB）—— ⚠️ R45 已删前两条，第三条**故意保留**
DROP INDEX CONCURRENTLY asel.idx_sm_board;                            -- ✅ 已删
DROP INDEX CONCURRENTLY public.idx_cpt_dashboard_run_dataset_hash;    -- ✅ 已删
-- DROP INDEX CONCURRENTLY public.idx_cpt_llm_call_subject;  ⛔ 不删：有 WHERE subject_id 查询路径

-- 3) 暂存表保留策略（需要你先定「保留几天」）
--    DELETE FROM asel.ref_adjust_factor_v2
--      WHERE computed_at < now() - interval '30 days';
--    ⚠️ 先确认不再需要「用暂存表再切一次」，否则别删。

-- 4) 备份表：一个月后再评估
--    DROP TABLE public.cpt_structure_event_id_backup_20261001;
```

---

## 8. 我的判断

**这个库目前没有值得清理的东西。** 1947 MB 里：

- 1334 MB 是 `asel` 因子表 —— 正在被 CPT + emotion-core 共用
- 1255 MB 是 `public` —— 绝大多数是 `daily_bar` / `derived_bar` 这类**行情主数据**，
  是整个 A 股体系的地基，不是垃圾

唯一"能回收"的是约 20 MB 死元组（1%）和 88 kB 索引（0.004%）。
**为了 1% 做 VACUUM FULL 值得吗？** 我的建议是**不值得** ——
除非你打算把 `derived_bar` 的 4 月底缺口问题（1~2% 缺天）一起处理时顺带做。

**真要省空间，得从数据建模下手**（比如因子表几百 MB，但有效数据只有
`code/trade_date/hfq_factor` 三列，加起来才 364 MB heap + 207 MB 主键），
那是另一个工程。

---

## 9. 顺带发现（不属于清理范畴，但该记）

`public.daily_bar` 有 **5221 次 DELETE**。行情表被删过 5221 行 ——
如果那是 ingest 的"当天数据重写"，属正常；如果是误删，得查 ingest 日志确认。

---

## 10. 已执行：删除 2 个确认无用的索引（2026-10-03 19:00）

```sql
DROP INDEX CONCURRENTLY asel.idx_sm_board;                        -- 56 kB
DROP INDEX CONCURRENTLY public.idx_cpt_dashboard_run_dataset_hash; -- 16 kB
```

索引数 **129 → 127**。用 `CONCURRENTLY`，不阻塞读写。**数据一行未动**
（`ref_adjust_factor` 3,393,640 / `ref_adjust_factor_v2` 3,334,585 / `daily_bar` 3,393,640 均未变）。

### 判定依据（不是只看 `idx_scan=0`）

| 索引 | 结论 | 依据 |
|---|---|---|
| `idx_sm_board` | **删** | CPT 的查询是 `WHERE code = ANY(...)`，不带 board；emotion-core 全仓无 `security_master` 的 board 查询 |
| `idx_cpt_dashboard_run_dataset_hash` | **删** | `dashboard_run_store` 只有 `WHERE run_id = ANY(...)`（走主键），**没有** `WHERE dataset_hash` |
| `idx_cpt_llm_call_subject` | **保留** ⚠️ | `llm_call_store.recent_calls()` 里有 `WHERE subject_id = %s` —— **查询路径存在**，只是表里只有 6 行还没触发过 |

> ⚠️ 第三条是本轮**推翻了初判**的地方：`idx_scan=0` 只说明"没被用过"，
> 不说明"不会被用"。**零扫描索引要先查代码里有没有对应查询路径**，
> 否则会把「还没跑到」误判成「不需要」。

### 保留的（按约定）

- `public.cpt_structure_event_id_backup_20261001` —— r27 迁移的**回滚依赖**，
  `2026-10-06_r27_market_prefix_rollback.sql` 会读它。一个月后再评估。
- `asel.ref_adjust_factor_v2` 整表 —— 每日 cron 的落点 + 再切表的数据来源。
  按约定**保留 3 年**；当前只有 2.75 年（666 交易日），所以这条策略**今天是空操作**，
  不需要任何定时任务。真要落地时点是约 2027-01，届时按 `computed_at` 清理。

## 11. 一个复核时的坑（记下来）

复核删索引后的数据时，我先拿 `pg_stat_user_tables.n_live_tup` 当"预期行数"，
发现 `daily_bar` 3,393,640 vs 3,393,644 差 4 行，警报了半天。

**`n_live_tup` 是 VACUUM 估算值，不是精确 count。** 判断数据有没有变，
一律用 `SELECT count(*)`，别用 `pg_stat`。

---

## 8. R45 新增表：`cpt_factor_epoch`（2026-10-03 建）

本文写作时生产库没有这张表，它是因子**口径切换**用的。R45 复盘时
`information_schema` 实测确认 7 张 `cpt_` 表，这是漏掉的一张。

| 表 | 用途 | 行数 |
|---|---|---|
| `cpt_factor_epoch` | 记录复权因子口径的切换点（单行） | 1 |

**为什么用单行表而不是给 `cpt_signal_event` 增列**：
口径切换本质是**一个时间点**，不是每行一个属性。
`created_at < switched_at` 即旧口径 —— 这样
**历史回填**（往表里插旧 `created_at` 的行）和**新写入**不会造成新旧混态，
而按行加 `factor_version` 列做不到这一点（回填的旧行天然带旧版本，
但「写入时刻」和「数据口径」会脱钩）。

用途：看板 API 的 `summary.factor_epoch` 字段暴露切换点，
供前端区分「这条信号是哪个口径下的」。

- 建表脚本：`scripts/migrations/2026-10-03_r45_factor_epoch.sql`
- 存取层：`cpt/storage/factor_epoch_store.py`
- 切表实测：两次，`2026-10-03T09:55:46Z` / `2026-10-03T10:34:24Z`

> ⚠️ 顺带一条本文的**方法论教训**：`information_schema.tables` 才是
> 「有哪些表」的判据，靠 `grep -rhoE "cpt_[a-z_]+" cpt/**/*.py` 反推会
> **漏掉没有代码常量的表**（本例：`cpt_factor_epoch` 的表名只在 SQL 里出现，
> 代码侧只 import store 类）—— 也**会多出索引名**（`idx_` 前缀被 grep 吃掉，
> 把 `idx_cpt_llm_call_subject` 误读成一张表）。
> 本轮两份文档都被这个假阳性/假阴性咬到。

---

## R45 追加：`cpt_dashboard_run` 的测试污染已清理（2026-10-05）

web 契约测试用 `tests/conftest.py::served` 起**真 server** 打**真请求**，
而测试与生产**共用同一个库**（`~/.dbconfig` 的 `$DBNAME`，无测试库）
⇒ 真的往本表写了测试快照。

同时 `/api/dashboard/runs` 是「**表优先**」，于是
`test_timestamp_falls_back_when_runtime_omits_generated_at` 长期失败
（读到的是别的测试写的墙钟时间）。

| | 清理前 | 清理后 |
|---|---:|---:|
| 总行数 | 89 | **35** |
| 判定为测试数据 | 54 | 0（已删） |

删除判据（**只删能确证的**）：

```sql
snapshot::jsonb #>> '{market,symbol}' LIKE 'SYM%'          -- 49 行，必是 fixture
OR snapshot::jsonb #>> '{runtime,data_source}'
   IN ('fixture', 'native_fixture')                          -- BTCUSDT 的 fixture 行
OR snapshot::jsonb #>> '{market_24h,reason}'
   IN ('demo_mode_no_upstream', 'fixture_mode_no_upstream')
```

保留的 35 行是 `data_source = db_local` 的真实运行记录。
⚠️ 其中 `000001` / `600036` 相隔 2 秒（08:34:38 / 08:34:40），
**这个形态也可能是测试** —— 未能确证，故保留，需要时再核。

**防复发**：新增 `CPT_RUN_STORE_PERSIST=0` 开关（`tests/conftest.py` autouse），
**读写两端**都关，且每个测试前后清空 in-process ring。
**生产默认行为不变**。
