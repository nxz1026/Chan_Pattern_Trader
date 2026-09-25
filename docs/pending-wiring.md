# 待接线模块清单（pending-wiring）

> 建立：2026-09-25（代码审核 P0-2 处置）
> 来源：`docs/audit/cpt-code-audit-20260925.md` §3.4、`docs/audit/verification-20260925.md` §4
> 维护：新增待接线模块必须登记在此；接线完成或决定删除时从此表移除，并在提交信息里说明。

## 为什么有这份清单

2026-09-25 的独立核实发现仓库里有约 2,353 行「生产代码零导入」的模块。核实结论是
它们**不是同一类东西**，不能一刀切删除，故按「是否有明确的产品位置」分三类处置：

| 处置 | 判据 |
|---|---|
| 删除 | 既无生产导入方，也**不在任何现行计划文档**里 |
| **待接线（本清单）** | 无生产导入方，但**产品路线图或现行台账里有明确位置** |
| 保留 | 生产路径可达（活代码） |

判据文档（按效力排序）：

1. `/home/ubuntu/work/cpt-audit/CPT-总计划-2026-09-24.md`——现行**唯一任务台账**（仓库外）
2. `docs/dashboard-product-roadmap.md`——`docs/dashboard-plan.md:151` 明确「**后续产品路线
   以本文件 Phase 0-6 为准**」
3. `docs/implementation-plan.md`——头部已声明自己是**历史记录**，**不作为**判据

> ⚠️ **判据踩坑记录**：本次执行第一版只按「模块名是否出现在台账里」判定，把 12 个
> `dashboard_*` 当无规划删掉了。装 P0-3 门禁时复查才发现 roadmap 的 Phase 3–6 用
> **功能描述**（不是模块名）覆盖了它们，遂全部回滚。教训：判据文档必须按**功能**而
> 非**文件名**比对。

## 清单（16 个模块，919 行）

| 模块 | 行数 | 产品位置 | 接线目标 |
|---|---:|---|---|
| `cpt/domain/signal.py` | 311 | `CPT-总计划-2026-09-24.md` 在范围内；`a_share_rules.py` docstring 写明「C4 T+1…由 `cpt.domain.signal` 在评估一买/一卖时读取」 | 一买状态机接进 A 股信号链；`signal_id` 已按**稳定 upsert 主键**设计 |
| `cpt/domain/a_share_rules.py` | 152 | 台账「**实现位置**」一节明确列出（涨跌停/停牌/T+1）；R17-3 刚落地 A 股主看板 | 结构标签接进 A 股主看板 |
| `cpt/application/dashboard_parity.py` | 83 | roadmap §「Phase 2：Oracle 对比」+ §「后端代码框架」 | 接 oracle 对比。**HTTP 路由已活**：`cpt/web/app.py:208`，但 `dashboard_snapshot_v2.py:61` 恒填占位 |
| `cpt/application/dashboard_runs.py` | 37 | roadmap §「R1 数据集/运行浏览器」+ §「后端代码框架」 | 接运行浏览器。**HTTP 路由已活**：`cpt/web/app.py:210`，但 `dashboard_snapshot_v2.py:62` 硬编码 `[]` |
| `cpt/application/dashboard_levels.py` | 35 | roadmap Phase 5 P1「级别递归树」 | 级别递归树视图 |
| `cpt/application/dashboard_event_audit.py` | 37 | roadmap Phase 5 P1「事件前后状态对比」 | 事件前后状态对比 |
| `cpt/application/dashboard_quality.py` | 28 | roadmap Phase 5 P1「数据质量报告」 | 数据质量报告面板 |
| `cpt/application/dashboard_stats.py` | 25 | roadmap Phase 6 P2「一买统计」 | 研究统计面板 |
| `cpt/application/dashboard_signal_history.py` | 41 | roadmap Phase 4 P1「信号历史列表」+ Phase 6 P2「alert→confirmed 转化率」「invalidated 原因分布」 | 信号历史与状态转移 |
| `cpt/application/dashboard_export.py` | 20 | roadmap Phase 6 P2「时间范围切片导出」 | 时间范围切片导出 |
| `cpt/application/dashboard_multi_run.py` | 23 | roadmap Phase 6 P2「双数据集同步对比」 | 多运行对齐 |
| `cpt/application/dashboard_compare.py` | 26 | roadmap R5「两份 snapshot 字段级 diff」+ Phase 6 P2「双数据集同步对比」 | A/B 快照对比（依赖 `dashboard_reproducibility.snapshot_diff`） |
| `cpt/application/dashboard_watchlist.py` | 23 | roadmap Phase 4 P1「多交易对」 | 多标的盯盘列表 |
| `cpt/application/dashboard_watch.py` | 24 | roadmap Phase 4 P1「reconnect/stale」 | 盯盘真实指标投影 |
| `cpt/application/dashboard_market_fetch.py` | 20 | roadmap Phase 3 P0「真实 24h 高低点和成交量，无法提供时明确 unavailable」 | 市场聚合归一化（现由活模块 `dashboard_market` 承担一部分） |
| `cpt/application/dashboard_realtime.py` | 34 | roadmap Phase 4 P1「SSE 或高效实时更新」 | 实时刷新投影（现由活路径 `_RealtimeProvider` 承担一部分） |

另有 **3 个函数**（非独立模块）同样待接线，它们所在模块本身是活的：

| 函数 | 产品位置 | 说明 |
|---|---|---|
| `cpt/application/dashboard_reproducibility.py::snapshot_diff` | roadmap R5「两份 snapshot 字段级 diff」 | 所在模块的 `reproducibility_metadata` / `config_hash` 已在生产链路里 |
| `cpt/adapters/a_share_local.py::_to_wind_code` | 台账决策 C1「复权因子 backfill 走 Wind」 | `000002 → 000002.SZ` 转换；`a_share_public.py:115` 的注释以它为实现参照 |
| `cpt/adapters/wind_source.py::fetch_adjust_factors` | 台账决策 C1 同上 | Wind 复权因子取数 |

## 约束

1. **改动本清单内模块前先读本文**——它们没有生产调用方，改错了不会有测试变红。
2. 这些模块的现有测试是**自证式**的（测一段不运行的代码），**不能**当作「已在生产验证」。
3. CI 的 vulture 门禁（`.github/workflows/ci.yml`，阈值已从 80 降到 60）对这批名字走
   `whitelist.py` 白名单。白名单是「已知未接线」的登记，不是「忽略告警」。
4. 接线时**先写从生产入口可达的集成测试**，再把模块从本清单和 `whitelist.py` 里移除。

## 相关

- `docs/audit/cpt-code-audit-20260925.md` §3.4（孤儿层与死模块）、§5.1（dbconfig 三胞胎）
- `docs/audit/verification-20260925.md` §4（P0 清单）
- `docs/architecture.md` §2（`engine/` 与 `storage/` 两层的删除说明）
