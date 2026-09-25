"""vulture 白名单（`.github/workflows/ci.yml` 的 Dead-code audit 步骤读取本文件）。

vulture 的用法是 `vulture cpt --min-confidence 60 whitelist.py`：本文件里出现的名字
被算作「已使用」。**这不是忽略告警的开关，而是逐条登记的判断**——每条都在下方注明
属于哪一类。

背景（2026-09-25 代码审核 P0-3）：原 CI 是
`vulture cpt --min-confidence 80 --exclude 'cpt/web/app.py'`，等于**门禁失效**——
vulture 给「未使用的函数/类」打的置信度正是 60%，阈值 80 把它们全部滤掉，该步骤
永远 exit 0 且零输出。改成 60% 后立刻点出 42 条，逐条定性如下。

**真死代码不进本文件。** 本次执行中门禁新发现并已删除的真死代码：
`cpt/web/__main__.py` 的 3 个 `current_symbol`（全仓零调用）、
`cpt/adapters/a_share_pool.py` 的模块级 `logger`（无任何日志调用）。
"""

# --------------------------------------------------------------------------- #
# 1. 框架回调：由标准库 BaseHTTPRequestHandler 调用，不是被我们调用
#    （这也是原来 --exclude 'cpt/web/app.py' 想压掉的东西；但排除整个文件会连带
#     隐藏 app.py 里对别处符号的**真实使用**，故改为逐条白名单，不再排除文件）
# --------------------------------------------------------------------------- #
_.do_GET  # cpt/web/app.py:74
_.do_POST  # cpt/web/app.py:379
_.do_DELETE  # cpt/web/app.py:384
_.log_message  # cpt/web/app.py:389
format  # cpt/web/app.py:389 —— log_message(self, format, *args) 的签名形参，基类要求

# --------------------------------------------------------------------------- #
# 2. 编译期断言：位于 `if TYPE_CHECKING:` 块内，只有 mypy 会读，运行时不存在
# --------------------------------------------------------------------------- #
_conforms_barlike  # cpt/domain/recursion.py:138

# --------------------------------------------------------------------------- #
# 3. 注解字段 / dataclass 字段：vulture 对「只有类型注解的类属性」的已知局限
#    （它们由 dataclass 生成 __init__，运行时确有使用）
# --------------------------------------------------------------------------- #
cont_days_em  # cpt/adapters/a_share_pool.py
pool_type  # cpt/adapters/a_share_pool.py
use_fx_qy_middle  # cpt/adapters/reference_chanlun.py
use_fx_qj_ck  # cpt/adapters/reference_chanlun.py
use_bi_type_new  # cpt/adapters/reference_chanlun.py
fixed_commit  # cpt/adapters/reference_chanlun.py
level_map  # cpt/adapters/reference_chanlun.py
role  # cpt/adapters/source_registry.py
note  # cpt/adapters/source_registry.py
latency_ms  # cpt/adapters/source_registry.py
contain_direction  # cpt/domain/config.py
zs_level_count  # cpt/domain/config.py
divergence_compare  # cpt/domain/config.py
first_seen_at  # cpt/domain/models.py
confirmed_at  # cpt/domain/models.py
invalidated_at  # cpt/domain/models.py

# --------------------------------------------------------------------------- #
# 4. 待接线模块（**保留**，非死代码）：产品位置与接线目标逐条见
#    `docs/pending-wiring.md`。接线后请把对应名字从本文件与那份清单同时移除。
# --------------------------------------------------------------------------- #
# 4a. roadmap 功能对应的 dashboard 服务
compare_snapshots  # dashboard_compare —— R5 两份 snapshot 字段级 diff
event_audit  # dashboard_event_audit —— Phase 5 P1 事件前后状态对比
slice_snapshot  # dashboard_export —— Phase 6 P2 时间范围切片导出
level_tree  # dashboard_levels —— Phase 5 P1 级别递归树
market_snapshot  # dashboard_market_fetch —— Phase 3 P0 真实 24h
align_runs  # dashboard_multi_run —— Phase 6 P2 双数据集同步对比
build_parity_snapshot  # dashboard_parity —— Phase 2 Oracle 对比
quality_report  # dashboard_quality —— Phase 5 P1 数据质量报告
realtime_update  # dashboard_realtime —— Phase 4 P1 高效实时更新
build_run_index  # dashboard_runs —— R1 数据集/运行浏览器
signal_history  # dashboard_signal_history —— Phase 4 P1 信号历史列表
signal_statistics  # dashboard_stats —— Phase 6 P2 一买统计
watch_metrics  # dashboard_watch —— Phase 4 P1 reconnect/stale
watchlist_rows  # dashboard_watchlist —— Phase 4 P1 多交易对
# 4b. A 股信号层（现行台账 `CPT-总计划-2026-09-24.md` 在范围内）
fetch_daily_tags  # domain/a_share_rules —— C2 涨跌停 / C3 停牌标签
t_plus_one_purchase_allowed  # domain/a_share_rules —— C4 T+1
# 4c. Wind 复权因子接入（台账决策 C1「复权因子 backfill 走 Wind」）
_to_wind_code  # adapters/a_share_local —— 000002 → 000002.SZ 代码转换
fetch_adjust_factors  # adapters/wind_source —— Wind 复权因子取数

# --------------------------------------------------------------------------- #
# 5. 仅测试使用的有意公开 API：无生产调用方，但是明确的窄接口，由测试直接驱动
# --------------------------------------------------------------------------- #
fetch_validated_bars  # adapters/a_share_local —— 取数 + 校验的组合入口
call_count  # adapters/wind_source —— 调用计数，供测试断言节流行为
