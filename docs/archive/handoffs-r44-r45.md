# 交接记录合并本 · 2026-09-25 ~ 2026-10-03（R44/R45 收尾）

> ## 这是一份**合并后的历史记录**，不是活文档
>
> **来源文件**（5 份交接单，已 `git rm`，可从 tag `pre-docs-consolidation` 取回）：
>
> | 原文件 | 日期 | 主题 |
> |---|---|---|
> | `docs/handoff-20260925-leftover-fixes.md` | 2026-09-25 | 遗留问题修复 |
> | `docs/handoff-20260930-snapshot-batch-and-run-table.md` | 2026-09-30 | A 股快照批量入库 + `cpt_dashboard_run` |
> | `docs/handoff-20261003-factor-recompute.md` | 2026-10-03 | 因子重算与对账 |
> | `docs/handoff-20261003-r44-fix-and-cutover.md` | 2026-10-03 | §4 bug 已修，切表方案 |
> | `docs/handoff-20261003-r44-status-and-cutover.md` | 2026-10-03 | 阶段小结与切表方案 |
>
> **本文保留**：① **仍然在 force** 的口径与纪律；② 决策及其**理由**；
> ③ **仍然开放**的待办。**本文不保留**：逐轮进度播报、命令速查手册、
> 已经走完的执行步骤、「当时卡在哪」的现场描述。
>
> ⚠️ **本文不含任何行号**。这些交接单里的行号是当时的现场坐标，
> `cpt/web/app.py` 与 `cpt/adapters/a_share_local.py` 此后多次重构漂移数百行
> （R54 已确认「行号在不在」≠「行号指不指对那件事」）。
> **要定位请用符号名 `git grep`。**

---

## 1. 数据基线（**仍然有用**，但数字已变，见 §1.2）

### 1.1 表与库

| 项 | 值 |
|---|---|
| 生产库 | PostgreSQL，`emotion_core`（**不是** `longkonglong`） |
| 鉴权 | `~/.dbconfig`，`$RDSHOST` / `$DB_PW` / `$DBNAME` |
| ⚠️ **dbname 权威** | **`~/.dbconfig` 的 `$DBNAME`**。`cpt/adapters/_dbconfig.py` 默认的 `dbname="longkonglong"` 只是 **fallback** |
| psql | 必须 `-h 127.0.0.1 -U postgres + PGPASSWORD`（peer auth 不接 ubuntu） |
| 读路径 | `asel.ref_adjust_factor` 走 `WHERE code = %s AND trade_date BETWEEN %s AND %s` —— **全仓没有任何查询按 `source` 过滤** |

**库是跨项目共享的 A 股数据枢纽，不是 SELECT 库。**

### 1.2 规模数字（**已被后续轮次推翻，仅供追溯**）

| 表 | 2026-09-25 实测 |
|---|---|
| `asel.ref_adjust_factor` distinct code | 101 |
| `asel.security_master` 行数 | 5,930 |
| `public.daily_bar` distinct code | 5,225（最新交易日行数是 5,221，**别混淆**） |

⚠️ **不要照这些数字判断现状**——因子表在 2026-10-03 两次切表后规模已变
（见 §3）。要现状请查库。

---

## 2. 仍然在 force 的口径与纪律

### 2.1 环境纪律（本项目一贯做法）

1. **先量结构，再真机验，最后才动代码。** 每个怀疑都要用真机验证。
2. **不看因子值，看可观测后果。** 台阶倍数对了不代表挂的日子对；
   判决要用**独立判据**——「后复权价在除权日是否连续」。
3. **独立判据第一次给出与预期相反的答案时，先怀疑自己的实现。**
4. **改完要复验，且用同一个判据对比修复前后。**
5. **CI 通过 ≠ 线上生效。** `cpt-dashboard` Python 启动时一次性 import，
   改 `cpt/web/app.py` 加新路由**必须**在 Oracle 端
   `sudo systemctl restart cpt-dashboard`。

> 判「有没有打破契约」要 `git stash` 后跑一遍**逐条对比失败名单**，
> **不要数条数**。

### 2.2 「生产因子表一行都没动」的纪律（**已被切表取代，见 §3**）

R37~R44 期间一直维持的纪律是：**只写暂存表，看报告后由人决定是否切换**。
这条纪律本身是对的；它已在 2026-10-03 完成切换后失效（§3）。

### 2.3 Windows 开发机的坑（仍然会咬）

- **内联 Python 会被 PowerShell 吞**：`ssh oracle "... python -c "..."` 里的
  中文/引号/`$(...)` 都会被本地先算掉。
  **复杂命令一律：写脚本文件 → `scp` → 归一化换行 → `bash` 执行。**
