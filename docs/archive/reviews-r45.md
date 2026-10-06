# R45 分层复盘 · 合并历史记录（R45 / 2026-10-03 ~ 2026-10-06）

> ## 这是一份**合并后的历史记录**，不是活文档
>
> **来源文件**（R45 一轮逐层复盘的 9 份文档，已 `git rm`，可从 tag `pre-docs-consolidation` 取回）：
>
> | 原文件 | 覆盖对象 |
> |---|---|
> | `docs/review-domain-layer-r45.md` | `cpt/domain/` |
> | `docs/review-adapters-layer-r45.md` | `cpt/adapters/` |
> | `docs/review-application-layer-r45.md` | `cpt/application/` |
> | `docs/review-storage-layer-r45.md` | `cpt/storage/` |
> | `docs/review-llm-layer-r45.md` | `cpt/llm/` |
> | `docs/review-web-layer-r45.md` | `cpt/web/` |
> | `docs/review-dashboard-r45.md` | `dashboard/`（无头浏览器实测） |
> | `docs/review-deploy-r45.md` | `deploy/` |
> | `docs/review-ensure-table.md` | `run_metric_store.ensure_table` 单点裁决 |
>
> **覆盖轮次**：R45（2026-10-03 ~ 10-04 为复盘主体），结论经 R46~R56 验证。
>
> **本文保留**：① 至今**仍然在force**的结论与约束；② 决策及其**理由**（可追溯性）。
> **本文不保留**：怎么找到的、试过什么被否、假阳性排查过程、逐轮叙事。
>
> ⚠️ **本文不含任何行号**。R45 的行号是 2026-10-03/04 当时的现场坐标，此后
> `cpt/web/app.py`、`cpt/adapters/a_share_local.py` 等多次重构漂移数百行，
> 且 R54 已确认「行号在不在」与「行号指不指对那件事」是两回事。
> **要定位请用符号名 `git grep`。**

---

## 1. 总览：六层 + 两个部署面的复盘结论

| 层 / 面 | R45 结论 | 今天是否仍成立 |
|---|---|---|
| `domain/` | 干净，无缺陷；补了两道冻结口径门禁 | ✅ 成立（门禁仍在） |
| `adapters/` | 3 个真 bug（异常契约 / rc=0 / 静默清空） | ✅ 已修并被门禁锁住 |
| `application/` | 3 个真 bug（DB 故障被报成缺因子 / CDN 护栏后门） | ✅ 已修 |
| `storage/` | 3 处「查不到 vs 查失败同码」，1 处零测试的生产 cron | ✅ 已修 |
| `llm/` | 3 个真 bug（清空进程环境 / reason 归类撒谎 / 丢回调） | ✅ 已修 |
| `web/` | 干净（R29 的运行时契约挡住了同类问题） | ✅ 成立 |
| `dashboard/` | 5 个源码看不出的浏览器侧问题 | ⚠️ **画布 D 已在 R51 下线，见 §4** |
| `deploy/` | env 模版漏告警键 + crontab 不指向仓内 | ✅ 已修 |

---

## 2. 仍然在 force 的结论与约束

### 2.1 `domain/` —— 纯算法层的防线是「前提正确」，不是异常处理

- **设计事实**：16 个文件里只有 2 个 `except`，唯一的静默分支是
  `structure_events._as_opt_int`（纯类型转换器，行为正确）。**这是正确的**——
  纯算法层「算不出就该炸，不该静默出一个错的结构」。
- **代价**：这一层唯一的防线是前提正确，所以需要文档断言门禁。
- **两道门禁（仍在 CI）**：
  - `tests/test_domain_semantic_contract.py`（5 用例，盯 `levels` 单位）
  - `tests/test_domain_frozen_contract.py`（把 14 个字段与默认值钉在
    `FROZEN_V0`，改了字段/默认值就红）

### 2.2 冻结口径的两套互不交叉的机制（这是被误解过的地方）

`RulesConfig` 的「冻结」由三套**互不交叉**的机制分担：

| 机制 | 守什么 | 谁在读 |
|---|---|---|
| `SCHEMA_VERSION` | 回放 fixture 反序列化时拒收异版本 | 仅 `from_dict` |
| `reproducibility.config_hash` | 运行历史的「换参数了」比对 | `run_metric.py` 确实在比对 |
| `reproducibility.rules_version` | 快照元数据对外展示 | 前端 |

