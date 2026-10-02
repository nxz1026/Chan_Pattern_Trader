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
event_type  # cpt/domain/models.py —— StructureEvent dataclass 字段
revision  # cpt/domain/models.py —— StructureEvent dataclass 字段
occurred_at  # cpt/domain/models.py —— StructureEvent dataclass 字段

# --------------------------------------------------------------------------- #
# 4. 待接线符号与门禁盲区（**保留**，非死代码）：产品位置与接线目标逐条见
#    `docs/pending-wiring.md`。接线后请把对应名字从本文件与那份清单同时移除。
# --------------------------------------------------------------------------- #
# 4a（R22 已清空）. roadmap 功能对应的 9 个 dashboard 服务全部于 R22 接线：
# compare_snapshots / slice_snapshot / level_tree / align_runs / quality_report /
# realtime_update / signal_statistics / watch_metrics / watchlist_rows
# 已从本文件移除，逐个接线点见 `docs/pending-wiring.md` 的 R22 记录。
# 4b. A 股信号层
# `fetch_daily_tags` / `apply_ashare_tags_to_bis` / `AShareDailyTag` 已于 R19 接进
# A 股主看板（adapters/a_share_local.py::AShareLocalClient.fetch_daily_tags →
# application/a_share_snapshot.py::_apply_daily_tags），故不再豁免。
t_plus_one_purchase_allowed  # domain/a_share_rules —— C4 T+1（待 signal 桥接线）
# 4c. **门禁盲区**（不是待接线）：调用方在 `scripts/factor_backfill.py`，而 CI 的
#     vulture 只扫 `cpt/`（`.github/workflows/ci.yml` → `vulture --min-confidence
#     60 cpt whitelist.py`），所以这两个**已接线**的符号仍被报「未使用」。
#     R22 接线点：`scripts/factor_backfill.py` 的 `--wind-fallback` 兜底路径
#     （默认关闭）。
#     要摘掉这两条豁免，得先把 vulture 的扫描范围扩到 `scripts/`；实测那样做会
#     另带出 2 条真死代码（`REPO_ROOT` 未使用变量、`latest_factor_date` 未使用
#     函数），属本次范围外，故不动门禁范围。
_to_wind_code  # adapters/a_share_local —— Wind 代码转换（调用方在 scripts/）
fetch_adjust_factors  # adapters/wind_source —— Wind 复权因子取数（调用方在 scripts/）

# --------------------------------------------------------------------------- #
# 5. 仅测试使用的有意公开 API：无生产调用方，但是明确的窄接口，由测试直接驱动
# --------------------------------------------------------------------------- #
fetch_validated_bars  # adapters/a_share_local —— 取数 + 校验的组合入口
call_count  # adapters/wind_source —— 调用计数，供测试断言节流行为
clear_runs  # application/dashboard_runs —— 清空进程内运行环形缓冲（测试用）
drain  # llm/queue —— 等异步队列排空（测试同步用；vulture 只扫 cpt/，看不见 tests/）

# R37：重算因子的两个新入口 —— 都在 scripts/factor_recompute.py 里被调用，
# 而 vulture 只扫 cpt/，所以它们在门禁眼里是死的（实际不是）。
fetch_corporate_actions  # adapters/wind_source 供 scripts/factor_recompute.py 取公司行动
hot_pool_codes  # adapters/a_share_factor 供 scripts/factor_recompute.py 排优先级
