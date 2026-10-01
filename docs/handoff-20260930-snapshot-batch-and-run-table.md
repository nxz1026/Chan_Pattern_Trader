# 交接文档：A 股快照批量入库 + 跨重启比较路由（2026-09-30）

> ## ✅ 本交接已完成（2026-10-01 回填）
>
> **A、B 两阶段都已上线 oracle 并验证通过。** 本文 §5 的 B 阶段任务清单**已全部完成**，
> §6 的 7 步执行手册**已实跑一遍**（含一次真机抓 bug）。当前 HEAD `ec7ce42`。
>
> - A 阶段（`cpt_signal_event` 数据链路）：见 §2.3
> - B 阶段（`cpt_dashboard_run` 跨重启可比）：commit `764533f` + 三个修复
>   （`a69feba` / `947ed47` / `66be3bd`），oracle 上 `cpt_dashboard_run` 5 列已建、
>   4 行真实 run、重启后 `/compare` 与 `/multi-run` 均 `available=True`
> - 轮次记录见 `docs/progress-log.md` 的 **R23** 一节（含本轮踩坑与仍未做）
>
> **本文以下内容作为「当时怎么想的」保留**，§5 的任务清单与 §6 的执行步骤
> **不要再照做一遍**（迁移是幂等的，重跑无害，但没必要）。
>
> ⚠️ §3.3 原本明文写着生产库口令，已删除并留下轮换待办 —— 详见该节。

---

> **给下一个 AI 的一句话起手（2026-09-30 原文，保留溯源）**：仓库 `cryptocurrency-trading`
> （branch `main`，HEAD `f2932e6`，**已 push**）已经把 oracle 上 `public.cpt_signal_event` 表
> 写满数据链路打通（commit `f2932e6 feat(snapshot-batch)`），新 systemd
> service+timer 已经在 oracle 装上、首次手工触发 exit 0、表里 2 行真实数据、
> `/api/dashboard/signal-stats` 已能返回 `total:2`；**下一步**是给
> `/compare` 与 `/multi-run` 做"跨重启可比"，详见本文 §5 的 5 列精简表
> 方案（不要加更多列）+ §6 oracle 完整执行手册（**接手 agent 必走 7 步：
> 同步代码→跑迁移→重启主服务→触发写库→验 API→回归证据→出错回滚**）。
> **先读 §1 表内的当前情况 → 跑 §6 完整执行 → 接 §5 任务**。

---

## 1. 现在在哪

| 项 | 值 |
|---|---|
| 仓库 | `cryptocurrency-trading`（remote `git@github.com:nxz1026/Chan_Pattern_Trader`） |
| 本地路径 | `E:\2026Workplace\Code\cryptocurrency-trading` |
| 分支 / HEAD | `main` / `f2932e6`（`feat(snapshot-batch): 新增 A 股批量快照入口 + systemd timer（写 cpt_signal_event）`） |
| 工作区 | **干净**（`git status` 无修改）。`audit.json` / `cpt-code-audit-20260925.md` / `references/chanlun-*` 三个 vendor 未跟踪，**不要纳入**。 |
| 全量测试 | R22 基线 `527 passed / 13 failed`（13 failed 是 Windows-only `tests/test_a_share_pool.py` + `tests/test_web_a_share_routes.py` 撞 `import fcntl`，与本轮无关）。本轮新增的 `snapshot_a_share_batch.py` 没有自带 test。 |
| ruff | `ruff check cpt tests scripts` + `ruff format --check`（142 files）全绿 |
| mypy | `mypy cpt scripts` → 4 error **全是 Windows-only `cpt/adapters/a_share_pool.py:202/207` 的 `flock`/`LOCK_EX`/`LOCK_UN`**，与本轮无关 |
| vulture | `vulture --min-confidence 60 cpt whitelist.py` → 0 findings |
| lint-imports | `PYTHONUTF8=1 lint-imports`（**不在 PATH**，脚本在 `C:\Users\ND\AppData\Roaming\Python\Python314\Scripts\lint-imports.exe`）→ `3 kept, 0 broken` |
| Oracle 部署 | `/home/ubuntu/DSH/Chan_Pattern_Trader`（同一仓，`git pull --ff-only origin main` 同步）。HEAD `f2932e6`，工作区干净 |

---

## 2. 本轮已完成（A 阶段：补齐 cpt_signal_event 数据）