**声明修正（仍在代码里）**：
> `config_version` 是**回放入口的单点守卫**，不是全局版本闸门。

### 2.3 ⚠️ 已推翻：`SCHEMA_VERSION` 恒为 `"v0"`

R45 当时的实测是：`SCHEMA_VERSION` 硬编码 `"v0"`，且 `from_dict` 会 `pop` 掉
传入的同名字段 ⇒「改参数必须升版本」这条纪律**没有任何机制强制**。

**最终立场（R45 内的裁决是「把冻结说准，不拆掉它」，其后于 2026-10-06 真的升了版）**：
`SCHEMA_VERSION` 现为 **`"v1"`**。见 §3.1 的升版起因。

### 2.4 `min_bi_len` 的量纲（`docs/rules.md` §9.8，不可与 §9.7 混用）

| 参数 | 量纲 |
|---|---|
| `min_bi_len` | **去包含后的 K 线根数**（底层笔） |
| `min_elements_for_higher_bi` | **低级别结构元素数**（高级别笔） |

`docs/rules.md` 专门写了「不可混用」——这正是容易写错的地方。

### 2.5 `adapters/` —— 三条错误语义（已被 `scripts/check_storage_failure_semantics.py` 锁住）

1. **`~/.dbconfig` 的 `$DBPORT` 必须抛注入的异常类型**：格式错与**越界**
   （0 / -1 / 65536）都抛 `exc_type`。静默去连一个不存在的端口比报错难查得多。
2. **`notify_problem` 失败必须返回非零**：`scripts/run_inspection.py` 与
   `scripts/factor_recompute.py` 都用 `return 3`。**「该做的没做成」与「做完了」
   不能共用 rc=0**——cron / 看门狗 / 外部监控都靠这个区分。webhook **未配置**
   那条分支仍然降级（那是「本来就该降级」）。
3. **自选股文件损坏一律抛**：`_read()` 损坏即抛，消息明说「未做任何修改」。
   **「猜成空列表」在这里是最坏的降级**——自选股是用户数据，猜错就是丢数据。

### 2.6 `adapters/` 外部契约（`scripts/verify_public_contracts.py`）

**刻意不进 CI**——它依赖公网，不能让腾讯/新浪的抖动把 CI 弄红；定位是手工巡检工具。

| 契约 | 结论 |
|---|---|
| `fqkline` 字段顺序 = `[日期,开,收,高,低,量]`（**不是 OHLC**） | ✅ |
| 复权键名 `hfqday` / `qfqday` / `day` | ✅ |
| 逐标的「无 hfq」抛 `AShareAdjustUnsupportedError` | ✅ |
| 新浪快照必须带 Referer | ✅ |
| 腾讯 hfq 价 vs 本地因子表算出的后复权价 | ✅ 差 0.000% |

最后一条最有价值：两条**完全独立**的数据路径算出同一个后复权价
⇒ 复权口径两边都没漂。

### 2.7 `application/` —— DB 故障必须与「没数据」分开报

- 外层 `except Exception` 补 `_rollback_quietly`（**仅共享连接时**——
  自有连接的 `finally` 会 close，不受影响）。
- 两处内层吞异常改为「记日志 + rollback + 如实报 `db_error:<类型>`」。
- 既有前科：`tests/test_ashare_db_error_not_masked.py` 记着「DB 故障被报成缺因子」。
  报成 `db_error` 是对的——报错方向会把排查带偏。

### 2.8 `storage/` —— PostgreSQL 放大了这个病

**核心教训：DB 失败必须抛，不能与「空」同码。**
PostgreSQL 的性质放大了它：**事务中一条语句失败 ⇒ 同连接后续全部 aborted**。
所以 catch 住 DB 异常再返回空值，不是降级，是**把局部失败放大成整页失败**。

| 函数 | 已改为 |
|---|---|
| `load_previous_signal` | 抛（调用方的 rollback 不再是死代码） |
| `enqueue_call` | 写失败抛；**重复提交才返回 `False`** |
| `recent_calls` | 抛 |
| `prune` | 抛（cron 才能判断「干完了」还是「一条没删」） |