- **CRLF**：从 Windows `scp` 上去的文件带 CRLF，仓/服务器规范是 LF。部署前必须归一化。
- **GBK 显示**：控制台按 GBK 解码 UTF-8，中文变乱码、且**行尾会被吃掉**
  （显示异常不代表文件坏了）。设 `$env:PYTHONIOENCODING='utf-8'`。
- **`ruff` / `mypy` / `vulture` / `lint-imports` 不在 PATH**（Windows）。
  `lint-imports` 还需要 `$env:PYTHONUTF8=1`，否则 GBK 解码报错。
- **Windows-only `import fcntl`**：`tests/test_a_share_pool.py` 在 collection 阶段
  就 abort。**这是已知且刻意接受的平台偏差**，不算未修缺陷。

### 2.4 判「代码有没有真的部署上去」

**判据是 curl，不是浏览器**：

```bash
curl -sk -u <user>:<pw> https://127.0.0.1/cpt/<file> | grep -c '<你刚加的标识符>'
```

返回 0 就是**没部署成功**，不是浏览器缓存。

⚠️ `curl http://127.0.0.1/cpt/` 返回空是因为 nginx 301 跳 https；要带 `-k` 和 `Host`。

### 2.5 别把用户数据当测试污染删掉

`~/.cache/cpt/watchlist.json` 里的**真实手输条目是用户数据**。
旧测试文件早已 monkeypatch 了 `DEFAULT_WATCHLIST_PATH`，且 mtime 与 `added_at` 吻合
⇒ 是手动/开发运行留下的。要清就用「移除手输」按钮，别当垃圾删。

### 2.6 工具调用层面的坑（仍在）

- **判「脚本里没有某符号」别用字符串匹配**——注释里会留着历史说明。
  用 `ast.parse` 判顶层 `FunctionDef`/`ClassDef`/`Assign` 的目标名。
- **判「导入了某符号」也别用字符串匹配**——多行 parenthesized import 会变格式。
  用 `ast.walk` 收集 `ImportFrom.names`。
- **`import-linter` 的 `layers` 是自上而下**（第一条是**最高**层）。
  写反了会立刻 `BROKEN`。补契约时**一定要做反向验证**（故意写反 → 应 `BROKEN`）。
- **`extend-exclude` 对显式传入的路径不生效**，只有 `per-file-ignores` 生效。
- **`asel.ref_adjust_factor.code` 是 `varchar(6)`** —— 探测/测试用的假代码必须 ≤6 字符。
- **`-q` 与 `-qq`**：`pyproject.toml` 的 `addopts` 已带 `-q`，
  再「顺手加个 `-q」` 会变成 `-qq`，**把 `N passed` 汇总行吃掉**。
  `-o addopts=""` 是最稳的写法。

### 2.7 静态前端不是仓库目录（**R44 踩坑，至今仍是真的**）

nginx 的 `location /cpt/ { alias /var/www/cpt-dashboard/; }` 是**独立部署副本**，
与 `~/DSH/Chan_Pattern_Trader/dashboard/` **各改各的**。
只 scp 到仓库目录的话，线上**毫无变化**。详见 `docs/archive/reviews-r45.md` §3.6。

---

## 3. 因子重算与切表：R44/R45 的最终立场

> 三份 2026-10-03 的交接单在这里**互相矛盾**——它们是同一件事的三个时间切片。
> 本节只给**最终立场**。

### 3.1 切表**已于 2026-10-03 完成两次**

| 时间 | 结果 |
|---|---|
| 2026-10-03 09:55:46Z | 第 1 次切表，写入 5017 只，占位降到 177 |
| 2026-10-03 10:34:24Z | 第 2 次纳入 71 只「窗口内无除权」，**占位降到 106（2.0%）** |

**切换点登记在 `cpt_factor_epoch` 表**（`cpt/storage/factor_epoch_store.py`）。
快照会随下发带上口径纪元，用 `cpt_factor_epoch.switched_at` 就能分清旧口径 / 新口径。

**此前所有交接单里「生产因子表一行未动」的封顶注记，一律以本节为准。**

### 3.2 「必须替换而非合并」—— 仍然成立的理由

暂存表 `asel.ref_adjust_factor_v2` **不是**生产的超集：多出的行是
**没有对应 K 线的孤儿日期**（生产因子表从 2023-06-15 起，而 `public.daily_bar`
从 2024-01-02 起）。

- 合并式切换（`INSERT ... ON CONFLICT DO UPDATE`）会让库里同时存在新旧两套口径
  ——而这正是**静默**的失败模式：三张表和报告都不报错，只有逐行对账才看得出来。