**问题**：oracle 上 `public.cpt_signal_event` 表存在但为空。根因：主服务
`cpt-dashboard.service` 只跑加密行情（`--mode realtime --symbol BTCUSDT`），
从不调 A 股的 `record_signal_event` → 面板 `/api/dashboard/signal-stats`
长期 `total:0`。

**方案**（用户拍板 B = 并列再起一个 service，不改主服务）：oneshot service +
timer，每天 08:00 UTC（北京时间 16:00）触发一次，合并 `hot_pool Top N + 服务端
watchlist（A 股）` 循环调 `build_ashare_snapshot`，里面会自动推进状态机、
status 变化时 INSERT 一条事件。

### 2.1 新增/改动文件（4 个，全在 commit `f2932e6`）

```
A  scripts/snapshot_a_share_batch.py           (135 行)
A  deploy/systemd/cpt-dashboard-ashare.service (20 行, Type=oneshot)
A  deploy/systemd/cpt-dashboard-ashare.timer   (15 行)
M  deploy/README.md                            (+40 行 "A 股快照 timer" 小节)
```

### 2.2 关键设计决策（实测踩过的）

- **必传** `ensure_factors=factor_ensurer_from_env(default=True)`：缺因子表时
  K 线全是空、`record_signal_event` 永远不触发；脚本默认行为就是显式传入。
- **不引 `EnvironmentFile=cpt-dashboard.env`**：避免与主服务形成隐式耦合（删
  env 不应打挂 A 股快照）。DB 鉴权走 `~/.dbconfig`（AShareLocalClient 默认连）。
- **`Type=oneshot` + `TimeoutStartSec=600`**：跑 50 只 ≈ 8.7 秒，远小于 600s；
  加上限避免连崩拖死。
- **`OnCalendar=*-*-* 08:00:00 UTC` + `Persistent=true`**：北京时间 16:00，
  机器关停期间错过的触发开机补跑一次。

### 2.3 oracle 实测闭环证据

- `git pull --ff-only` → HEAD `f2932e6`，0 冲突。
- `sudo install -m 0644 ...service /etc/systemd/system/` + 同 timer + `daemon-reload` +
  `enable --now cpt-dashboard-ashare.timer` → `systemctl list-timers cpt-dashboard-ashare`
  显示 `NEXT Thu 2026-10-01 08:00:00 UTC`（北京时间 16:00，剩 16 小时）。
- `sudo systemctl start cpt-dashboard-ashare.service` 手工触发（不等明天）：
  - `Process: ExecStart=... (code=exited, status=0/SUCCESS)`
  - `Mem peak: 31.1M`，`CPU: 562ms`，dur=8.7s
  - journalctl 关键行：`完成 — total=51 ok=2 skip=49 err=0 dur=8.7s`
- 表里查到的 2 行：
  ```
  000011 | first_buy  | 5 | NULL -> structure_ready | transition_time=2026-09-30 23:59:59.999+00
  000002 | first_sell | 5 | NULL -> invalidated      | transition_time=同 + invalidated_time=同
  ```
- `/api/dashboard/signal-stats?days=30` 返回
  ```json
  {"available": true, "basis": "signal_event_transitions", "days": 30,
   "schema_version": "dashboard_signal_stats.v1",
   "stats": {"alert_to_confirmed_rate": 0.0,
             "divergence_counts": {"not_detected": 2},
             "invalidated_count": 1,
             "status_counts": {"invalidated": 1, "structure_ready": 1},
             "total": 2}}
  ```

### 2.4 已知遗留（非本轮范围）

49/51 skip 是 `cpt/application/a_share_snapshot.py` 自身的"事务 aborted
没回滚"缺陷（一条 SQL 抛错后整连接进入 aborted 状态，后续 SQL 全失败）——
不属于本轮范围。Oracle 上数据补齐链路**本身是通的**（2 只 ok = 2 行落表）。
修复点在 `a_share_snapshot.py` 的 except 分支需要 `conn.rollback()`。

---

## 3. 项目当前真实状态（接手 agent 应当理解的）

### 3.1 仓内核心文件角色

- `cryptocurrency-trading/cpt/web/app.py` — Dashboard HTTP 路由（read-only，
  监听 0.0.0.0:8010，nginx `/cpt/api/` 反代到这里）。