**`prune` 的两个坑**：
1. `public.cpt_run_metric` 一张表混两类行 —— `KIND_RUN`（每轮一行，高频）与
   `KIND_INSPECTION`（每天状态比对依据）。**必须带 `kind` 条件**，
   不带就会把巡检行一起删掉。两类分开配窗口（cron 里 90 天 / 30 天）。
2. 失败与「没东西可删」同码 ⇒ 静默慢性泄漏。

**门禁与测试**：`scripts/check_storage_failure_semantics.py`（已接 CI）+
`tests/test_run_metric_store_prune.py`（5 用例）。
> **「加了门禁」不等于「这一层有覆盖」**——门禁查的是「有没有 catch 住 DB 异常
> 返回空」，它**不检查这个函数有没有测试**。两件事，别互相顶替。

### 2.9 `llm/` —— 三条

1. **`load_config` 绝不能动进程全局环境**（`os.environ.clear()`）。
   CPT 是多线程的（`ThreadingHTTPServer` + LLM worker），任何线程在
   `clear()` 与 `update()` 之间读环境变量，拿到的是残缺甚至空的环境——
   包括 `RDSHOST` / `DB_PW` / 飞书 webhook。
   做法是把 `source` 一路传给 `_env_str/_env_int/_env_float/_env_bool`。
2. **`not_json` 的定义是「找不到任何能解析的 JSON 候选」**。
   合法 JSON 标量（`"str"` / `123` / `null` / `true`）解析**成功了**，
   只是形状不对 ⇒ 归 `schema_mismatch`。
   **归类撒谎比归类粗糙更有害**：撒谎会把人引向错误的假设。
3. **`get_queue` 必须能补注册后传的审计回调**：`LLMQueue.set_on_status()`
   （后设覆盖先设）；worker 侧走 `_emit()` 取**快照**再调（持锁调用会卡住
   补注册，而回调自己要开 DB 连接）；配 `_callback_lock` 保护。
   ⚠️ 补注册那段**必须在 `load_config()` 之前**——否则「环境变量被改成停用」时
   `get_queue()` 返回 `None`，**把正在正常运行的队列凭空藏起来**。

**本层做得对、重做时别改坏的地方**（仍成立）：
- `api_key: str = field(default="", repr=False)` —— 密钥不进 dataclass repr
- `redacted()` 只回 `has_api_key: bool`
- `except LLMRateLimited` **排在** `except LLMError` **之前**（前者是子类，顺序正确）
- `submit()` 查 `_worker_alive()` 而不只看 `_stop`
- `levels.level_label` 未知 level 返回 `未标注级别（level=abc）`，**不编造**

### 2.10 `web/` —— R29 立的运行时契约仍然成立

`tests/test_web_error_contract.py`（4 用例）保证「JSON API 的错误响应必须是 JSON」。

**这层干净是有原因的**：R29 那次复盘建立的是**运行时契约**（不是源码 grep）。
R45 在 `application/` 找到的 bug 形态都是**没有契约覆盖**的地方
（DB 故障的 reason 语义、CDN 后门、跨层异常边界）——这层因为有契约，
恰好躲过了同一类问题。

### 2.11 三处「不是 bug」的判定（避免后人重复排查）

| 位置 | 判定 |
|---|---|
| `app.py::_read_json_body` 读失败返回 `None` | 正确。让调用方回**自己的 400**，而不是在 handler 里冒未捕获异常变 500 |
| `__main__.py::_cached_snapshot` 未命中返回 `None` | 正确。这是「没命中」信号，且先判 `entry is None` 不会 KeyError |
| `limit` 畸形 → HTTP 200 + 合法默认值 | 合理降级，不是撒谎 |

---

## 3. 已被后续推翻的结论（保留最终立场，不叙述过程）

### 3.1 `min_bi_len` 是否只对 czsc 生效 —— R45 的「B」方案已被推翻

**R45 当时的实测**：`min_bi_len` 只喂给 czsc，4 个调用点都写成
`resolve_backend(DEFAULT_BACKEND, min_bi_len=...)`，而 `DEFAULT_BACKEND` 是 `native`
⇒ 这个参数**被生产静默丢弃**。实测 40 只票，native 笔跨度 <4 根的占 **35.3%**，
而 czsc 同一批输入退化笔 0%（最短 10 根）。