- 实测孤儿日期 = 0，**「必须替换」的前提成立**。
- 合并还会把生产的过期孤儿行留下——那正是 R36 之前 `daily_bar_raw.source`
  那种脏数据形态。

### 3.3 切表必须带的三条硬断言（不通过就 rollback）

```sql
-- 断言 1：暂存表覆盖度
SELECT count(DISTINCT code) FROM asel.ref_adjust_factor_v2;

-- 断言 2：无孤儿日期（期望 0 行；有行说明误用了合并而不是替换）
SELECT count(*) FROM asel.ref_adjust_factor_v2 v
WHERE NOT EXISTS (SELECT 1 FROM public.daily_bar b
                  WHERE b.code = v.code AND b.date = v.trade_date);

-- 断言 3：单调性（后复权因子必须单调不降）
SELECT code FROM (SELECT code, trade_date, hfq_factor,
                  lag(hfq_factor) OVER (PARTITION BY code ORDER BY trade_date) prev
                  FROM asel.ref_adjust_factor_v2) t
WHERE prev IS NOT NULL AND hfq_factor < prev * 0.999
GROUP BY code;
```

**切表前先 `pg_dump` 两张表**。这三条断言在切表那一刻已经执行过；
若将来再次切换，**仍然必须带**。

### 3.4 因子恒定**不是失败**（**最容易误修的一条**）

报告里 `new_constant` 这个字段 = 「窗口内确实一次分红都没有（东财全史查过）」。
因子恒定**是对的**，而生产同期可能在乱跳。

> **别把「恒定」当 bug 去修。**

同理，重算时**除权日早于本地 bar 起点**的票是**整只拒写**，而不是写个偏小的值——
这也是正确的（跳过早期台阶会让整段因子系统性偏小）。

### 3.5 东财 `success:false` 的两个语义（**必须分开**）

R44 定位的根因比原记录更细：东财有**两个** `success:false`，语义相反——

| code | 含义 | 正确处置 |
|---|---|---|
| `9201` | 查无此记录（这家公司没分过红） | 返回**空行动列表**（合法结果） |
| `9501` | 报表配置不存在（你请求写错了） | **fatal** |

原实现只按 `success:false` 判死 ⇒ **一只「从没分过红的票」把整轮 3000 只的任务掐死了**。
`cpt/adapters/eastmoney_actions.py::_is_no_data()` 已把两者分开。

**判据不是照抄文档，是实测出来的**：
- 是不是限流？同一票连打 25 次，**零波动** ⇒ 确定性。
- 退市票（如「长生退」）东财现行表不收录，同样回 `9201`。

> ⚠️ **新增异常类型时注意 `except` 的顺序**——子类会被父类分支先吃掉。

### 3.6 台阶 off-by-one 的正确公式（**仍然在 force**）

`factor_from_actions` 从最新除权往前连乘。原始代码取的是 `Π{ex > d}`，
而一根 bar 要的是 `Π{ex <= d}`——**正好取反**。

后果不是精度问题而是**方向**问题：除权日价格已跌、因子还停在除权前的水平，
后复权价在除权日凭空多出一次下跌（实测 -65.7%）。

**修法**：`f(d) = 1 / fmap[min{ex > d}]`。

### 3.7 「静默成功」比崩溃更危险（**已转为强制 rc**）

因子重算曾出现：跑 2 分半钟、`rc=0`、看起来「成功」——实际只算了 62 只就死了。
**那 62 只已经写进暂存表**。

**修法（已实施）**：`scripts/factor_recompute.py` 显式检查 `state.stopped`，
「跑完了」与「跑死了」用不同的退出码区分（`return 3`）。
`scripts/run_inspection.py` 同样。

> 这与 `docs/archive/reviews-r45.md` §2.5 是同一条纪律。

---

## 4. A 股快照批量入库 + `cpt_dashboard_run`

> **这一整节已完成上线并验证。** 保留的是**设计决策**，
> 执行步骤（7 步手册）已被 R23 的实际执行取代，不再复述。

### 4.1 A 阶段：`cpt_signal_event` 数据链路

**问题**：主服务只跑加密行情，从不调 A 股的 `record_signal_event`
⇒ `/api/dashboard/signal-stats` 长期 `total:0`。

**方案（用户拍板）**：**并列再起一个 service，不改主服务**——
oneshot service + timer，每天 08:00 UTC（北京时间 16:00）触发一次。

**仍然在 force 的三条设计决策**：

