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
# 4c. ~~门禁盲区~~ **R57 已消解**：vulture 的扫描范围已扩到 `scripts/`，
#     所以下面这几个「调用方在 scripts/」的符号不再被误判为死代码，
#     豁免随之撤销 —— 保留它们等于让白名单掩盖未来的真死代码。
#     （扩范围时确实如旧注释预言地多带出 2 条**真**死代码：
#      `factor_backfill.py` 的 `REPO_ROOT` 与 `latest_factor_date` —— 已删。）

# --------------------------------------------------------------------------- #
# 5. 仅测试使用的有意公开 API：无生产调用方，但是明确的窄接口，由测试直接驱动
# --------------------------------------------------------------------------- #
fetch_validated_bars  # adapters/a_share_local —— 取数 + 校验的组合入口
call_count  # adapters/wind_source —— 调用计数，供测试断言节流行为
clear_runs  # application/dashboard_runs —— 清空进程内运行环形缓冲（测试用）
drain  # llm/queue —— 等异步队列排空（测试同步用；vulture 只扫 cpt/，看不见 tests/）

# --------------------------------------------------------------------------- #
# 7. R57（2026-10-06 上线前审计）：**有意保留的已弃用函数**，不是漏删的死代码
# --------------------------------------------------------------------------- #
# `_read_prev_status`（application/a_share_snapshot.py）已被
# `_derive_first_buy_signal` / `_derive_first_sell_signal` 回传的 `prev_status`
# 取代 —— 那两个函数在写事件**之前**捕获旧状态；查库拿到的必然是本轮刚写的值。
# 它被保留，是为了让 `grep _read_prev_status` 能找到「为什么不能再查库」这段说明
# （a_share_snapshot.py 里另有两处 :func: 引用也指着它，删掉会变成死链）。
#
# ⚠️ **本条不是「忽略告警」**：它登记的是一次**明确的取舍**。
# 若将来不再需要这段说明，正确做法是删函数 + 同步改那两处 :func: 引用，
# 然后把本行删掉 —— 而不是在代码里留一个没人调用的函数装作还在用。
_read_prev_status  # application/a_share_snapshot —— 已弃用，保留仅为让 grep 找到说明
# --------------------------------------------------------------------------- #
# 7. R57（2026-10-06 上线前审计）：vulture 扫描范围扩到 `scripts/` 后的新增登记
# --------------------------------------------------------------------------- #
# 扩范围消掉了 6 条「调用方在 scripts/ 所以被误判」的旧豁免（见上方 4c 的撤销说明），
# 同时带出下面这些 —— **全部是工具的盲区，不是死代码**。
#
# 顺带一句：扩范围这件事本身就是上一条旧注释预言过的（「实测会另带出 2 条真死代码」）。
# 预言成真的那 2 条（`factor_backfill.py` 的 `REPO_ROOT` 与 `latest_factor_date`）
# 已删 —— **扩范围的收益不只是消误报，还有真的挖出死代码**。

#: 公开常量，声明「四种原因的全集」，本身不是死代码。
#: vulture 看不见 `Final` 常量的语义用途（它没有出现在任何调用点）。
CAUSES  # application/run_metric —— 四种 cause 的全集（data/config/backend/code）

#: `CodeReport` 是 **dataclass**，下面两个是它的**字段**：dataclass 会生成
#: `__init__` 并自动接收它们，vulture 对「只有类型注解的类属性」有已知局限
#: （与本文件第 3 节同一类）。而且它们**确实在用**：`factor_report.py:120`
#: 用关键字传入 `bars_new`，`:122` 给 `new_down_jumps` 赋值。
bars_new  # scripts/factor_report —— CodeReport 字段，:120 构造时传入
new_down_jumps  # scripts/factor_report —— CodeReport 字段，:122 显式赋值

#: 这不是未使用的变量，而是给**第三方对象**赋属性：
#: `conn` 是 psycopg 的连接，`autocommit` 是它的真属性。vulture 只看
#: 本模块的 AST，看不见外部类型有哪些属性 —— 于是任何 `obj.foo = x`
#: 都会被报成「unused attribute」。
autocommit  # scripts/verify_public_contracts —— psycopg 连接属性赋值，不是本地变量

# --------------------------------------------------------------------------- #
# 8. R59（2026-10-09 上线前审计修复）：HTTP 层三个「调用方在框架里 / 在 tests / 在门禁里」
# --------------------------------------------------------------------------- #
# 下面三条都是 vulture 的**扫描盲区**，不是死代码。逐条写明谁在用：
#
# ① `log_request`（web/app.py）覆写的是 stdlib `BaseHTTPRequestHandler` 的钩子，
#    由 `send_response()` 内部回调 —— 调用点不在本仓 AST 里。它就是 R59 H4 的
#    access log 落点（`_redact_path_for_log` 会先把 query 里的 token/api_key 抹成
#    `***`）。删掉它不会「清死代码」，只会让全站失去 access log。
log_request  # web/app.py —— 覆写 BaseHTTPRequestHandler 钩子（框架回调）

# ② `rejected_connections`（web/app.py）是并发上限的拒绝计数：`process_request`
#    抢不到信号量时 +1。读取方在测试里（`tests/test_web_audit_r59_hardening.py`
#    断言打满 32 个 worker 后该计数 == 1），而 vulture 只扫 `cpt/` 与 `scripts/`。
rejected_connections  # web/app.py —— 并发拒绝计数，由测试断言

# ③ `MAINTENANCE_PATH`（web/track_api.py）是**机器可读的路由声明**：
#    `route_for` 按 `p.split("/")` 段解析，代码里不需要再比对一遍字符串，
#    但 `scripts/check_doc_drift.py` 正是靠「带引号的字面量」判定
#    「文档写了、代码里没有」—— 没有这个常量，07:30 的 track 维护作业就会
#    被误判成幽灵接口（R59 实况）。它的引用点在 docstring 的 :data: 里。
MAINTENANCE_PATH  # web/track_api.py —— 路由字面量声明，供 check_doc_drift 识别

# ④ `daemon_threads` / `block_on_close`（web/app.py）同理：它们不是本地变量，
#    而是 `socketserver.ThreadingMixIn` / `BaseServer` 读的**类属性**。设成
#    True/False 的目的是「主线程退出时别等 worker、别阻塞在 close 上」，
#    调用方在 stdlib 内部（`process_request_thread` / `server_close`）。
daemon_threads  # web/app.py —— ThreadingMixIn 类属性（标准库读取）
block_on_close  # web/app.py —— BaseServer 类属性（标准库读取）