> 退化笔不是精度问题而是**方向**问题：笔太细 ⇒ 力度度量碎在 2 根 K 线上
> ⇒ 一买/一卖背驰比较的分母不稳。

**R45 的裁决是选 B**（保持现状 + 把声明改准，因为接上门槛会让
41 条信号、`cpt_run_metric` 的水位/指纹历史、缓存的 run 全部作废），
并明确「需要 owner 决定 A 还是 B」。

**最终立场：选了 A，已实施。** `cpt/domain/config.py` 现记：
2026-10-06 `min_bi_len` 的跨度门槛**真正在 native 后端生效**，
`SCHEMA_VERSION` 因此从 `"v0"` 升到 **`"v1"`**（门槛一生效，全部结构
—— 笔 / 中枢 / 走势类型 / 信号 / 水位指纹 / 缓存 run —— 与 v0 不兼容）。
`min_bi_len` 现在按 bar 间隔分档（`RulesConfig.min_bi_len_for(interval)`）。

### 3.2 `reference` 后端已落地为一等后端

R45 新增 `cpt/adapters/reference_backend.py`，并把
`parity_reference.build_parity_snapshot_for` 改为**委派**给它——
参照侧从此只有**一份**实现，不会出现「parity 走 czsc、离线导出走腾讯」的漂移。

| 项 | 值 |
|---|---|
| 回落链 | czsc（实现对照）→ 腾讯 hfq（数据链路对照）→ 抛 `ReferenceUnavailableError` |
| 用了哪一级 | `backend.source` / `backend.detail` **如实报告** |
| 默认档 | **仍是 `native`**（R16-4 的决定，`DEFAULT_BACKEND` 硬编码） |

**刻意不做**：参照侧**不静默回落 native**——那等于自己跟自己比，
是对照面板最没意义的一种「通过」。宁可报「没参照」。

`BACKEND_CHOICES` 现为 `("auto", "czsc", "native", "reference")`。

**契约级约束**：`ChanlunBackend` 只传 `bars`，而腾讯那条路需要标的代码，
但 `CanonicalBar` **没有 `code` 字段** ⇒ 代码只能作为**后端级属性**显式传
（`ReferenceChanlunBackend(code="600519")` / `resolve_backend("reference", code=...)`）。
留 `None` 时走到腾讯那级直接判不可用，**不猜代码**。

**新异常与 `UnknownBackendError` 分开**：
后者是「**档位名写错了**」，前者是「**名字对、但环境不支持**」。
混成一个，会让「配置错误」和「依赖缺失」在日志里长得一样。

### 3.3 三后端对比的实测结论（仍是有效证据）

czsc 与 native 的**笔数差 3.4~5.7 倍**（6 只票，无一例外），分型数接近（差 1~15），
中枢差 2~6 倍。两边**契约检查都全通过**（笔严格递增、无真重叠、方向 ±1；
中枢 `high>low`、`bi_ids` 不越界）⇒ **不是谁算错了，是口径不同**。

工具：`scripts/compare_chanlun_backends.py`（探测后端可用性、跑契约、
**明确报出缺哪个**，不假装做了对比）。仓内 `InMemoryChanlunBackend` 是
**测试占位**，脚本刻意不把它算进「三方对比」。

### 3.4 `deploy/` —— env 模版曾漏 `CPT_FEISHU_WEBHOOK`（已补）

- `cpt/adapters/feishu.py` 从 `CPT_FEISHU_WEBHOOK` 读 webhook。
- `deploy/cron/run-inspection-daily.sh` 里有一行**显式警告**必须加载 env。
- **漏了它的后果是一个不会报错的故障**：巡检照跑、日志照写，
  只是没人在手机上收到东西。已补进 `deploy/env/cpt-dashboard.env.example`。

### 3.5 `deploy/` —— crontab 已改为直接指向仓内路径

**问题**：crontab 指向 `/home/ubuntu/bin/` 的**副本**，没有任何机制保证两边继续一致。
**方案（owner 选定）**：crontab 直接跑仓内路径。
「两份文件靠人记得同步」本身就是问题的根源。

**代价（已知并接受）**：仓被 `git checkout` 到旧提交时跑的就是旧脚本——
但这本来就是 git 该有的行为，而「改代码却不同步线上」本来就不该发生。