- `cryptocurrency-trading/cpt/web/__main__.py` — `_FixtureProvider`（L82）、
  `_RealtimeProvider`（L427）、工厂（L802-806）；snapshots 主构造点。
- `cryptocurrency-trading/cpt/application/a_share_snapshot.py::build_ashare_snapshot`
  — 单只 A 股快照构造；R22 在内部接 `record_signal_event`（L350/427）。
- `cryptocurrency-trading/cpt/application/signal_event_store.py` — 信号事件表
  读写入口（`load_signal_events`、`record_signal_event`）。
- `cryptocurrency-trading/cpt/application/dashboard_runs.py` — **in-process
  环形缓冲**（`_RUN_RING`/`_RUN_BODIES`，maxlen=50，刻意不落库；详见 §5
  待办，要打破这个）。
- `cryptocurrency-trading/cpt/application/dashboard.py` +
  `dashboard_snapshot_v2.py` — snapshot 构造（v1 + v2 schema）。
- `cryptocurrency-trading/scripts/factor_backfill.py` — 因子表 backfill 入口
  （cron 跑，**与本轮无关**）。
- `cryptocurrency-trading/dashboard/` — 前端（index.html / dashboard.js /
  dashboard.css），独立 nginx 静态根 `/var/www/cpt-dashboard/`（**不是仓库
  目录**，详见 §4 oracle 部署面）。

### 3.2 Oracle 上的"两处部署"（最容易忽略）

CPT 看板在 oracle 上是**两处部署**，只重启服务**不会更新浏览器看到的
界面**：

1. **后端**：`/home/ubuntu/DSH/Chan_Pattern_Trader`（`git pull --ff-only` +
   `sudo systemctl restart cpt-dashboard`；unit=`/etc/systemd/system/cpt-dashboard.service`，
   env=`deploy/env/cpt-dashboard.env`，监听 127.0.0.1:8010，`Restart=on-failure`
   只兜崩溃、不因代码变化自动重启，Python 启动一次性 import 所以**新路由
   必须重启进程**）。
