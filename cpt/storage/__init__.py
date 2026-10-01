"""CPT 自有持久化层。

**这一层只做存储**：SQL、连接、事务边界、表结构，全在这里。
上层（`application` / `web`）只调用函数，不出现 SQL 字符串、不直接持有连接。

## 边界：什么该进这一层，什么不该

| 该进 `storage/` | 不该进 |
|---|---|
| **CPT 自有表**（`public.cpt_*`）的读写 | 外部数据源读取 → `adapters/` |
| SQL 语句、psycopg 调用、连接管理 | 用例编排逻辑 |
| 事务边界（谁 commit、谁 rollback） | 领域算法 |

**为什么 `emotion_core` 的读不算 storage**：那个库共 28 张表，**只有 2 张是 CPT 的**
（`cpt_signal_event` / `cpt_dashboard_run`），其余 26 张（`daily_bar` 470MB、
`derived_bar` 381MB、`asel.ref_adjust_factor` 515MB、`trade_calendar` …）是
**跨项目共享的 A 股数据枢纽**。CPT 是它的读者，不是它的主人 —— 所以那些查询属于
「接外部数据源」，留在 `adapters/`。

## 事务边界：store 层不 commit

本层的函数**只 execute，不 commit**。理由与 R21 一致：便于多个写入共享一个事务，
也便于调用点把多个 store 的写入合成一次提交。

> ⚠️ **调用方必须自己 commit。** 踩过的坑：`cpt/web/app.py::_persist_run` 一开始
> 漏了 `conn.commit()`，导致 `AShareLocalClient.close()` 对未提交事务**静默回滚**——
> HTTP 全 200、日志零告警、`public.cpt_dashboard_run` 却是 0 行。回归测试见
> `tests/test_dashboard_runs_persisted.py::test_app_persist_run_commits`。

## 模块

| 模块 | 表 | 轮次 |
|---|---|---|
| `signal_event_store` | `public.cpt_signal_event` | R21 |
| `dashboard_run_store` | `public.cpt_dashboard_run` | R23 |
"""