**实施前验掉的风险**：三个脚本都被 git 跟踪（`git clean` 只删未跟踪文件）；
都自带 `cd "$REPO" || exit 1`，不依赖 cron 给的 `$HOME`；
解释器走仓内 `.venv/bin/python`。

> **cron 不经过 shell，没有执行位就是 `Permission denied`。**
> 两个脚本的 git 模式曾是 `100644`，是 `install -m 755` 复制时掩盖了这个缺失。
> 三个脚本现均为 `100755`。

### 3.6 静态看板部署（**这是本仓最贵的一个部署坑**）

看板在 Oracle 上是**两处独立部署**，只重启服务**不会**更新浏览器看到的界面：

1. **后端** `/home/ubuntu/DSH/Chan_Pattern_Trader` +
   `sudo systemctl restart cpt-dashboard`（Python 启动时一次性 import，
   **新路由必须重启进程**；`Restart=on-failure` 不因代码变化自动重启）。
2. **静态前端** `/var/www/cpt-dashboard`（nginx `dsh-web` vhost 的
   `location = /cpt/` + `location /cpt/` 走静态，`location /cpt/api/` 反代 :8010）。

- **不要 `sudo chmod -R`**：会打掉 `vendor/` 目录执行位 → `vendor/*.js` 全 403，
  HTML/CSS 看起来全对而图表库静默加载失败。逐文件 `chmod u=rw,go=r`。
- **R34 纠正**：早期交接单里那条 `cp` 命令的文件清单**不全**（漏了 5 个 `canvas_*.js`，
  含 `canvas_registry.js`），照抄会让画布 D 诊断静默停在旧版而页面看起来完全正常。
  **权威流程在 `deploy/README.md`「部署静态看板」一节。**
- **R51 之后**：`deploy/dashboard-sync.sh` 已改为「真实加载物清单」；
  改过 `dash-*.js` 后必须重建 bundle。

---

## 4. 已被 R51 推翻的结论（画布 D / `dashboard.js` 已下线）

> **画布 D（wbt 报告视图）与 `/api/canvas/wbt` 已完全下线**（R51）。
> 本节只保留**仍然有价值的约束**，不保留下线过程。

### 4.1 仍然在 force 的两条硬约束

1. **所有画布永远不许自己取数**。
   根因是**画布各自取数**导致的跨市场错配（A/B/C 画 123 根 A 股 K 线、
   D 画 579 根 BTCUSDT K 线同屏）。守卫现在是
   `test_canvas_modules_never_fetch_server_side()`：遍历所有 `canvas_*.js`，
   断言源码里不出现 `fetch(` / `XMLHttpRequest` / `/api/`。
   这比原来只管 D 怎么透传的断言**更强**。

2. **iframe 绝不加 `allow-same-origin`**。
   旧组合有已知逃逸：帧内脚本能 `frameElement.removeAttribute("sandbox")`
   再重载从而拿到父页 origin。按 R28-11 的决定刻意只给 `allow-scripts`。
   ⇒ srcdoc 文档 origin 是不透明源 `"null"`，而**字体是 CORS 受限资源**，
   所以图标字体**永远加载不了**。
   > 若将来真要用图标字体，正确做法是把字体**内联成 `data:` URI**——
   > data: URI 不受 CORS 约束，且不破坏 sandbox 边界。

   **代价（仍在）**：父页**无法**从外部判断 iframe 内画没画出来
   （`contentDocument` 跨域不可读）。这是 R28-11 的安全设计，不是缺陷。

### 4.2 parity 四态配色（结论仍有效：`dashboard_parity.py` 仍用四态）

`status ∈ matched | missing | extra | mismatched`。**四态不是三态**——
`mismatched`（两边都有但字段对不上）恰恰是**最值得看**的一类。

| 状态 | 颜色 | 含义 |
|---|---|---|
| `matched` | `--color-text-muted` | 一致 |
| `mismatched` | `--color-accent` | 有出入（**最该看的**） |
| `missing` | `--color-error` | ORACLE 缺 |
| `extra` | `--color-degraded` | ORACLE 多 |

**必须带真实计数的图例**——光有颜色是歧义的，观者无从知道红点代表「缺」还是「多」；
数字比颜色更有用。