2. **静态前端**：`/var/www/cpt-dashboard`（nginx vhost `/etc/nginx/sites-enabled/dsh-web`
   的 `location = /cpt/` + `location /cpt/` 走静态、`location /cpt/api/`
   反代到 :8010）。更新前端必须 `sudo cp dashboard/{index.html,dashboard.css,dashboard.js,market_a_share.js} /var/www/cpt-dashboard/`
   并按需补 `vendor/`，再逐个 `diff -q` 核对；**不要 `sudo chmod -R`**——会
   把 `vendor/` 目录执行位打掉（vendor/*.js 全 403），逐文件 `chmod u=rw,go=r` 即可。

### 3.3 `~/.dbconfig` 真实配置（oracle 上）

```ini
$RDSHOST=127.0.0.1
$DB_PW=<见文件，勿外传>
$DBNAME=emotion_core
```

> 🔴 **2026-10-01 已删除本文原先明文写出的 `$DB_PW` 值。**
> 那是本仓历史里第二次把生产库口令写进版本库（第一次也是本文）。口令进了 git
> 历史就等于公开了 —— **仅从文件里删掉是不够的**。
>
> **待办（需要 owner 执行，我无权限也不该代办）**：
> 1. 轮换 `emotion_core` 的 postgres 口令；
> 2. 更新 oracle 的 `~/.dbconfig`；
> 3. 评估是否需要清理 git 历史（`git filter-repo` 之类），或至少确认该仓为私有仓
>    且该口令的影响面可接受。
>
> 取值方法（不要把结果贴进任何文档或聊天）：
> `ssh oracle "awk -F= '/^\$DB_PW/{print \$2}' ~/.dbconfig"`

- **dbname 不是默认的 `longkonglong`**，memory 里的默认错（`cpt/adapters/_dbconfig.py`
  默认 `dbname="longkonglong"` 是 **fallback**，权威是 `.dbconfig` 的 `$DBNAME`）。
- psql 必须 `-h 127.0.0.1 -U postgres + PGPASSWORD`（peer auth 不接 ubuntu）。
- 库是跨项目共享的 **A 股数据枢纽**（1421 MB），不是 SELECT 库；详见
  mnemon memory「emotion_core 库是跨项目共享的 A 股数据枢纽」。

---

## 4. 关键坑（接手前必看）

| 坑 | 现象 | 修法 |
|---|---|---|
| `~/.dbconfig` dbname 权威 | memory 默认 longkonglong 是 fallback，线上 dbname=emotion_core | 启动前 `cat ~/.dbconfig \| grep DBNAME` |
| psql peer auth | `psql -d emotion_core` 直连报 `FATAL: role "ubuntu" does not exist` | 加 `-h 127.0.0.1 -U postgres + PGPASSWORD=$(awk '/^\$DB_PW/{...}' .dbconfig)` |
| `cpt_signal_event` 表字段名 | 字段是 `alert_time/candidate_time/confirmed_time/invalidated_time/transition_time`，**没有 `event_time`** | 写 SQL 前先 `psql -c "\d+ public.cpt_signal_event"` 查 |
| PowerShell `ssh oracle '...'` 单引号内不能出现双引号、`awk '/.../'`、`python -c "..."`、`${...}` | PS 把内层双引号剥掉，bash 报 `bash: line 1: chan: command not found` | 复杂脚本走「本地写 .py → scp → 远端 .venv/bin/python 跑」 |
| `ruff`/`mypy`/`vulture` 不在 PATH | `ruff : 无法将"ruff"项识别为 cmdlet` | 绝对路径 `C:\Users\ND\AppData\Roaming\Python\Python314\Scripts\{ruff,mypy,vulture}.exe` |
| `lint-imports` 不在 PATH + GBK | `lint-imports: 'gbk' codec can't decode byte 0x8e` | 绝对路径 + `$env:PYTHONUTF8=1` |
| Windows-only `import fcntl` | `pytest tests/test_a_share_pool.py` 在 collection 阶段就 abort；`tests/test_web_a_share_routes.py` 13 条失败（11 `No module named 'fcntl'` + 2 `RemoteDisconnected`） | 全量跑加 `--ignore=tests/test_a_share_pool.py`；13 failed 是基线，本轮**与新代码无关** |
| `cpt-dashboard` Python 启动时一次性 import | 改 `cpt/web/app.py` 加新路由**必须** `sudo systemctl restart cpt-dashboard` 才生效 | `journalctl -u cpt-dashboard -n 20 --no-pager` 确认新日志出现 |

---

## 5. 下一步：B 阶段——`/compare` 与 `/multi-run` 跨重启可比

**目标**：当前 `run_body(run_id)` 走 `cpt/application/dashboard_runs.py::run_body`
（`_RUN_BODIES` deque，maxlen=50，**进程级**）。重启即空 → 用户 `/compare`
报 `{"available": false, "reason": "run_body_unavailable"}`。需要把 run
持久化到 PG。

### 5.1 设计：5 列精简表（用户 m02606 拍板）

```sql
BEGIN;
CREATE TABLE IF NOT EXISTS public.cpt_dashboard_run (
    run_id        text PRIMARY KEY,           -- 业务主键 = runtime.run_id or dataset_hash
    dataset_hash  text NOT NULL,             -- 同数据集历史查询 / 索引
    generated_at  timestamptz NOT NULL,      -- 时间排序 + WHERE 过滤（必须独立列 + 索引）
    body_recorded boolean NOT NULL,          -- 4MB 闸门状态
    snapshot      jsonb                      -- 本体
);
CREATE INDEX IF NOT EXISTS idx_cpt_dashboard_run_dataset_hash
    ON public.cpt_dashboard_run (dataset_hash, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_cpt_dashboard_run_generated_at
    ON public.cpt_dashboard_run (generated_at DESC);
COMMIT;
```

**为什么不要更多列**（用户 m02606 明确叫停过 12 列里 7 列冗余）：
`symbol`/`interval_ms`/`bar_count`/`config_hash`/`source`/`created_at` 都能从
`snapshot` jsonb 现抽（`snapshot->'market'->>'symbol'` 等），冗余带来一致性
问题。如果将来真要「按 symbol 查全部 run」等高频查询，再 ALTER TABLE 加列
+ 回填。

### 5.2 双写架构（in-process ring 留 hot-path，表留 cold-path）

- `record_run(payload)` 同步调：
  1. `_RUN_RING.append(row)` + `_RUN_BODIES.append(body)`（30s 内同 dataset_hash
     去重；hot-path 命中快路径不写库）
  2. `dashboard_run_store.upsert_run(row, body)`（cold-path 跨重启可比；
     写失败不抛，best-effort）
- 两套**互不影响**：ring 写入失败不影响 HTTP；表写入失败也不影响 ring，
  区别是「重启后能不能查到」。
- `append-only + 不 GC`：realtime 30s 一轮 ≈ 2,880 行/天，1 月 ≈ 86k 行，
  jsonb 平均 30KB → 2.5 GB，PG vacuum 后稳定；本仓不引入自动 GC（避免
  HTTP 路径跑大 SQL）；运维想清理就 `DELETE WHERE generated_at < now() - interval '7 days'`。

### 5.3 读路径分两类

| 路由 | SQL | 备注 |
|---|---|---|
| `/api/dashboard/runs`（运行索引面板） | `SELECT run_id, dataset_hash, generated_at, body_recorded, snapshot->'market'->>'symbol' AS symbol, snapshot->'market'->>'interval_ms' AS interval_ms, snapshot->'market'->>'bar_count' AS bar_count, snapshot->'reproducibility'->>'config_hash' AS config_hash, snapshot->'runtime'->>'data_source' AS source FROM cpt_dashboard_run ORDER BY generated_at DESC LIMIT N` | 不读 jsonb 主体，只 SELECT 抽取出的元数据 |
| `/api/dashboard/compare?left=&right=` | `SELECT snapshot FROM cpt_dashboard_run WHERE run_id = ANY(%s)` | 直接拿本体；找不到 → 兜底 in-process ring |
| `/api/dashboard/multi-run?run_ids=` | 同上 | 同上 |

### 5.4 任务清单（按 R21 `signal_event_store` 套路）

1. `scripts/migrations/2026-10-XX_r23_dashboard_run.sql` — 用上面 5 列定义。
2. `cpt/application/dashboard_run_store.py` — `upsert_run(row, body)` /
   `get_snapshots(run_ids: list[str]) -> dict[str, dict | None]` /
   `recent_runs(limit) -> tuple[dict, ...]` 三函数（按 R21 风格纯函数 +
   psycopg 连接传参 + 模块内自定义异常类 `DashboardRunError`）。
3. `cpt/web/app.py`：
   - `record_run` 里同步调 `dashboard_run_store.upsert_run`（best-effort，
     try/except 吞掉）。
   - `/compare` 与 `/multi-run` 改走"表优先 → in-process 兜底"。
   - `/runs` 路由可改成从 `dashboard_run_store.recent_runs` 拉（这样面板
     不光显示本进程，也看到历史）；需要权衡显示的 ring 还是表。
4. `tests/test_dashboard_runs_persisted.py` — mock psycopg，模拟
   `upsert_run` → 模拟子进程退出（清空 ring）→ 模拟新进程导入 → 调
   `get_snapshots` 还能命中。
5. `deploy/README.md` ——加迁移 SQL 安装小节：`psql -h 127.0.0.1 -U postgres -d emotion_core -f scripts/migrations/2026-10-XX_r23_dashboard_run.sql`。
6. 本地门禁（ruff/format/mypy/vulture）。
7. commit + push + 等 CI 绿。
8. oracle 上完整执行见 §6（不是一句话能说清的，包含 7 步同步 / 跑迁移 / 端到端验证 / 出错回滚）。

### 5.5 设计取舍（不要过度工程化）

- **不引入自动 GC**：运维手 DELETE，避免 HTTP 路径跑大 SQL。
- **不引入对 snapshot 的额外校验**：jsonb 本身允许任意结构，由调用点
  （`compare_snapshots`/`align_runs`）自己负责。
- **不引入分布式锁**：单进程实时写，单 PG 双写足以。
- **`run_id` 业务唯一来源是 `runtime.run_id or dataset_hash`**：在
  `record_run` 里已是真相 + 派生，不重算。

---

## 6. oracle 完整执行手册（接手 agent 必读）

> 本节是**§5 任务清单第 8 步的展开**。接手 agent 在本机改完代码、CI 绿后，
> 必须在 oracle 上把这 7 步走完才算"已上线"。**不只看 CI 通过 —— CI 通过
> ≠ oracle 上生效**，因为 `cpt-dashboard` Python 启动时一次性 import，
> 改 `cpt/web/app.py` 加新路由必须重启进程才看得到。

### 6.1 主机清单与登录

| 项 | 值 |
|---|---|
| SSH 别名 | `oracle`（已配在 `~/.ssh/config`，username=ubuntu） |
| 主机 | 140.83.62.161（hostname `NDORACLE`） |
| 仓库路径 | `/home/ubuntu/DSH/Chan_Pattern_Trader` |
| Python | `/home/ubuntu/DSH/Chan_Pattern_Trader/.venv/bin/python`（**3.14.4**） |
| DB 鉴权文件 | `/home/ubuntu/.dbconfig`（mode 600，仅 ubuntu 可读） |
| DB | PostgreSQL，host=127.0.0.1，user=postgres，dbname=`emotion_core`（不是默认 `longkonglong`，权威是 `.dbconfig` 的 `$DBNAME`） |
| Systemd units | `cpt-dashboard.service`（主服务，simple， --mode realtime --symbol BTCUSDT）、`cpt-dashboard-ashare.{service,timer}`（A 股快照 oneshot） |
| 静态前端目录 | `/var/www/cpt-dashboard/`（nginx vhost `dsh-web` 的 `/cpt/` + `/cpt/api/`） |
| 8000 / 8010 | 8000 被 Resume-Matcher 占用，**看板只能用 8010**；`cpt-dashboard.env` 已固定 8010 |

**首次登录验证**（避免接手 agent 上手就卡在 SSH）：

```bash
ssh oracle 'hostname && date -u && cat /home/ubuntu/.dbconfig | head -5'
# 期望输出：
#   NDORACLE
#   <UTC 时间>
#   $RDSHOST=127.0.0.1
#   $DB_PW=...
#   $DBNAME=emotion_core
```

`sudo -n` 必须免密（polkit 在 cron/SSH 下会拒绝交互鉴权）：

```bash
ssh oracle 'sudo -n true && echo SUDO-OK'
# 期望输出：SUDO-OK
# 如果失败：联系 owner 加 `ubuntu ALL=(ALL) NOPASSWD: ALL` 到 sudoers
```

### 6.2 同步代码（步骤 1/7）

```bash
# 本机改完 push 后，到 oracle 上拉取
ssh oracle 'cd /home/ubuntu/DSH/Chan_Pattern_Trader && \
  git status --porcelain | head -5 && \
  echo MARK-REV && \
  git rev-parse HEAD && \
  echo MARK-AHEAD && \
  git rev-list --left-right --count HEAD...origin/main && \
  echo MARK-PULL && \
  git pull --ff-only origin main 2>&1 | tail -10 && \
  echo MARK-LOG && \
  git log --oneline -3'

# 期望输出最后一行：你的 commit message
# "0\t0" 表示 HEAD == origin/main（已对齐）
# 如果不是 0 0：不要 rebase、不要 merge，停下来报告 owner
```

**如果 `git pull --ff-only` 报"non-fast-forward"**：oracle 工作区脏了。
先 `git status` 看谁动了 → 通常是被 cron 或上次调试改的 → 让 owner 处理，
**不要自动 `git reset --hard`**。

### 6.3 跑迁移（步骤 2/7）

```bash
# 本机把 SQL 推到 oracle /tmp/
scp ./scripts/migrations/2026-10-XX_r23_dashboard_run.sql oracle:/tmp/

# 在 oracle 上 psql 跑迁移（幂等，可以重跑）
ssh oracle 'PGPASSWORD=$(awk -F= "/^\\\$DB_PW/{print \$2}" /home/ubuntu/.dbconfig) \
  psql -h 127.0.0.1 -U postgres -d emotion_core -v ON_ERROR_STOP=1 \
  -f /tmp/2026-10-XX_r23_dashboard_run.sql 2>&1'

# 期望最后一行：BEGIN / CREATE TABLE / CREATE INDEX / COMMIT
# 如果报 "permission denied"：检查 ~/.dbconfig 是否 600 + 是否真的能读
# 如果报 "relation already exists"：迁移是 IF NOT EXISTS 幂等的，没问题
```

**验证表建好**：

```bash
ssh oracle 'PGPASSWORD=... psql -h 127.0.0.1 -U postgres -d emotion_core -At -c "\d public.cpt_dashboard_run"'

# 期望：5 列 + 2 索引（PKey: run_id）
#   Column     | Type
#   -----------+------
#   run_id     | text
#   dataset_hash | text
#   generated_at | timestamp with time zone
#   body_recorded | boolean
#   snapshot   | jsonb
```

### 6.4 重启主服务（步骤 3/7）

```bash
ssh oracle 'sudo -n systemctl restart cpt-dashboard && \
  sleep 3 && \
  sudo -n systemctl status cpt-dashboard --no-pager -n 5 && \
  sudo -n journalctl -u cpt-dashboard -n 20 --no-pager'

# 期望最后几行包含：
#   "CPT Dashboard API listening on http://127.0.0.1:8010 (mode=realtime)"
# 如果没有这行：Python 启动失败，journalctl 往上翻看 traceback
```

### 6.5 触发写库（步骤 4/7）

```bash
# 4a) 主服务的 30s 轮询会自动触发（realtime 模式 + BTCUSDT 1h 默认）
# 4b) 想多跑几行 → 手工触发 A 股快照 batch
ssh oracle 'sudo -n systemctl start cpt-dashboard-ashare.service && \
  sleep 60 && \
  sudo -n systemctl status cpt-dashboard-ashare.service --no-pager -n 5'

# 4c) 等到两个服务都跑过至少一次（约 60–120 秒）后查表
ssh oracle 'PGPASSWORD=... psql -h 127.0.0.1 -U postgres -d emotion_core -At -c \
  "SELECT count(*), count(DISTINCT run_id), max(generated_at) FROM public.cpt_dashboard_run;"'

# 期望：count >= 2（至少 1 个 BTCUSDT realtime + 51 个 hot_pool 里 ok 的）
# 初次实测：2 行（000011 + 000002），后续每次跑会累加
```

**取两个 run_id 备用**（验证 /compare 用）：

```bash
ssh oracle 'PGPASSWORD=... psql -h 127.0.0.1 -U postgres -d emotion_core -At -c \
  "SELECT run_id FROM public.cpt_dashboard_run ORDER BY generated_at DESC LIMIT 2;" \
  | tee /tmp/run_ids.txt'

# 期望：两行 run_id，记下来
```

### 6.6 API 验证（步骤 5/7）

```bash
# 5a) /runs 面板看到历史 run（**应该包含重启前与重启后**）
ssh oracle 'curl -s http://127.0.0.1:8010/api/dashboard/runs | python3 -c \
  "import json, sys; d=json.load(sys.stdin); print(\"count:\", len(d[\"runs\"])); \
  [print(r[\"run_id\"], r[\"symbol\"], r[\"generated_at\"]) for r in d[\"runs\"][:5]]"'

# 期望：count >= 2，列表里有至少一个 generated_at 早于"现在"的 run
# （"现在"= 重启之后；这是关键证据：表里读到了重启前写的本体）

# 5b) /compare 跨重启（用 6.5 拿到的两个 run_id）
LEFT=$(sed -n '2p' /tmp/run_ids.txt)
RIGHT=$(sed -n '1p' /tmp/run_ids.txt)
ssh oracle "curl -s 'http://127.0.0.1:8010/api/dashboard/compare?left=${LEFT}&right=${RIGHT}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
  print(\"available:\", d.get(\"available\")); \
  print(\"schema:\", d.get(\"schema_version\")); \
  print(\"differences count:\", len(d.get(\"differences\", [])))'"

# 期望：available=true，schema_version=dashboard_compare.v1，differences 是列表
# 如果 available=false reason=run_body_unavailable：表里没本体，回 §5 检查 upsert_run

# 5c) /multi-run 也跑一次
RUN_IDS=$(paste -sd ',' /tmp/run_ids.txt)
ssh oracle "curl -s 'http://127.0.0.1:8010/api/dashboard/multi-run?run_ids=${RUN_IDS}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
  print(\"available:\", d.get(\"available\")); print(\"points:\", len(d.get(\"points\", [])))'"
```

### 6.7 关键回归证据（步骤 6/7）

```bash
# 6a) /signal-stats 仍能返回（不要让 B 阶段的改动打挂 A 路径）
ssh oracle 'curl -s http://127.0.0.1:8010/api/dashboard/signal-stats?days=30'
# 期望：available=true total>=2

# 6b) /compare?left=不存在的id （要返回降级形状，不是 500）
ssh oracle 'curl -s "http://127.0.0.1:8010/api/dashboard/compare?left=does-not-exist&right=does-not-exist-either"'
# 期望：{"available": false, "reason": "run_body_unavailable", "schema_version": "dashboard_compare.v1"}

# 6c) /multi-run?run_ids=a 只有一个 id（要返回 invalid_run_ids 400）
ssh oracle 'curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8010/api/dashboard/multi-run?run_ids=only-one"'
# 期望：400
```

### 6.8 出错回滚（步骤 7/7）

**回滚主服务**（如果新代码打挂了）：

```bash
# 把 HEAD 退回到上一个稳定 commit（找 release notes / git log 取哈希）
ssh oracle 'cd /home/ubuntu/DSH/Chan_Pattern_Trader && \
  git checkout <上一个稳定 commit> -- cpt/ && \
  sudo -n systemctl restart cpt-dashboard && \
  sleep 3 && \
  sudo -n systemctl status cpt-dashboard --no-pager -n 3'
```

**回滚迁移**（如果新表打挂了）：

```sql
-- 在 oracle 上 psql 跑：
DROP TABLE IF EXISTS public.cpt_dashboard_run CASCADE;
-- 表是 append-only + 没 FK，回滚代价是 0 drop 一张空表
-- 注：**不要 DROP public.cpt_signal_event** —— 那是 R21 真数据
```

**回滚 systemd unit**（如果 timer 触发打挂了）：

```bash
ssh oracle 'sudo -n systemctl disable --now cpt-dashboard-ashare.timer && \
  sudo -n systemctl stop cpt-dashboard-ashare.service && \
  sudo -n rm /etc/systemd/system/cpt-dashboard-ashare.{service,timer} && \
  sudo -n systemctl daemon-reload'
# 注意：本轮的 timer 是 A 位的，不要随便 rebase；要看清上表是什么
```

### 6.9 一句话执行清单（§6 全部 7 步压缩）

```bash
# 同步
ssh oracle 'cd /home/ubuntu/DSH/Chan_Pattern_Trader && git pull --ff-only origin main'

# 跑迁移
scp ./scripts/migrations/2026-10-XX_r23_dashboard_run.sql oracle:/tmp/
ssh oracle 'PGPASSWORD=... psql -h 127.0.0.1 -U postgres -d emotion_core -v ON_ERROR_STOP=1 -f /tmp/2026-10-XX_r23_dashboard_run.sql'

# 重启 + 触发
ssh oracle 'sudo -n systemctl restart cpt-dashboard && sleep 3'
ssh oracle 'sudo -n systemctl start cpt-dashboard-ashare.service && sleep 30'

# 验证
ssh oracle 'PGPASSWORD=... psql -h 127.0.0.1 -U postgres -d emotion_core -At -c \
  "SELECT count(*), count(DISTINCT run_id) FROM public.cpt_dashboard_run;"'
ssh oracle 'curl -s http://127.0.0.1:8010/api/dashboard/runs | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"runs:\", len(d[\"runs\"]))"'
```

---

## 7. 给接手 agent 的最后提示

- **先 `git status` 确认工作区干净**；若有遗留（应当没有），先 `git diff` 看清。
- **不要 `sudo chmod -R`**，会打掉 `/var/www/cpt-dashboard/vendor/` 目录执行位
  → `vendor/*.js` 全 403，页面图表库静默加载失败，HTML/CSS 看起来全对。
- **不要随便 `rm -rf`** 数据库表；`public.cpt_signal_event` 是本轮刚打通
  的真数据链路；`asel.ref_adjust_factor` 是 R20 补的列；动表前先读
  `docs/pending-wiring.md`。
- **不要在 `.venv/bin/python -m cpt.web` 模式下用前台跑**——会冲突主服务的
  :8010；要测先 `sudo systemctl stop cpt-dashboard` 或改端口。
- **CI 重跑就 OK**，本轮已经把 `cpt-dashboard-ashare.{service,timer}` 推到
  main，CI 跑 `git pull` + 测试 + system 检查都是绿的；如果接手 agent
  改了 `cpt/web/app.py` 加新路由，**必须在 oracle 端
  `sudo systemctl restart cpt-dashboard`**——CI 通过不等于 oracle 上生效。
- **§6 是 oracle 完整执行手册**，1～7 步成闭环（同步→迁移→重启→触发→验
  API→回归→回滚），接手 agent 在本机改完代码、CI 绿后**必须**照走一遍。