# storage/ 层复盘（R45 第一轮，补记于 2026-10-04 第五轮全量扫描）

> **这份文档是第五轮「从头全量扫」时补的。**
> 前四轮给 5 个层（domain / adapters / application / llm / web）都写了
> `review-<层>-layer-r45.md`，**唯独 storage 没有独立文档** ——
> 结论散在 `architecture.md` / `known-traps.md` / `progress-log.md` 三处。
> 全量扫描时按「每层复盘文档是否齐」一查才发现。
>
> ⇒ 这本身就是一个**元问题**：分层复盘时，「给每层产出同构的产物」
> 也得有人盯，否则漏掉的那层会一直没人补。

## 本层改了什么（提交即证据）

| 提交 | 改了什么 |
|---|---|
| `c26b67d` | `load_previous_signal` 吞 DB 异常 ⇒ 调用方的 `_rollback_quietly` 成**死代码** |
| `ad7f0ea` | 审计写失败被当成「重复提交」⇒ **对用户撒谎**（`enqueue_call` 写失败与重复提交共用 `False`） |
| `5d0f66c` | 补齐三处 rollback 缺口 + `prune` 接进 cron |
| `64c2782` | 删死代码 `find_by_id`；扫完 `dashboard_run_store` |
| `e7b9639` | 因子口径纪元标记（`factor_epoch_store`）+ chanlun 后端对比工具 |

## 核心教训：DB 失败必须抛，不能与「空」同码

这一层三个真 bug 是**同一个病**：把「查不到」和「查失败」混成同一个返回值。

PostgreSQL 的性质放大了它：**事务中一条语句失败 ⇒ 同连接后续全部 aborted**。
所以 catch 住 DB 异常再返回空值，不是降级，是**把局部失败放大成整页失败**。

| 函数 | 原来 | 修后 |
|---|---|---|
| `load_previous_signal` | 吞异常返回 `None` | 抛；调用方 rollback 不再是死代码 |
| `enqueue_call` | 写失败与重复提交都返回 `False` | 写失败抛，重复提交才 `False` |
| `recent_calls` | 读失败返回 `[]` | 抛 |
| `prune` | 失败 `return 0` | 抛（cron 才能判断「干完了」还是「一条没删」） |

**已用门禁② `scripts/check_storage_failure_semantics.py` 锁住**，并接进 CI。

## `prune` 的两个坑

1. **一张表混两类行**：`cpt_run_metric` 同时装 `KIND_RUN`（每轮一行，高频）
   与 `KIND_INSPECTION`（每天状态比对依据）。原来**不带 kind 条件**，
   一调用就把巡检行一起删了 ⇒ 加 `kinds` 参数，两类分开配窗口
   （cron 里 90 天 / 30 天）。
2. **失败与「没东西可删」同码** ⇒ cron 无法区分，静默慢性泄漏。改成抛。

**并且给 `prune` 补了测试**（`tests/test_run_metric_store_prune.py`）——
理由见下一节。

## ⚠️ 全量扫描撞见的缺口：零测试覆盖的**生产 cron 作业**

第五轮按「每个 storage 文件有没有测试引用」逐个查：

```
$ grep -rl run_metric_store tests/*.py | wc -l
0
```

**`run_metric_store` 被 0 个测试引用** —— 而它的 `prune`
**已经在生产 cron 里每天 04:10 UTC 删行**（R45 刚接的）。

本层复盘改了三处、接了 cron、加了门禁，却**没注意到这个模块整体没有测试**。
⇒ 已补 `tests/test_run_metric_store_prune.py`（5 个用例，钉住：
失败必须抛 / kind 过滤打在参数上 / 不 commit / 删���行数）。

> 教训：**「加了门禁」不等于「这一层有覆盖」**。
> `check_storage_failure_semantics.py` 查的是「有没有 catch 住 DB 异常返回空」，
> 它**不检查这个函数有没有测试**。两件事，别互相顶替。

## 本层其余结论

- `prune` 长期**零调用方**（R45 实测：全仓含测试 grep 无引用）——
  即这张表**从来没被清理过**，会按「每轮一行」无界增长成第二份 `daily_bar`。
  R45 已接 cron 收口。
- `find_by_id` 是死代码，已删。
- `test_web_a_share.py` 有时钟依赖 flake（`close_countdown`），已排除。

## 回归

全量 `pytest tests/`：**2 个失败，与基线逐条相同**
（`test_dashboard_runs_index.py::test_timestamp_falls_back_when_runtime_omits_generated_at`、
`test_dashboard_wiring_d.py::test_signal_stats_route_degrades_when_history_unavailable`），
均非 R45 引入。