### 4.3 无头浏览器验收的四类判据（仍然适用）

不靠「页面能打开」，而是：
1. console 报错 / 未捕获异常（`pageerror` + `console` **双通道**）
2. 请求失败与 HTTP >= 400
3. 页面可见文案里的 `undefined` / `NaN` / `[object Object]` / 占位符残留
4. **渲染结果本身**：SVG 图元数、canvas 非透明像素占比、计算样式

**以及截图实看**——有些问题数字全绿、只有看图才发现。

> **判据必须用真实 API 响应，不能用自己拼的样本。**
> 拼样本时把真实字段名写错，会造出一份「全是 null」的**假证据**。

---

## 5. 仍然开放的遗留项

### 5.1 `cpt_run_metric` 的 schema —— R45 的裁决已被 R56 执行

R45 的 `docs/review-ensure-table.md` 记了一条**真债**：
`public.cpt_run_metric` 的 schema **没有迁移文件**（`_DDL` 是唯一来源）。
R45 当时的裁决是**方案 A（保持现状）**，理由是 `ensure_table` 是幂等的、
且 2026-10-03 真库实测与 `_DDL` **零漂移**（22 列 / 3 索引 / 21 条 NOT NULL 约束）。

> ⚠️ **那个裁决有一个致命前提**：`CREATE TABLE IF NOT EXISTS` 意味着
> **表已存在时 DDL 整个不生效**。于是改 `_DDL` 的列之后——
> 任何**测试都不会变红**（改的是字符串常量），真库却仍是旧结构。

**最终立场**：该表已固化为迁移文件
`scripts/migrations/2026-10-06_r56_cpt_run_metric.sql`（R56）。

### 5.2 仍需 owner 决定的事项

| 项 | 内容 |
|---|---|
| **`deploy/dashboard-sync.sh` 的默认 Basic Auth 口令** | 硬编码默认口令在仓内（可被 `CPT_BASIC_AUTH` 覆盖）。owner 已明确表示不轮换口令，此处**只记录** |
| **623 只票的 K 线缺口** | 成因在 ingest 侧（另一个项目）。缺口在 `daily_bar`（行情主数据）里，平均缺 7.4 天、**最多缺 62 天**；缺口日全市场覆盖率 95.6%~99.6%，**666 天没有一天低于 90%** ⇒ 不是系统性故障，是**个体级**。要抓它需要**独立于 K 线本身**的判据（本仓现成有：`public.trade_calendar`） |
| **78 只「分过红但算不出」** | 除权日早于 bar 起点（2024-01-02）。要治得往前拉 K 线起点，同属 ingest 侧工程 |
| **llm 层两处低危** | ①`structure` 字符串值里含**奇数**个 ``` 围栏可提前闭合（`structured.py` 防御式解析兜住，危害有限）；②`submit_llm_explain` 的 `structure` **直接来自 HTTP 请求体**，不回查 DB、不校验形状（无权限边界跨越，但若将来 LLM 结果要展示给其他用户需重新评估） |
| **「文档断言」门禁是否扩容** | 现有 5 用例只盯 `levels` 单位。**不建议**现在加更多——门禁的价值来自「覆盖了真的会出错的点」，不是数量 |

### 5.3 一个值得记住的元教训

`validate_ashare_bars` **刻意跳过连续性检查**（A 股的周末/节假日/停牌缺口合法）。
这个设计对 C3/C5 是对的，代价是**对 ingest 丢数据也一并放行**。
⇒ 「一个正确的校验器」与「它放行了不该放行的」可以同时为真，
判据必须来自**另一个数据源**。

---

## 6. 一条仍然在 force 的工作纪律

> **补测试之所以有价值，不在于新测试本身，而在于它强制重新走一遍那条路径**——
> 哪怕是离线跑，也会立刻炸出真问题。
>
> R45 的实例：`parity_reference.py` 补测试后连续炸出 3 个真 bug，
> 其中 1 个**会让每次 A 股快照都抛**。而那天**已经测过 parity 面板**——
> **但那是在这次重构之前**。
>
> ⇒ **把「我验证过上一个版本」当成了「我验证过这个版本」**。
> 改完要真机验证，用**同一个判据**对比修复前后。