| 决策 | 理由 |
|---|---|
| **必传** `ensure_factors=factor_ensurer_from_env(default=True)` | 缺因子表时 K 线全是空、`record_signal_event` 永远不触发 |
| **不引 `EnvironmentFile`** | 避免与主服务形成隐式耦合——删 env 不应打挂 A 股快照。DB 鉴权走 `~/.dbconfig` |
| **`Type=oneshot` + `TimeoutStartSec=600`** + `Persistent=true` | 跑 50 只 ≈ 8.7 秒，远小于上限；加上限避免连崩拖死；错过的触发开机补跑一次 |

### 4.2 B 阶段：`cpt_dashboard_run`（5 列精简表）

**目标**：`run_body` 原先走进程级 deque，重启即空 ⇒ 用户 `/compare` 报
`{"available": false, "reason": "run_body_unavailable"}`。

**设计（用户拍板，5 列不多加）**：

```sql
CREATE TABLE IF NOT EXISTS public.cpt_dashboard_run (
    run_id        text PRIMARY KEY,      -- 业务主键 = runtime.run_id or dataset_hash
    dataset_hash  text NOT NULL,
    generated_at  timestamptz NOT NULL,
    body_recorded boolean NOT NULL,
    snapshot      jsonb
);
```

> **为什么不要更多列**：`symbol` / `interval_ms` / `bar_count` / `config_hash` /
> `source` / `created_at` 都能从 `snapshot` jsonb 现抽，冗余带来一致性问题。
> 真要「按 symbol 查全部 run」等高频查询，再 `ALTER TABLE` 加列 + 回填。
>
> 迁移文件：`scripts/migrations/2026-10-02_r23_dashboard_run.sql`。

**仍然在 force 的取舍（不要过度工程化）**：
- **双写架构**：in-process ring 留 hot-path，表留 cold-path。两套互不影响。
- **不引入自动 GC**：运维手 DELETE，避免 HTTP 路径跑大 SQL。
- **不引入对 snapshot 的额外校验**：jsonb 允许任意结构，由调用点负责。
- **不引入分布式锁**：单进程实时写，单 PG 双写足够。

⚠️ **不要 DROP `public.cpt_signal_event`**——那是真数据链路。

---

## 5. 仍然开放的待办

### 5.1 🔴 安全：生产库口令进过 git 历史（**未处理**）

`$DB_PW` 在 2026-09-30 **明文进过 git 历史**，2026-10-01 从文件里删了——
**但进过历史就等于公开过**。

`gh repo view` 实测：**`{"isPrivate": false, "visibility": "PUBLIC"}`**。
交接单里写的「或至少确认该仓为私有仓」——**第一个条件不成立**。

**需要 owner 执行**（没有权限也不该代办）：
1. 轮换 `emotion_core` 的 postgres 口令；
2. 更新 oracle 的 `~/.dbconfig`；
3. 评估是否需要清理 git 历史，或至少确认影响面可接受。

> 这是本仓历史上**第二次**把生产库口令写进版本库。
> **不要把口令值贴进任何文档或聊天。**

### 5.2 其他未决项

| 优先级 | 项 | 状态 |
|---|---|---|
| P1 | **30 只「台阶对不齐」**（605388 / 601811 / 603259 / 603919）成因 | 未查 |
| P2 | **992→2125 口径更正**的根因在上游 `asel` 产数逻辑 | 未查（**另一个仓**） |
| P2 | **`derived_bar` / `daily_bar` 每月 4 月底缺口** | ingest 侧未定位；实测规模见 `docs/archive/reviews-r45.md` §5.2 |

---

## 6. 不要做的事（仍然有效）

- **不要 `sudo chmod -R`**：会打掉 `/var/www/cpt-dashboard/vendor/` 执行位
  → `vendor/*.js` 全 403，页面图表库静默加载失败，HTML/CSS 看起来全对。
- **不要随便 `rm -rf` 数据库表**：`public.cpt_signal_event` 是真数据链路；
  `asel.ref_adjust_factor` 是切表后的生产口径。
- **不要用 `.venv/bin/python -m cpt.web` 前台跑**：会冲突主服务的 :8010。
  要测先 `systemctl stop cpt-dashboard` 或改端口。
- **不要在生产机器上 `git reset --hard`**：`git pull --ff-only` 报 non-fast-forward
  说明工作区脏了，先 `git status` 看清谁动的，**让 owner 处理**。
- **不要把一次 `sudo` 失败当成永久状态**：先用 `sudo -n id` 与
  `/proc/self/status` 各测一次再下结论。
- **备份放工作区，不放 `/tmp`**——本仓 `/tmp` 配额紧，且不支持 SQLite WAL
  （`MYPY_CACHE_DIR` 指 `/tmp` 会让 mypy INTERNAL ERROR）。
