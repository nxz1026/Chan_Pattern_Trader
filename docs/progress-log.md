# CPT 进度日志（按主题整理）

本文件记录 R13–R56 的工程事实，按**主题**组织而非按轮次：同一件事往往在好几轮里被
反复记录与修订，按轮次写就会重复，按主题写才读得下去。

- 本轮起算：2026-09-24（R13）
- R1–R12 见 `docs/archive/progress-log-至R12-2026-09-24.md`
- 现行规则口径见 `docs/rules.md`；跨切面陷阱见 `docs/known-traps.md`；
  分层与目录见 `docs/architecture.md`；接口清单见 `docs/web-api-reference.md`
- 口径类结论若与本文冲突，以 `docs/rules.md` 与代码为准

> **历史文档的处理原则**：带日期的验收/审计快照是当时的现场记录，改写等于篡改历史。
> 本文件只保留**当前事实**；某轮结论被推翻时，写最终位置并注明取代它的轮次。

---

## 轮次索引（每轮一行，只记结果）

| 轮次 | 结果 |
|---|---|
| R13 | 移除 3 个 GPL 参照、改用 czsc + wbt；删 oracle 全家桶；日志归档重开 |
| R14 | 算法换引擎：分型/笔改用 czsc；中枢延伸改为**不收缩**；引入 `RulesConfig.min_bi_len`；移植一买/一卖谓词与力度度量 |
| R15 | A 股接入：复权因子工具、本地 DB adapter、规则标签、热门池 + 自选落盘 |
| R16 | 四画布 + flag 切换；`CzscChanlunBackend` 接入生产路径（`backend_factory`） |
| R16-5 | 四个画布落地（原生 SVG / lightweight-charts / plotly / wbt 报告），断网可用 |
| R17 | 数据源扩充与「源状态一等化」（`source_registry` + `/api/dashboard/sources`）；纠正本地库是**不复权**原始价 |
| R17-2 | Wind 额度实探；修 Wind 回执解析；`trade_calendar` 探活取代 weekday 启发式 |
| R17-3 | A 股接入主看板（换 snapshot URL，画布零改动）；按需补因子；证券名称显示 |
| R18 | 未完成任务盘点；8 个 fixture 迁到真实 5m 契约；台账职责收归本文件 |
| R19 | `pending-wiring` 重排；A 股规则标签接线（`Bi.source_ids` 合成 id） |
| R20 | 删除 `dashboard_market_fetch` / `dashboard_parity` 后端；一买翻译层；`ref_adjust_factor` 补列；`dashboard_runs` |
| R21 | 信号事件持久化（`cpt_signal_event`）；一卖信号；面板四项；hot_rank 回补 |
| R22 | 「D 类」9 模块全部接线（6 条新路由 + 8 个前端面板 + Wind 兜底链路） |
| R23 | 运行本体落表（`cpt_dashboard_run`），`/compare` 与 `/multi-run` 跨重启可比 |
| R24 | **恢复 storage 层**；SQL 只许出现在 `adapters/storage`，并新增门禁 |
| R25 | 独立 LLM 服务层（异步 + 429 退避重入 + `cpt_llm_call`） |
| R26 | 结构事件流接线：`StructureEvent`/`StructureState` 第一次有生产者和出口 |
| R27 | 事务边界收口（8 处 `rollback`）；结构事件 HTTP 出口与 UI；`structure_id` 加市场前缀 |
| R28 | 429 退避真机验证；LLM 结构化防御式解析；LLM 前端面板；worker 静默死亡修复；画布 D 换不透明 origin；级别标签表 |
| R29 | web 层复盘：错误响应统一 JSON，并立运行时契约门禁 |
| R30 | domain 层复盘：level 单位按市场而异，改掉权威 docstring 里的错话 |
| R31 | adapters 层复盘：PG schema 零漂移；Wind `CPT_WIND_NODE`；后复权基准闸 |
| R32 | application 层复盘：导出的时间切片只切一半，如实声明 |
| R33 | 差点退役在用通道（查错日志文件）；补 `run7.sh` |
| R34 | 前端复盘：3 个键名错配（camelCase 泄漏到原始 API 对象） |
| R35 | `dual_compare` 换源 + 修后复权口径错配；parity 接上（czsc 优先、回落腾讯） |
| R36 | `DEFAULT_BACKEND` 从 `auto` 改成 `native`；parity 匹配率低追到因子表 |
| R37 | 因子重算工具落地（只写暂存表）；推翻离线台阶检测方案 |
| R38 | 日志分两轨：飞书告警出口 + `cpt_run_metric` 水位表 + 巡检 + 看板面板 |
| R39 | 免费真值源（东财分红送配）；A 股也落水位；`structure_event.cause`；golden set |
| R40 | 因子重算 off-by-one：台阶幅度对但挂错日 |
| R41 | 送转比例重复计数一倍（`BONUS_IT_RATIO` 是总数） |
| R42 | 复盘：候选集排除了全部占位票；992 只非单调低估约一个数量级 |
| R45 | 逐层复盘六层 + 文档对齐 + 四次因子切表 + 门禁⑨⑩⑪ |
| R51 | 画布 D 整体下线 |
| R52 | 修 `ci.yml` 自己的 YAML 缩进（5 个门禁从未真正执行过），CI 首次全绿 |
| R53 | 占位行守卫：零价 K 线不再静默进结构计算 |
| R54 | 全仓文档对齐；发现「门禁验错了属性」，新增 `check_line_refs.py` |
| R55 | 给「门禁恒返回 0」「断言依赖本机环境」两类补专盯门禁 |
| R56 | 全仓通读复审 26 项修复；`min_bi_len` 真正接到 native，`SCHEMA_VERSION` 升 `v1` |
| R57 | 上线前 360° 审计：① 清 39 条未来结构事件 + 17 行假时间戳；② 修轮询陈旧误报（`startPolling` 收进 `markFresh`，补契约测试）；③ `cpt_dashboard_run` 保留期**首次真正自动化**（`prune` + 04:30 cron）；④ 更正 `min_bi_len` 定档的**量纲口径**（6 实为 p95~p97 而非 p80）；⑤ 新增门禁⑭「变异抽查」——只看它会不会响 |
| R58 | 「我的追踪」段 1：数据 + API + 建议点。`cpt_track` + `cpt_track_snapshot` 两表（`X-CPT-User` 头提取，缺省 `default`，字符白名单 + 截断）；BUY/SELL **reference 与 confirmed 双版**（reference 永远有 = 中枢下沿/笔低 × 缓冲 ÷ 倍率；confirmed 仅 `status==confirmed`+动作匹配时填 = 最新结构位无缓冲）；30 天快照 / 6h 人话缓存 / 90 天回收站；缺 `price_ratio` 全 None（除零风险） |
| R58-2 | 同轮续：段 2 + 段 3 一起落地（用户先选「段 1 完即停」、后改主意）。① **段 3 推送**：`cpt/application/signal_notify.py`，maybe_notify + 24h 内存节流 + best-effort + 异常吞掉；`a_share_snapshot.py` 一买/一卖两处 `record_signal_event + commit` 之后立即调；放在 application 层而非 storage 层，保持 R24「storage 越纯越好」精神；标题前缀 `[CPT 追踪]` 区分 emotion-core 同群机器人。② **段 2 UI**：`dashboard/track.html + dash-track.js`（IIFE 独立页面，与现有 dashboard 解耦）；加入表单 + 列表卡（折叠 current / algorithm / points / human + 「再讲一次人话」按钮 + 回收站区块）；nginx `/cpt/track/` 路由 + `dashboard-sync.sh` 加 FILES。③ 实测：端到端烟雾通过（add/list/advice/history/remove/restore 全 200）+ 推送一条到群验证联通 |
| R58-3 | 同轮再续：① **CPT 主页加「我的追踪」入口**：`dashboard/index.html` topbar 加 `<a href="/cpt/track/">我的追踪</a>` 硬链接，**不动 JS**（最小风险）。② **「再讲一次人话」走真 LLM**：`cpt/web/track_api.py::handle_track_speak` + `POST /api/dashboard/track/{code}/speak`；**复用现成** `cpt.application.llm_cases.summarize_recommendation`（R38 入口，prompt 严密：模型只看到确定性三行结果，没有机会编造）；note 拼进 disclaimer 段（"用户偏好：长期持有。…"），让模型语气贴近用户意图但不参与动作判断；`subject_id="track:{user}:{code}"`；6h TTL 由 LLM `request_hash` 幂等 + `SPEAK_TTL_HOURS=6`（**绝对值断言**钉死）实现。③ **前端** dash-track.js：「再讲一次」按钮 POST `/speak` 拿 call_id → 每 2s 轮询 `/api/dashboard/llm/calls?subject_id=…`（30s 超时）→ 完成后 reload advice 渲染新 human。`duplicate` status 直接 reload（6h 内已写过）。**不**写 `cpt_track_snapshot`（人话是异步结果，由前端轮询完成后 update UI，不污染历史）。 |

| R59 | 上线前 360° 审计（外部只读审计：4 严重 / 10 高 / 29 中 / 18 低）**修复批次**。① 严重：S3 `signal_id` 主路径补 `code`（`first_buy:{level}:{structure_id}:{code}`，迁移 `scripts/migrations/2026-10-09_r59_signal_id_code_scope.sql`，幂等 + scratch 库 5 行夹具实证；生产 dry-run 71/72 行待补、补后撞号 12 组 → 0）；② 高：H5 保存点回滚、H6 `test_track.py` 全局 `DELETE` 作用域化、H7 CI 真起 Postgres + `CPT_REQUIRE_DB=1`（此前 DB 用例因不装 `db` extra 静默 skip）、H8 飞书解析响应体（**200 不等于送达**）+ webhook token 脱敏、H9 伪笔（`build_bis` 合并端点不校验两端类型与方向交替）、H10 收盘倒计时/`date.today()` 改用 `Asia/Shanghai`（新增 `cpt/domain/market_time.py`）；③ 中 29 项：web 限流/符号白名单/access log/并发上限/写库副作用显式、trade 回执 Content-Type+`for_date` 窗口+留存、异常原文不再回客户端（只回稳定代号）、inspection N+1 上限、LLM 总 deadline+响应体上限+`finish_reason` 校验、signal 并发 `pg_advisory_xact_lock` 去重、parity/水位口径、结构事件状态映射等；④ 低 18 项：systemd 沙箱（`ProtectSystem=strict` 等）、`innerHTML` 逃生口、cron 作业表更正为 6 条、nginx 模板补 `auth_basic`、curl 凭据不再进 argv、`dashboard.bundle.js` 一致性检查、每日 track 留存维护作业 + `MAINTENANCE_PATH`、市场时区残留、`transition_time` 索引（迁移 `scripts/migrations/2026-10-10_r59_signal_event_transition_time_idx.sql`，50k 行实测 1143→262 shared buffers）、节流表上限。⑤ 门禁：`check_doc_counts` / `check_doc_drift` / `check_line_refs` / `check_job_poll_unique`（`pollSpeak` 并入全站唯一实现 `CPTJob.poll`）/ `vulture` 全部转绿 —— 其中前三条在 HEAD 本就红。 |

> 缺口成因与 R43 那次一样：`docs/pending-wiring.md` 把本文指定为「唯一的轮次记录
> 台账」，但**没有任何检查保证台账与实际轮次同步** —— 门禁全绿，台账却断档。

---

## 1. 架构与分层决策

### 1.1 依赖方向

```
web > application > storage > adapters > domain
llm  独立于 web/application/storage，SQL 只经 cpt/storage
```

- `domain/` 只依赖 stdlib。`cpt/storage/` 只装 **CPT 自有表**（`public.cpt_*`）的读写。
- 共享的 A 股数据枢纽（`emotion_core` 的 28 张表，其中只有 2 张属 CPT）在
  `adapters/` —— CPT 是读者不是主人。
- **SQL 只许出现在 `adapters/` 与 `storage/`**（R24 立，`scripts/check_sql_layering.py` 守）：
  用 `ast` 定位 docstring 行范围 + `tokenize` 定位注释后抹白，再在
  `execute`/`executemany` 参数后 300 字符内找 SQL 起始关键字。
- `.importlinter` 共 6 条契约，**0 broken** 是硬要求。

### 1.2 store 层的两条硬约定

1. **store 层不 commit**，事务边界归调用方。每个调用点自己提交。
2. **写侧可以吞异常**（只影响「有没有落库」），**读侧吞掉会改变答案**（「查不到」
   会变成「没有」）—— 读路径必须原样抛出，降级上移到 HTTP 层。
   `cpt/storage/structure_event_store.py` 的模块 docstring 记着这两条策略的对照表。

门禁 `scripts/check_storage_failure_semantics.py`（R45）用 AST 扫 `storage/` 与
`adapters/`，找出「执行了 DB 语句、且 `except` 里不 raise 而返回空值/pass」的函数。
豁免必须写 `# gate: allow-silent: <理由>`，理由进 docstring。

### 1.3 事务语义（PostgreSQL）

一条语句在事务内报错 → 整条事务进 **aborted** 态 → 此后**任何**语句都抛
`InFailedSqlTransaction`，直到 rollback 解除（PG 18.6 实测）。后果是
「catch 住 DB 异常再返回空值」不是降级，而是把局部失败放大成整页失败。

三条由此派生的纪律：

- 共享连接上每个出错路径都要 rollback（`a_share_snapshot` 补了 8 处）。
- **降级必须发生在真正 `except` 异常的那一层**。中间层提前吞掉异常，上层挂什么
  补救都是摆设 —— `check_t_plus_one_calendar` 自吞异常，挂在调用方的回滚第一版
  完全无效。
- aborted 事务里 `COMMIT` **不抛、等于 ROLLBACK**，所以外层「防忘记提交」的包装
  救不了吞异常的 store。

### 1.4 跨层异常边界

`except` 块**内部**抛出的异常不会被兄弟 handler 接住 —— 让异常直接逃出
`build_ashare_snapshot` 比原来更糟。R56 的 P0：`structure_event_store` 读失败降级成
`{}`，在 PG 下把事务打成 aborted 并原样交还共享连接，导致后续十余处查询全废、整轮
逐只降级；现按 SQLSTATE 分流。

---

## 2. 口径（规则）决策

完整规则见 `docs/rules.md`。这里只记**在 force 的结论**与它们定下来的轮次。

### 2.1 结构计算

| 口径 | 现行值 | 来源 |
|---|---|---|
| 分型 / 笔 | **czsc** 实现（`CzscChanlunBackend`） | R14 |
| 中枢 | CPT 自研（`cpt.domain.zhongshu.build_zhongshus`） | R14 |
| 中枢延伸 | **不收缩** —— 区间由建枢的前三笔唯一确定 | R14 |
| `min_bi_len` | **6**，量纲＝**去包含后 K 线根数** | R14，R56 接线 |
| `min_elements_for_higher_bi` | **5**，量纲＝低级别结构元素数 | R14 |
| `zs_wzgx` / `divergence_compare` | `zgd` / `area` | 冻结于 v0 |
| 一买/一卖背驰 | czsc 笔力度口径（`power_price` 变小**且** `power_volume` 或 `length` 至少一个变小） | R14 |

**中枢不收缩**（R14）：旧实现延伸时 `high = min(...)` / `low = max(...)`，与已收缩的
区间比较后继续收缩，收敛到一个越来越窄的核，把一个大中枢切成多个窄区间。缠论原文
里中枢区间＝连续三段走势类型的**共同重叠部分**，延伸只延长不收缩。

**`min_bi_len` 真正对 native 生效（R56，取代 R16-4「native 不参与」的说法）**：
该门槛此前只对 czsc 生效，生产用的 native 一直静默丢弃。实测 native 口径下
35.3% 的笔是跨度 < 4 根的退化笔，而力度度量正是一买/一卖的**背驰比较输入**。
量纲是难点：门槛量纲是「去包含后 K 线根数」，而 `Fractal.bar_index` 是**原始** K
线下标 —— 包含关系合并 K 线，两者不可互换。`Fractal` 追加 `merged_index`，
缺失即抛错（`known-traps.md` #33）。

因此 `SCHEMA_VERSION` 从 `v0` 升到 `v1`（`cpt/domain/config.py`）。字段默认值一个
没变（`min_bi_len` 仍是 6），但仍必须升版 —— 升版挡的是「拿 v0 的 fixture 跑 v1
的代码，界面一切正常而每个数字都是错的」。

**按 bar 间隔分档（R56）**：一根 K 线代表多长时间取决于间隔，所以
`DEFAULT_MIN_BI_LEN_BY_INTERVAL = (("1d", 6), ("5m", 6))`，`RulesConfig.min_bi_len_for(interval)`
取档，未登记的间隔回落到 `min_bi_len`。5m 档用 90 天真实样本定档后确认保留 6。

**czsc 残留的短跨度不是 bug**：czsc `check_bi` 的门槛是
`if !ab_include && bars_a.len() >= min_bi_len` —— 两端分型 K 线互相包含时
（`ab_include`）门槛不适用。这是 czsc 的既定规则。

**必须传原始 K 线给 czsc**：czsc 内部自己做包含处理（`remove_include`），先跑
`merge_contained_bars` 会让包含关系被处理两次。周期由相邻 `open_time` 的**中位**
间隔反推（用中位而非均值，避免停牌/断线缺口带偏）。时间必须能反查回原始 K 线
下标，映射失败即响亮报错。

**只借分型与笔**：czsc 的 `zs_list` 虽 `is_valid()` 全过，但会产出 <3 笔的假中枢，
且 `ZS` 无 `bi_ids` 溯源。`test_real_fixture_zhongshu_never_has_negative_width` 钉住
这一事实。交叉验证比**区间宽**而不是纳入笔数 —— czsc 在退化中枢边界处会差 1 笔。

### 2.2 level 的单位按市场而异（R30）

`RulesConfig.levels = (5, 30)` 的**计算**含义（哪个相对层级的结构）在两个市场里是
同一套，域内只比较相对大小；**对不上的是展示单位**。

- 加密：`5 → 5 分钟级别`、`30 → 30 分钟级别`
- A 股：`5 → 日线级别`（**没有** `minutes` 字段 —— 日线不是「1440 分钟级别」）；
  `30 → 日线之上的高级别` 且标 `produced: False`，如实说未启用
- 未知市场：回落成「未标注级别（level=N）」，**绝不**默认按分钟解释

`cpt/domain/levels.py` 是唯一权威出处。LLM 提示词里带整张级别表 + 一条硬规则
（`a_share` 时 `level=5` 是日线级别不是 5 分钟级别），并让模型**照抄标签**而不是
自己换算 —— 标签诚实才敢照抄。

`cpt/domain/config.py` / `models.py` / `recursion.py` 三处 docstring 已改掉
「无条件写成分钟」的错话，指向 `levels.level_label(market, level)`。
门禁 `tests/test_domain_semantic_contract.py` 扫「无条件断言单位是分钟」的表述。

### 2.3 一买 / 一卖

- `cpt/domain/first_buy.py` 是移植 czsc `check_first_buy`/`check_first_sell` 的纯谓词
  （输入一串 `Bi`、输出 `bool`），不是 czsc 的信号模板调用。
- `cpt/domain/signal.py` 是**状态机**：它把 `has_two_centers` / `has_divergence_leg` /
  `has_reversal_bi` 当入参。三事实由 `cpt/application/first_buy_bridge.py` 的
  `derive_first_buy_facts` / `derive_first_sell_facts` **从结构对象现算**
  （`ZhongShu` 与 `TrendType` 都没有对应字段，必须现算）。
- 趋势方向取自数据（末笔方向），**不硬编码 `-1`** —— 最后一笔向上则不产信号。
- **未填充必须报错，不能静默返回 `False`**。`Bi.power_price`/`power_volume`/`length`
  默认 `0` 表示「未填充」，`_require_power_metrics` 会 `raise ValueError`。否则背驰
  比较会拿 `0` 参与运算并得出「不背驰」的错误结论。
- `divergence_status` 力度未填充时降级 `not_checked`，**不与 `not_detected` 混用**。

### 2.4 复权口径

**本地 `public.daily_bar` 是不复权原始价。** 后复权序列 = 原始价 ×
`asel.ref_adjust_factor.hfq_factor`（`raw × hfq_factor` 能精确复现腾讯 `hfq` 序列）。

判定复权口径**必须选跨除权日的日期**，否则 `bfq == qfq` 恒成立，任何比对都给出
「相等」的假阳性。

- 腾讯侧请求 `hfq`（与本地库口径一致）
- Wind 侧 `aftype="1"`，但**「都是后复权」≠「同一条序列」**：600519 / 2026-09-30
  不复权两边一致，因子 Wind 8.6469 vs 本地 7.0605 **差 22.47%** —— 差的是后复权的
  **起算基准**，跨家不可比。`scripts/factor_backfill.py::check_wind_basis()` 据此设闸：
  重叠 <3 天（=不知道）或中位比偏离 ±2%（=知道且不一致）就带数字拒绝写入。
  `wind:*` 源的因子零行，该路默认关闭（`--wind-fallback`）。

**成交量不复权**：除权日反复「补偿」成交量是噪声，仅 OHLC × 因子。

### 2.5 A 股规则标签

`cpt/domain/a_share_rules.py` 只留纯逻辑（`AShareDailyTag` /
`apply_ashare_tags_to_bis` / `t_plus_one_purchase_allowed` / `AShareTagsError`）；
所有 SQL 下沉到 `cpt/adapters/a_share_local.py`。

- **C2 涨跌停**直接读 `public.derived_bar` 的 `is_limit_up`/`is_limit_down`/
  `is_bomb`/`is_one_word`，不重算 ±10/20/30% 阈值（随板块变化，重算易错）。
- **C3 停牌 / C5 非交易日**：`public.daily_bar` 是交易日表，停牌日本来就没行。
- 标签以合成 id 挂到 `Bi.source_ids`（如 `ashare:is_limit_up:2026-09-21`），
  **不改 `Bi` schema**，保持跨市场一致。端点匹配用 `Bi.end_time`（毫秒）转 ISO date
  查表，**不是 `start_time`**。
- 审计块 `data_quality.ashare_tags` 刻意区分三种「没标签」：客户端没实现
  （`client_unsupported`）/ 查了但失败（`tag_fetch_failed`）/ 查通了但区间内确实没有
  极端日（`available: true` + `tagged_bis: 0`）。
- `t_plus_one_purchase_allowed` 是**全仓唯一零生产引用的公开符号**（R22 起至今未接）。
  真正的 T+1 日历读点在 `check_t_plus_one_calendar` + `_attach_t_plus_one`。

---

## 3. 存储与 schema

### 3.1 CPT 自有表（`cpt/storage/`，8 个模块）

| 表 | 用途 | 迁移 |
|---|---|---|
| `public.cpt_signal_event` | 信号状态跃迁事件（`Signal` 落库） | `scripts/migrations/2026-10-01_r21_signal_event.sql` |
| `public.cpt_dashboard_run` | 运行快照（`/compare` `/multi-run` 跨重启可比） | `scripts/migrations/2026-10-02_r23_dashboard_run.sql` |
| `public.cpt_structure_event` | 结构变化事件流（唯一真相） | `scripts/migrations/2026-10-05_r26_structure_event.sql` |
| `public.cpt_llm_call` | LLM 调用审计（12 列） | `scripts/migrations/2026-10-03_r25_llm_call.sql` |
| `public.cpt_run_metric` | 水位表（每轮一行，一表装两轨） | `scripts/migrations/2026-10-06_r56_cpt_run_metric.sql` |
| `public.cpt_factor_epoch` | 因子口径切换点登记 | `scripts/migrations/2026-10-03_r45_factor_epoch.sql` |
| `public.cpt_recommendation` | 推荐记录 | `scripts/migrations/2026-10-05_r45_recommendation.sql` |
| `public.cpt_structure_event_id_backup_20261001` | 市场前缀迁移的旧 id 映射（回滚用） | 同 R27 迁移 |

- 迁移风格：`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`，可重复跑。
- **不建状态表**：事件流是唯一真相，当前状态从事件流派生 —— 两张表必然出现
  「状态表说 A、事件表说 B」。
- 列纪律：能从 jsonb 现抽的一律不建列（`cpt_dashboard_run` 建表时只 5 列，12 列方案被叫停）。
  ⚠️ **R57 补了第 6 列 `created_at`** —— 不是违反这条纪律，而是 R56 放开
  `generated_at` 的 NOT NULL 之后，那批 NULL 行**没有任何字段能让它变老**，
  保留期永远命中不了；`created_at`（`DEFAULT now()`，入库时刻）补上了这个缺口。
  见 `scripts/migrations/2026-10-07_r57_dashboard_run_created_at.sql`。
- `scripts/migrations/` 之外，仓里**没有任何 `.sql`**。

### 3.2 共享 A 股数据（只读）

| 表 | 代码用到的列 |
|---|---|
| `public.daily_bar` | code/date/open/high/low/close/volume/amount（另有 pre_close、turnover_rate） |
| `public.derived_bar` | code/date/is_limit_up/is_limit_down/is_bomb/is_one_word |
| `public.trade_calendar` | date/is_open（13,162 行，1990→2026 底） |
| `public.hot_rank` | date/code/rank |
| `public.ladder_day` | date/code/cont_days |
| `public.limit_pool_em` | date/code/name/cont_days_em/pool_type |
| `public.strategy_signal` | trade_date/code/strategy/name/action/score/confidence/reason/model |
| `asel.ref_adjust_factor` | code/trade_date/**hfq_factor**（列名是 `hfq_factor`，不是 `adj_factor`） |
| `asel.security_master` | 5,930 行，带 `board`，证券名 + 板块 |
| `asel.ref_adjust_factor_v2` | **暂存表**，重算专用，生产代码不读 |

R31 逐条向真库 `information_schema` 核过：8 张表、47 个列引用**零漂移**。
`date`/`trade_date` 类型都是 `date`（不是 text/timestamp），所以
`BETWEEN %s AND %s` 传 `datetime.date` 成立。

`public.daily_bar` 全仓无任何 INSERT —— CPT 只读，坏数据只能在读取侧拦（R53）。

### 3.3 结构事件流

- `structure_id` 形状 `{market}:{kind}:{level}:{start_time}`，`MarketKey = Literal["cn", "crypto"]`。
  **market 是必填参数、不给默认值**（默认值等于留后门给下一个调用方，踩中后症状是
  「每轮都在写新 created」、看板完全正常）；非法 market 直接抛 `ValueError`。
- 事件行写 `occurred_at` 会并列（同批 `executemany` 一次写进），倒序键用
  `(occurred_at, id)`，id 是 bigserial 单调。`kind` 过滤走 `payload->>'kind'`、
  **不加索引**（表只在结构真变时追加，增长极慢；加列会破坏「表不存 kind / 不存
  market」的口径）。
- **id 确定性生成**是整条线的地基：domain 层零时钟零随机 ⇒ 同输入必同输出 ⇒ 同 id
  ⇒ 幂等重放成立。
- **写库失败仍返回事件**：`snapshot.events` 回答「本次算出了什么变化」，与「能不能
  落库」是两件事。代价是这批事件不在事件流里，跨重启追溯查不到（docstring 写明）。
- **消失的结构不记事件**：「曾经有、这次没有」的原因太多（级别切换、递归参数变了、
  bars 重算），没把握一律记 `invalidated` 会污染事件流。宁可少记。
- `snapshot.events` 的语义是「**本轮**算出了什么变化」，稳态下多为空是正常的。
  看板两个面板刻意并存：「结构事件（本轮变化）」= `snapshot.events`（回放时间轴
  依赖它），「结构事件流（累计）」= `cpt_structure_event`（跨重启可比）。
- `cause` 四类：`data`（输入变了）/ `config`（规则参数变了）/ `backend`（结构后端换了）
  / `code`（三者都没变却仍变 ⇒ **只能**归到算法自己）。`code` 是**残差归因**不是
  检测到的，判据与依据写在模块 docstring。
- 只接**真正上服务的快照**（加密侧 `_RealtimeProvider._poll_once`、A 股侧
  `build_ashare_snapshot`）。另外 5 个 `build_dashboard_snapshot_v2` 调用点刻意不接 ——
  视图不是状态，一个结构在一个时刻只有一个状态。

### 3.4 信号事件

`record_signal_event` 只在 status 变时 append（同 status 返回 `False` 不碰 DB）。
`event_time=0` 兜底墙钟。

**R56 修掉的死功能**：「信号状态跨轮询变化」自 R21 起一直是死的 ——
`public.cpt_signal_event` 没有 `event_time` 列（真实列是 `transition_time` 等），
`ORDER BY event_time` 报 `column does not exist` 被 `except` 吞掉且只记 debug。
R45 修了 SQL 排序，**R56 才发现功能仍然是死的**：写完再读自己，前值必须在写之前
捕获（`latest_status()` 单列投影 + `ORDER BY id DESC`，不用 `load_previous_signal`）。

`confirmed → forming` 也不落事件：状态确实变了所以 `updated` 那条同样不成立，
事件流永远停在 confirmed，**对外报出一个不存在的状态**。R56 已修。

---

## 4. 数据源与因子管线

### 4.1 源状态是一等对象

`cpt/adapters/source_registry.py` + `GET /api/dashboard/sources`：

- `SOURCES` 是**声明**（静态、可单测、不联网）；`probe_source()` 是**实测**。
- 任何异常都折叠成 `status`（`ok` / `degraded` / `unavailable` / `skipped`），
  **绝不让该端点 500**。
- **Wind 默认 `skipped`**：一次探测就是一次真实万得额度，必须显式 `?include_quota=1`
  才允许（默认路径绝不花配额，有测试钉这条纪律）。
- 探活结果 60s 进程内缓存，`?refresh=1` 强制重探。
- `known_dead_endpoints` 留档已知不可用端点。

| 源 | 角色 | 备注 |
|---|---|---|
| `binance_futures` | 加密 primary | |
| `ccxt` | 加密 fallback | 需 extra `crypto` |
| `tencent_kline` | A 股 fallback | 腾讯 `web.ifzq.gtimg.cn` fqkline |
| `sina_quote` | A 股 fallback | `hq.sinajs.cn` |
| `a_share_local` | local | 读 `public.daily_bar` |
| `wind` | A 股 primary | 默认 `skipped`（额度） |

**`degraded` 顶成 P1**（审计侧）：合法状态不等于健康状态。

### 4.2 探活的正确判据

- **缺整天检测走 `public.trade_calendar`**，不是 weekday 启发式 —— 用 weekday 会把法定
  休市日报成「缺整天」并把整个源标 `degraded`（假警比不报警更贵）。
  `a_share_local.open_days_between()` 一次查区间（不逐日往返）。日历不可用时退回 weekday
  口径**但必须说明退回**。新增 `missing_trade_days` / `trade_calendar_available` 两键，
  `missing_weekdays` 保留兼容。
- A 股 K 线缺口的真实量级是 **11.9%**（623 只、平均缺 7.4 天、最多 62 天），
  **在 `daily_bar` 而非 `derived_bar`**（`known-traps.md` #23）。

### 4.3 腾讯 fqkline 的字段顺序

`["日期, 开, 收, 高, 低, 量]` —— **不是 OHLC**。按 OHLC 解析会得到「最高价 <
收盘价」的坏数据**且不会报错**。适配器逐根做 OHLC 自洽校验，不自洽就报错。

顶层 key：`bfq` → `day`；`hfq` → `hfqday`。先按 `d.get(f"qfq{adj[1:]}")` 猜 key 会
得到「0 根」假象。

**后复权可用性是逐标的属性，无法用代码前缀预测**（实测 688111/688036 有 hfq、
688981 没有；多数 301 有、近期新股没有；北交所 `920201` 连 `day` 都基本没有）。
所以实现里**没有任何板块判断** —— 拉一次，然后如实报告腾讯实际返回了什么。
任何「按板块硬编码」的优化都会重犯这个错。

### 4.4 因子管线

| 脚本 | 作用 |
|---|---|
| `scripts/factor_backfill.py` | 从腾讯 raw+hfq 算因子并 upsert 生产表；`--mode full\|incremental`、`--wind-fallback`（默认关）、`--retries` |
| `scripts/factor_recompute.py` | 按公司行动**重算**，只写暂存表；`--scope placeholder\|all\|wired`、`--source`、`--max-calls` |
| `scripts/factor_report.py` | 逐票对账（比暂存表 `asel.ref_adjust_factor_v2`） |
| `scripts/golden_set.py` | 结构指纹基线，`--build` / `--check` |

**重算只写暂存表，绝不自动切换生产表**（R37 起的纪律）。切换是人工决定。
切换点登记在 `cpt_factor_epoch`。

**因子应当分段常数**（按定义：`f = Π(1+送转)/(1−每股派息/除权前收盘)`），库里存的是
`hfq_close/raw_close` 这个**逐日比值**，段内有 ~0.1% 漂移。逐日重算会改 89%~100%
的行、最大相对改动约 3%。

三处会让全量回填**静默空转**的坑（已修）：

1. `list_all_a_codes` 查 `asel.daily_bar_raw` —— 该表不存在，真表是 `public.daily_bar`。
2. 增量过滤用 `trade_date > max(trade_date)`，腾讯返回的 801 天会被全过滤掉、脚本打
   「新增 0 行」返回 0，看着像成功。改按 `source IS NULL` 覆盖。
3. 脏数据防护必须收口到一处实现（两处各写一份 h/r 循环，挡不住腾讯整根退化）。

**腾讯 hfq 会整根退化**（扫 40 只票 15 只中招）：hfq 那根与 raw 逐字相同，或比值乱成
量级错误。判据用**方向**不用幅度 —— `_looks_degenerate(raw_ret, hfq_ret, tol)` =
`abs(hfq_ret) > abs(raw_ret) + tol`。**任何幅度单阈值都必然在漏脏与误伤除权之间二选一**
（002594 真送转比值跳 203.55%，比任何脏数据都狠），而真除权（raw 假跳空、hfq 被调
连续）与腾讯退化（raw 正常、hfq 单根崩）方向相反且稳定。脏日**不推进锚点**。

**东财免费真值源**（R39，取代 Wind 作主源）：`datacenter-web.eastmoney.com` 的
`RPT_SHAREBONUS_DET`，从大阪直连 200，零额度。`cpt/adapters/corporate_actions.py`
（共享模型 `CorporateAction` + `ex_div_ratio` + `filter_implemented`）与
`cpt/adapters/eastmoney_actions.py`（HTTP 客户端）。

- **单位陷阱**：`PRETAX_BONUS_RMB` 是**每 10 股**（茅台 2024-12-31 报告期 276.73 ⇒
  每股 27.673 元）。忘了除 10，因子差一个数量级。
- **`BONUS_IT_RATIO` 是送 + 转的**总和**，`BONUS_RATIO` 是送股那部分，`IT_RATIO` 是
  转增那部分。取 `BONUS_IT_RATIO` 当送股再加 `IT_RATIO` = 同一个 4 记两列，**因子直接
  大一倍**（R41）。修后 16%~29% 的台阶差 → ±0.21% 以内（`known-traps.md` #9）。
- 列集合**按标的甚至按次而变**，取值一律**按列名子串**，绝不按下标。
- `A or B` 里若 `A` 是**列索引 0**，它会被当成「没找到」⇒ 静默返回空元组 ⇒ 上层报
  「无公司行动记录」—— 一个看起来像数据缺失的错误，而数据就在那儿
  （`known-traps.md` #26）。
- **网络不可达与「返回坏数据」是两件事**：`EastmoneyActionUnavailable` 归 transient
  可重试，解析失败确定性不重试。**`except` 的顺序有语义** —— `Unavailable` 是 `Error`
  的子类，`except fatal` 写在前面会吃掉一切（`known-traps.md` #11、#13）。
- 「窗口内没有公司行动」⇒ 因子恒定，这是**对**的结果，不是失败
  （`known-traps.md` #15）。报告字段叫 `new_constant` 不叫 `new_placeholder`。

**归一化锚点**：库里因子锚在**最早**一根，按定义算的锚在**最新**一根，两者差约 196 倍。
因子是乘在价格上的，直接换表会让每张图每个价格缩放 ~200 倍，所以 `reanchor()` 把新
因子**重标定到与库里一致的锚点**。

**切表是「替换」不是「合并」**：暂存表**不是**生产的超集（生产因子表从 2023-06-15 起，
`public.daily_bar` 从 2024-01-02 起，多出的 134~6862 行是**没有对应 K 线的孤儿日期**）。
合并会把过期孤儿行留在生产表里，正是「同一接口分裂成两个标签」的脏数据形态。

### 4.5 因子表的当前状态

四次切表（2026-10-03 ×2、10-04 ×2）后：

| 指标 | 值 |
|---|---|
| 票数 | **5206** |
| 行数 | **3,339,427** |
| placeholder（`source IS NULL`） | **0** |
| legacy `tx:fqkline` 残留 | **0**（已用东财补回） |
| 因子向下跳 | **0 只 / 5206 只**（全部单调非降） |
| 仍缺 | 17 只新股（K 线不足 30 根，ingest 侧的事） |

**切表核对不能只看 `IS DISTINCT FROM` 的行数** —— 那把「值不同」和「表示精度不同」
混成一个信号（生产表 `numeric(12,8)` 截掉最后 4 位小数，报出 221 万行假警报，而相对
误差最大只有 1.3e-8）。该看**相对误差**和**业务量**（台阶数）。

⚠️ **「因子向下跳 2544 只」是判据方向反了，不是数据问题**：那个判据用
`min(hfq_factor) < 0.999`，但后复权因子从最新除权日往前累乘，任何有过分红的票最早
那天的因子都远大于 0.999。正确判据是 `lag()` 逐日比对 `hfq_factor < prev` ⇒ **0 只**。
纪律：**判据本身要被验证，不能只验证「用它得出的结论」；自洽的错比不自洽的错更难发现。**

### 4.6 A 股按需补因子

```
GET /api/dashboard/a-share/snapshot?code=XXXXXX
  └─ build_ashare_snapshot(code)
       ├─ 1. 读本地因子
       ├─ 2. 覆盖不足（整段缺 **或部分缺**）→ 腾讯 raw+hfq → upsert（幂等，主键 code+trade_date）
       ├─ 3. 重读 → 生成快照
       └─ 4. 仍无 → 区分 unsupported / cooldown / fetch_failed
```

- **部分缺也补**（`skipped_no_factor` 非空即触发）：K 线序列中间有空洞比整段缺更隐蔽 ——
  画出来的笔/中枢是错的，但图上看起来「有东西」。
- **默认关**：application 层 `build_ashare_snapshot` 不传 `ensure_factors` 就绝不联网、
  绝不写库；只有生产入口 `cpt.web.a_share_routes.snapshot_payload` 用
  `factor_ensurer_from_env(default=True)` 显式打开，由 `CPT_ASHARE_ONDEMAND_FACTOR` 兜底。
  测试会话用 autouse 夹具把它置 0。
- `unsupported` 是**永久**结论（同进程内不再变），单独存 `_unsupported` 字典；
  只有**可重试**的失败才走冷却。否则同一只票两次请求报不同原因，审计断言不可重复。
- **不补日线**：`public.daily_bar` 的入库是另一条链路（`lkl` ingest）。
- **不做全市场批量**（5,225 只，已明确否决）。

### 4.7 失败语义（A 股特有）

全库 5225 只里大部分缺复权因子，「点进去是空图」是常态，所以失败必须分类：

| reason | 含义 |
|---|---|
| `no_factor` / `no_factor_unsupported` | 有行情但整段缺因子（腾讯确实没有该标的 hfq） |
| `no_factor_cooldown` | 之前失败，退避中 |
| `no_data` | `public.daily_bar` 里没有该代码 |
| `placeholder_rows` | 全部 bar 都是零价占位行 |
| `db_error:*` | DB 不可达，保留原始异常类型名 |
| `invalid_code` | 代码格式非法，HTTP 400 + JSON 体 |

**合法状态 ≠ 健康状态，失败 reason 必须比粗糙的 reason 更有害**（`known-traps.md` #19）。
热门池里缺因子的票**仍然列出但禁用并标注** —— 直接过滤会让人以为池子少了票。

**零价占位行**（R53）：`public.daily_bar` 有 35 行 / 18 只票 `open=high=low=0`
（2026-09-28~09-30 三天，上游批量坏掉）。分两档危害：17 行 `close≠0` 被校验器拦下，
**18 行 `close=0` 满足 `0<=0<=0` 被放行**，零价线进结构计算 ⇒ 假分型/假笔/假中枢、
零报错。`cpt/adapters/a_share_local.py` 新增 `ASharePlaceholderRowsError`，
循环里丢弃 `O/H/L` 同时为 0 的行，**判据不看 close**；`AShareFetchResult` 加
`skipped_placeholder` 字段（duck-type 兼容）；部分丢弃打 WARNING、全部丢弃用
`placeholder_rows` reason。

### 4.8 候选池三源合并

| 来源 | 取数 | 口径 |
|---|---|---|
| 热门池 Top5 | `public.hot_rank` 最新日 ∪ `public.ladder_day` `cont_days>=2` | 按 rank 排，hot_rank 优先 |
| 手输（自选） | `~/.cache/cpt/watchlist.json`（`CPT_WATCHLIST` 可覆盖） | 加入时间倒序 |
| 策略综合 Top5 | `public.strategy_signal` 最新日 | **先滤 `action∈{BUY,WATCH}`，再按 `评分×0.5+置信×100×0.5` 排序** |

- 合并去重**是必须不是优化**：同一只票会同时是热门池 #2 和策略 42 分，不去重会出现
  两个 `value` 相同的 option、`selected` 归属随机 —— 正是「选错票」最容易发生的地方。
  每条带 `sources: [...]` 与 `group`（optgroup 归属）。
- **分组优先级 `manual > hot > strategy`**：手输排第一，否则用户会以为手输又丢了。
- 自选用 `fcntl.flock` 单进程安全 + JSON 落盘，幂等添加，自动建父目录。
  **不是 localStorage**。
- 三个来源的失败**互不影响**（`factor_error` / `strategy_error` / `watchlist_error` 分别记录）。
- `public.strategy_signal.name` 是**策略名称**（「默认多头趋势」）不是股票名，股票名要
  另查 `asel.security_master`；`confidence` 是 `numeric(4,3)` → psycopg 回 `Decimal`，
  排序与 `json.dumps` 前必须转 float。
- 证券名归一化**删掉全部空白**（老行情源按 4 字宽补齐，80 条带填充空白如 `深 赛 格`、
  `ST 中 侨`、`万  科Ａ`）。已核对 80 条里 `[A-Za-z] +[A-Za-z]` 匹配数为 0，删除不吃
  掉真实空格。名称是装饰，**绝不允许它把快照搞挂**。

---

## 5. LLM 层

`cpt/llm/`（8 个模块），SQL 只经 `cpt/storage/llm_call_store.py`；`.importlinter`
6 条契约含「LLM 不许 import storage」。

### 5.1 队列与重试

- provider 为 OpenAI 兼容。实测形态：**429 响应体为空、无 `Retry-After`**、限流是
  **令牌桶**（恢复后单请求仍有约 1/8 概率吃到 429）、6 并发全过 / 15 并发 3 过 12 个 429
  ⇒ 队列用**单 worker** 串行。
- 退避上限 `backoff_max` 默认 **60 秒**。真机验证（本地 429 桩，走完全相同的生产
  代码路径）覆盖两个场景：耗尽（3 次后 `rate_limited_exhausted`）与**恢复**（3 次后
  `ok`）。只验「耗尽」验不出恢复路径能不能救回来，而生产要的正是后者。
- 写操作**必须 commit**（store 层不 commit 是本仓约定）。已用 `_write` 包装器收口，
  并有两条测试：一条对着源码断言防「漏掉 `_write`」，一条**真调一遍**防「`_write` 用错」。
- **worker 线程绝不能因一个任务死掉**（R28-4）：`LLMQueue._run` 调 `_execute` 时没有
  try/except，逃出去的异常会让 worker **永久退出**，之后 `submit()` 仍返回
  `accepted=True`、任务永不执行、**任何地方都不报错**，同一进程后续所有提交静默丢失。
  现 `_run` 加 `BaseException` 兜底 + `_LOG.exception` + 尽力把这次调用标 `error`；
  `submit` 用 `_worker_alive()`（**不只看 `_stop`**，那只反映「被人正常停掉」）在
  worker 不可用时如实拒绝 `llm_worker_unavailable`。

### 5.2 结构化输出

`cpt/llm/structured.py::parse_structured` 的核心纪律：**schema 校验是每一次解析尝试
的一部分**，不是解析之后的独立步骤。

候选按优先级：`whole`（整段就是 JSON）→ `fence`（```json 围栏）→ `embedded`（文本里
配平的 `{...}`）。每个候选都要**同时**满足「能解析」+「schema 通过」才算命中；
整段能解析但 schema 不符判为**这次尝试失败**，继续试下一个；全都不行才报
`schema_mismatch`。原因码：`ok` / `empty` / `not_json` / `truncated` / `schema_mismatch`。
**全程不抛** —— 解析器抛异常会把「模型输出不合预期」变成「看板 500」，方向完全反了。

- **最危险的失败是静默的**：模型无视 schema、把**输入**原样包一层吐回来时
  `json.loads` 成功、`.get("summary")` 返回 `None`，一路传到前端某字段显示空白才发现
  「结构化摘要一直是空的」。围栏/截断/散文反而显眼（`json.loads` 直接抛）。
- **截断不「就地补括号硬救」**：截断的 JSON 语义残缺，硬救出来的数据是编的。
  `truncated` 是独立原因码，交给调用方降级到原文展示。
- **扫描器必须手工扫**，不能 `text.find('}')`：字符串字面量里可以出现 `}`、`{`、`\}`。
  `_scan_balanced` 逐字符跟括号深度并跳过字符串字面量与转义；`_inside_open_container`
  只取顶层容器。
- 22 条测试的夹具**全部是真机抓包**（`tests/fixtures/llm_structured_samples.py`）。
- **解析器管不了的部分**：只回显标的、一个字的解释都没有，语法正确字段齐备 ——
  「内容有没有用」是质量判据，得在用例层另外做（长度下限 / 与输入的相关性）。

### 5.3 配置与密钥

| 变量 | 作用 |
|---|---|
| `CPT_LLM_ENABLED` | `1` 才启用，`0`/未设 → 整个层短路（`queue.submit()`） |
| `CPT_LLM_API_KEY` | 密钥，不入库、不入日志 |

- `api_key` 用 `field(repr=False)` 防止进 repr；`redacted()` 只回 `has_api_key`。
- `list_calls` 特意回 `unavailable_reason` + `config.redacted()`：**最费时间的就是分不清
  「没 enable / 没 key / base_url 写错」**。
- `llm/config.py::load_config` **不得**用 `os.environ.clear()` 实现「传 dict 就只读这份
  dict」—— CPT 是多线程的，任何线程在 clear/update 之间读环境变量都会拿到残缺环境
  （含 `DB_PW` / 飞书 webhook）。实测并发读者有 520 次读不到 `DB_PW`。
- `cpt/llm/__init__.py::get_queue` 见到 `_QUEUE` 非空就**直接 return**，会把后传的
  `on_status` 静默丢弃。`list_calls()` 不传回调且是看板打开就轮询的处理函数 ⇒ 谁先跑
  谁定 ⇒ 用户先开看板再点「解释结构」，队列带**空回调**建好，`on_llm_status` 永不注册
  ⇒ **LLM 跑完了但状态永远不落库，每条卡在 queued**。
- `queue._execute` 的 `except LLMRateLimited` 必须排在 `except LLMError` **之前**
  （子类在前）。
- `openai_compatible.py` 的错误分类是按**真机抓包**定的。

### 5.4 前端

- 「解释选中的结构」端点是 **A 股专用**（路由在 `a-share/` 下），所以按钮只在
  「当前是 A 股代码 + 选中了 bi / zhongshu / trend_type 之一」时可点。
- **在途任务要继续轮询**：有非终态（`queued` / `running` / `rate_limited`）就 2 秒后
  再拉，终态集合写死 `LLM_TERMINAL = {ok, error, interrupted}` —— 漏一个的后果是该状态
  的任务被永远当成在途，面板无限打接口。

---

## 6. Web 与看板

### 6.1 错误响应契约

**所有 API 错误响应都是 JSON**（R29 全改，不是只修一处）。`web/app.py` 里 17 处
`send_error` 全部换成 `_write_json_error`（13 处带 message、4 处无），每处补稳定的
`error.code` slug。

- `BaseHTTPRequestHandler.send_error()` 把 message 写进 HTTP **状态行**，而状态行只能
  latin-1 编码 —— 任何中文提示都会抛 `UnicodeEncodeError` 并**直接断连接**，浏览器侧
  只看到 `RemoteDisconnected`。所以错误消息必须纯 ASCII，中文一律走 JSON 体。
- 门禁 `tests/test_web_error_contract.py` 是**运行时契约**（起真 server、打真请求、
  查真 `Content-Type`），不是源码 grep —— grep 只证明「没调用」，证明不了真返回了 JSON。

### 6.2 画布

画布 A/B/C 由 `dashboard/canvas_registry.js`（`window.CPT_CANVASES`）注册，
必须在其它画布模块**之后、`dashboard.bundle.js` 之前**加载（boot 要读到完整注册表）。

| 画布 | 实现 | 中枢表达 |
|---|---|---|
| A | 原生 SVG | SVG rect |
| B | lightweight-charts（vendored） | 两条虚线边界（LWC 无矩形图元） |
| C | plotly（vendored，`plotly-finance-dist-min`） | `layout.shapes` 真矩形 |

- **画布 D（wbt 报告）已于 R51 整体下线**：`/api/canvas/wbt` 端点除名（外部调用拿到
  404），`cpt/application/canvas_wbt.py` 与 `dashboard/canvas_d.js` 一并删除，
  `pyproject.toml` 的 `report = ["wbt==0.9.1"]` extra 也删了。删除它顺带根除了 CI 五连红
  的第一根因 —— 它是全仓唯一 import pandas/plotly 的地方，两处 import 都写在 `try` 之外。
- **`dashboard/dashboard.js` 已在 R45 拆成 `dash-*.js` 等模块，R51 删除**（拆分后已无入口
  孤儿）。当前入口是 `dashboard.bundle.js`。
- 画布模块**不自己取数**：`tests/test_dashboard_canvas_contract.py` 遍历 A/B/C 断言源码里
  不出现 `fetch(` / `XMLHttpRequest` / `/api/`。数据一律由 `buildCanvasView()` 归一化后
  喂进去 —— 各画布各自归一化的话，「渲染一致」就无从证明。
- 每个画布的 `draw(view)` 必须返回**自己真正画出来的**元素个数，不是「快照里有几个」。
- vendor 资产在 `dashboard/vendor/`（含 sha256 清单与许可证，测试逐字节校验），
  **断网可用：`externalRequests = 0`**。

### 6.3 静态资源

当前 `dashboard/` 顶层：`canvas_b.js` / `canvas_c.js` / `canvas_registry.js` /
`cpt_job.js` / `dash-alert.js` / `dash-chart.js` / `dash-core.js` / `dash-ops.js` /
`dash-signal.js` / `dash-structure.js` / `dashboard.bundle.js` / `dashboard.css` /
`index.html` / `inspection_panel.js` / `market_a_share.js` / `url_safety.js` + `vendor/`。

- 部署流程在 `deploy/dashboard-sync.sh`（R51 修过：它**一直在同步线上根本不加载的文件**，
  却漏掉了真正在线的 `dash-*.js` + `dashboard.bundle.js`）。
- 部署用 `tr -d '\r'` 归一化换行后再 `install -m 644`。**跨机器比文件大小前先统一换行**，
  否则每次部署检查都在演假警报。
- `chmod -R u=rwX,go=rX`，**不要**用 `chmod 644 /var/www/cpt-dashboard/*` ——
  通配符会把 `vendor/` 目录的执行位也去掉，Nginx 无法穿越该目录 ⇒ `vendor/*.js`
  全部 403。HTML/CSS 看起来完全正常，只有 console 里十几条 error 才暴露。
- 唯一的前端轮询实现在 `dashboard/cpt_job.js`（门禁 `check_job_poll_unique.py` 钉住唯一性）。
- 唯一入队骨架由 explain/summarize 共用（门禁 `check_enqueue_skeleton_unique.py`）。
- **`index.html` 的 `<script>` 标签必须成对且不吞后续标签**（门禁 H 类）：标签被前一个
  标签当成文本吞掉 ⇒ 该文件从未被请求、`window.CPTJob` 恒 undefined、轮询走「缺它就
  隐藏」的兜底 ⇒ 页面正常、接口 200、**console 零错误**，只有一个功能悄悄没了
  （`known-traps.md` #30）。

### 6.4 A 股主看板

接入点是**换掉 `loadSnapshot()` 的 URL**（A 股快照与加密侧同构 `dashboard.v2` schema），
四个画布零改动 —— 这条性质由 `tests/test_dashboard_ashare_contract.py` 钉住
（`canvas_b.js` / `canvas_c.js` 里不允许出现 `a_share` 或 `market` 字样）。

- `market.kind` 走通用 `data-field` 循环，一套管线通吃两个市场。
- 宽度默认 **120 根**（30 根只能出 2 笔，笔/中枢形态根本看不出来）。
- 证券名显示在三个「确认身份」的位置：顶栏「标的」、代码下拉、页面标题。
  `<dt>交易对</dt>` 改成 `<dt>标的</dt>`。
- **A 股模式下顶栏的加密交易对下拉必须隐藏**（不隐藏时用户在 A 股模式下看到
  BTCUSDT/ETHUSDT/SOLUSDT，而画布画的是 A 股 —— 这正是「看岔」的来源）。
- 前端读的键是 snake_case（`close_countdown` / `dual_compare` / `rules_version`）。
  曾经有三处把本地 camelCase 视图模型**泄漏到原始 API 对象**上，导致取到 `undefined`：
  倒计时掉进兜底分支显示「距下一根 K 线」（一个看着挺合理的错标签）、双数据集面板
  永久隐藏、规则版本永远显示「—」。

### 6.5 面板与降级

- 收敛倒计时优先用 `snapshot.closeCountdown`（A 股），回退到 K 线间隔（加密）。
- 双数据集对比（`dual_compare`）：CPT 后复权收盘价 vs 上游**同口径**现价。
  上游现价要**乘最后一根 bar 当日的因子**再比（`a_share_local.hfq_factor_on()`）——
  不乘就是「市场跌了 86%」的假数字。查不到因子就说不知道（新 reason
  `factor_unavailable`），不给假数字。源为**新浪快照**（东财 `push2.eastmoney.com`
  从大阪机 502；`push2his` 历史接口是 200 ⇒ **按 host 分别封，不是整站封**）。
- `market_24h` 上游不可用时显示「—」+「A 股暂无数据源」，**不用 window 兜底算一个假涨跌幅**。
- `inspect` / `signal-stats` / 结构事件流等重面板**不进 30s 轮询**（只 boot 首次 + 手动
  按钮），避免每轮多几个请求。
- 降级分三态渲染：`available=false`（后端不可用）/ 有响应但空 / 从未拉取成功
  （整块隐藏）。**降级文案不能和「空」渲染成同一句话**。

---

## 7. 部署与运维

### 7.1 服务

- 后端 `127.0.0.1:8010`（`cpt-dashboard.service`，systemd 托管，`ActiveState=active` /
  `UnitFileState=enabled` / `PPID=1`）。**不要用 8000** —— 已被另一个项目占用。
- 模式 `realtime`（已可用），`CPT_POLL_SECONDS=30`，`CPT_SYMBOL=BTCUSDT`，
  `CPT_INTERVAL=1h`。另有 `cpt-dashboard-ashare.service` + `.timer`（每日）。
- 模板：`deploy/systemd/`、`deploy/env/cpt-dashboard.env.example`、`deploy/nginx/cpt-dashboard.conf`。
  真实 `.env` 被 `.gitignore` 挡住（**这是有意的**），只提交 `.example`。
- **nginx 模板必须含 `location /cpt/api/`**（最长前缀匹配）。

### 7.2 认证

`/cpt/` 挂 `auth_basic`（R30 拍板方案 A）。**三个 location 块必须都挂**：`/cpt/api/`
是独立且更具体的 location，只给静态块加认证会出现「静态 401、API 照样 200」，
**看起来做了、其实没做**，比完全没做更危险。内网直连 8010 不受影响。

**不能用 URL 内嵌凭据（`https://user:pwd@host/`）驱动页面**：相对 `fetch` 会继承
凭据，浏览器直接抛 `Request cannot be constructed from a URL that includes credentials`。
浏览器自动化验这个看板必须让代理在服务端加 `Authorization` 头。

### 7.3 Wind 通道

- 取数走 `cd <wind-mcp-skill> && node scripts/cli.mjs call <server_type> <tool> '<params_json>'`；
  成功时数据在 `content[0].text`（JSON 字符串），失败时 stdout 是 `{"ok":false,"code":...}`。
- **必须显式配 `CPT_WIND_NODE`**（`deploy/env/cpt-dashboard.env` 里指向
  `/home/ubuntu/.local/bin/node` 软链，不写死 nvm 版本号）。systemd 给的 `PATH` 里没有
  node，不配则每次调用都死在 spawn 上。
- `WindSourceClient.availability()` 多查 `node`（`CPT_WIND_NODE` 可指定，裸名走 PATH），
  缺了就说 `wind_node_missing:<名字>` —— **要判断一个通道能不能跑，就得把真正要执行的
  那个东西也查一遍**。
- **额度不足必须与「源坏了」分开报**：Wind 的「试用已到期」不带任何英文关键词，
  `_QUOTA_MARKERS` 专门收录中文提示，否则运维会去查网络而不是去充值。
- 真实回执是第三种形状（`{"data": {"columns": [...], "rows": [...], "unit": {}}}`）；
  表格检测必须放进解包循环**内部**、按 `columns[].name` 建索引，否则位置数组会被硬读成
  `[TIME, OPEN, MATCH, HIGH, LOW, ...]` —— 而实测第 6 列是 `TURNOVER` 不是 `VOLUME`，
  **差 170 倍且不报任何错**。`TIME` 带 `+02:00` 时区，按瞬时时刻换算 UTC 会让**日线日期
  整整退一天**，必须只取前导日期。MCP 外壳的 `isError: true` 失败信封也要处理。
- **这条通道本身不稳**：实测 3 次 `get_stock_kline` 里 1 次在 90s 上限真超时。
  线上「Wind 可用」不等于「Wind 稳」。

### 7.4 告警与巡检

- **告警出口** `cpt/adapters/feishu.py`：webhook 从 `CPT_FEISHU_WEBHOOK` 读，真实值只
  在 gitignore 挡着的 env 里。**best-effort** —— 失败只记一条 warning、绝不抛进被观测
  的路径；不重试。
- **水位表** `public.cpt_run_metric`，每轮一行，一表装两轨：
  - 轨道一（运行）：`last_bar_time` / `gap_count` / `stale` / `factor_coverage` / `health`
  - 轨道二（算法）：`config_hash` / `dataset_hash` / `rules_version` / **`backend`** /
    笔/分型/中枢计数（`backend` 是为「装个 czsc 就静默切生产后端」查不出来而加的）
- `health` **三态不是两态**：`ok` / `degraded`（配置导致的已知降级）/ `failing`
  （数据不完整）。判据只认**数据事实**，不认「某个依赖没装」。
- 落点在加密侧「**只有走到这里才算成功发布**」之后 —— 上面几条 return 是降级快照，
  记进去等于把「降级」记成「正常水位」。
- A 股侧也接落水位，`gap_basis: "trade_calendar"` 标注数字来源。**A 股日线跨周末与法定
  休市（一年约 25 天）天然不满足 7×24 连续交易判据**，用日历口径后 `gap_count 25 → 0`、
  `health failing → ok`。取不到日历时**不动**原值 —— 宁可保留一个可疑数字，也不把
  「查不到」写成「没有」。
- 巡检：首次只记基线不发 / 状态无变化不发 / 变了或 `failing` 就发。判据看**结构计数
  突变**（相对上一行 >15%）与**指纹变化**。
- 端点 `GET /api/dashboard/inspection`（只读旁路，DB 抖动降级成 `available:false`），
  面板 `dashboard/inspection_panel.js`，脚本 `scripts/run_inspection.py`。
- cron：`deploy/cron/` 下 `factor-recompute-daily.sh`（`flock` 防并跑 + `timeout 11h`，
  `--max-calls` 故意设得远高于可能的日限额，**让它去撞真正的墙**）、
  `run-inspection-daily.sh`（03:40 UTC）、`run-metric-prune-daily.sh`（04:10 UTC）。
- **退出码是契约**：`run-inspection-daily.sh` 配了 webhook 但发失败 ⇒ `rc=3`；
  「压根没配 webhook」那条也已改成 3（问题确实发现了只是送不出去，而 env 文件是
  gitignore 的，「没配」比「发失败」更严重）。
- `run_metric_store.prune` 已接进 cron；它**本身有 bug**（原 SQL 没有 kind 过滤，
  一调用就把 `inspection` 行一起删了），已加 `kinds` 参数。
- **告警不能因为 webhook 丢失而静默成功**（R56 修）：缺 env 只写 stderr 不改退出码，
  而巡检在「无问题且状态未变」时 `return 0` ⇒ webhook 一丢就永远不告警。

### 7.5 golden set

`scripts/golden_set.py` + 基线 `deploy/golden/ashare.json`。
集合 = 锚标的 ∪ 热门池 ∪ 服务端自选，去重保序。指纹只存**结构形状**
（`fractal_ids` / `bi_ids` / `zhongshu_ids` / `bar_count` / `dataset_hash`），
**不存价格**（价格天天在动，存了只会天天报差异）。`dataset_hash` 是有意放进去的：
它让「结构没变但输入变了」也能被发现。

用法：`--build <path>` 建基线，`--check <path>` 比对，有差异 exit 1。

⚠️ **`collect()` 的「只读检查」说法不成立**：`build_ashare_snapshot` 内部的
`record_structure_events` 结尾有 `db.commit()`，提前划走了事务边界，后面的
`rollback` 只回滚了最后一段。注释已改成如实描述。

**基线用的是生产因子**（未动时仍有效）；一旦切表它**必然报差异**，那是它的正确行为，
别顺手改基线。

### 7.6 不属于 CPT 的东西

`/var/www/cpt-dashboard/_pkg/` 是 `collector-cn` 采集机（服务 **league-predict**）的发布
通道，**恰好寄居在 CPT 的静态根里**。CPT 自己的 A 股行情走 `public.daily_bar` + 腾讯 +
Wind，从没依赖过它。

现行规矩（`deploy/README.md`）：只删无任何 `run*.sh` 引用者、删前逐个核 sha256、
先 `cp` 到 `/tmp` 留底再移走；**比「最新被引用包」还新的未引用包视为「可能正在发布中」，
一律保留**。`run5.sh` / `run6.sh` / `run7.sh` 的凭据从环境变量 `CPT_AUTH` 读
（**不写进文件**，避免它变成秘密载体），并已加 `bash -n` 校验。

---

## 8. 质量门禁

**逐层复盘记录**（六层，R29–R32 + R45）：web 层复盘（R29）、domain 层复盘（R30）、
adapters 层复盘（R31）、application 层复盘（R32）都在本文对应章节；storage 层复盘与
llm 层复盘（R45，全层勘察各修 3 个真 bug）的详细记录见 `docs/archive/reviews-r45.md`。
`web/` 与 `domain/` 在 R45 重扫时**未发现需修问题**。

`ci.yml` 里 13 个脚本步骤（`selftest_gates.py` 排在最前）。**自检必须排在最前** ——
顺序反了的话，一个恒返回 0 的门禁会把整条链伪装成绿的。

- `selftest_gates.py` 每个门禁配一个已知错例，先证明它**能红**
- `check_sql_layering.py` SQL 只许在 `adapters/` 与 `storage/`
- `check_storage_failure_semantics.py` 执行了 DB 语句而 `except` 不 raise 的函数
- `check_doc_drift.py` §2.1 计数 / 复盘状态 / 冻结参数 / `/api` 路径 / 存在性 五类
- `check_all_claims.py` 路径 / 符号 / 接口 / 表名 / 配置键 / 行号越界 / `<script>` 标签
- `check_doc_counts.py` 文档里散落的「N 个文件」「M 行」断言逐条对账
- `check_enqueue_skeleton_unique.py` 入队骨架唯一实现
- `check_job_poll_unique.py` 前端轮询唯一实现在 `cpt_job.js`
- `check_ci_workflow.py` `run: |` 块标量有没有吞掉 `- name:` step
- `check_line_refs.py` 行号引用指向的**符号**是不是文档说的那件事
- `check_gate_coverage.py` `ci.yml` / `FIXTURES` / 实际脚本三方对齐
- `check_cold_environment.py` 以 `PATH=/nonexistent` 重跑 `tests/`，抓依赖本机环境的断言
- `build_dashboard_bundle.py` 前端 bundle 与源一致

其余静态门禁：`lint-imports`（6 kept, 0 broken）、`vulture --min-confidence 60 cpt
whitelist.py`、`ruff check`、`ruff format --check`、`mypy cpt scripts`。

**三条方法论（都是花钱买来的）**：

1. **门禁本身必须先被验过。** 「自检 13/13 通过」不够 —— **替身也能 13/13 通过**。
   验证自检有效性时要把真门禁改成恒返回 0，确认自检转「漏报」。
2. **测行为，不测写法。** 源码 grep 只证明「没调用」，证明不了「真的返回了 JSON」。
3. **同一类病会换入口复发。** 本仓出现过四类**静默门禁失效**，共同点是**都不产生测试失败**：

   | 形态 | 表现 |
   |---|---|
   | 门禁恒 `return 0` | 永远绿（R45） |
   | 断言依赖本机环境 | 本机绿、CI 红（R49） |
   | YAML 块标量吞 step | 5 个门禁一次都没跑过（R52） |
   | **验错了属性** | 跑了、绿了，查的条件比声明的弱（R54） |

   通用判据：**判断门禁有没有效，要看它在不在 `ci.yml`、那一行有没有真被执行、
   退出码有没有被传播。** 红着的门禁如果长期是假阳性，人就会开始忽略它 —— 那比没有
   门禁更糟。

### 8.1 静态门禁的已知盲区

- **vulture 只扫 `cpt/`**，`scripts/` 是盲区；也看不见 `__all__` 里的死代码（认为
  「已导出即已用」）。这类项按「门禁盲区」在 `whitelist.py` 写明理由。
- `.pre-commit-config.yaml` 注释曾声称「与 CI 跑同一套」，实际 13 个门禁一个都不在
  （R56 记录）。
- **`check_all_claims` 的 L 类只验「行号没超出文件总行数」**；「那行还是不是文档说的
  那件事」由 `check_line_refs.py` 负责（R54 拆开）。
- **表名 / 配置键断言扫的是「代码 + 模板」文本**，所以一个 key 从真实代码删掉却仍留在
  `.env.example` 模板中，本门禁**抓不到**。模板即部署契约声明 —— 若代码已不再读某变量，
  正确做法是把模板那一行一起删掉。
- **本地全绿 ≠ 门禁在守**：要看 CI 的 `conclusion`，**还要看哪几步被 `skipped`**。
- **本地环境本身会污染结论**：沙箱边界造成的失败极易被读成「代码坏了」。廉价判据是
  **换权限档位复跑一次**。
- **Windows + PowerShell**：内联 Python 会被吞；含中文的脚本要存 UTF-8 BOM 或用
  `write` 工具 + `PYTHONUTF8=1` 跑。**跑门禁前先设 `PYTHONIOENCODING=utf-8` 与
  `PYTHONUTF8=1`**，否则中文输出会按 GBK 解码失败。

### 8.2 测试纪律

- **守门用例的入口必须是生产构造函数**（自证式用例证明不了生产会不会走它），
  且**已实测未接线时为红**才算数。
- **红绿对照要做双向**，且**变异没生效就等于没验** —— 每次先确认注入真的落到了文件里。
- **判行为，不判调用记录**：假连接要如实模拟 psycopg 的 aborted 事务语义，然后故意让
  中间某条 SQL 失败、**断言后续查询仍能成功**。
- **判据不能只看 mock**；固化错误契约的测试比没有测试更糟。**别编 key、别编文件名**
  —— 用同一个对象算，改完拿 `git ls-files` 核实。
- **覆盖率数字不是目标，「生产路径上有没有没跑过的分支」才是**；只能靠「被生产代码
  引用」这个判据把真坑和无关边角分开（`web/__main__._run` 只有 15.4%，而它是服务入口）。
- **测试里走不到的分支就是没测过**。「契约允许 dict」不等于「实现支持 dict」。
- **已修复不可靠 ≠ 已修好**：断言层与被断言层同时自证。
- **自制的验证工具第一版基本都错**；**验证工具的输出必须用独立手段交叉验证再采信**
  （能用项目已有的权威工具就用）。**验证手段本身可能制造它要检出的现象** ——
  判据是换一个不依赖该现象的观测手段，而不是「看起来在不在」。
- **优雅降级会掩盖功能缺失**，**skip 也会制造「绿灯来自根本没执行」的假象**。
- 查「有没有人用过」之前**先确认请求会记在哪个日志里** —— 一个存在但记错位置的日志
  比没有日志更危险，它给你一个**假的 0**，而假的 0 会一路支撑到「可以删」这种不可逆决定。
- 取 pytest 权威计数用 `--junit-xml`（`-q -q` 会吞掉最终统计行）；
  `git worktree add <tmp> HEAD` 是定位「红点是新引入还是既有的」的关键手段。
- 跨机器比文件前统一换行；改完 Python 清 `__pycache__`。
- **`「照着 ci.yml 的原句跑」`**：手敲命令这件事本身要被取消，让命令从 `ci.yml` 里
  读出来 —— 这条已重复踩坑两次。

更多具体陷阱与反例见 `docs/known-traps.md`（本仓的权威清单）。

---

## 9. 仍开放的事项

1. `t_plus_one_purchase_allowed`（`cpt/domain/a_share_rules.py`）是**全仓唯一零生产
   引用的公开符号**，R22 起至今未接。
2. **17 只新股** K 线不足 30 根 ⇒ 因子算不出来。根因在 ingest 侧（另一个项目）要往前
   拉历史，owner 已明确不在本仓范围。
3. `daily_bar` 缺口成因未定位（集中在每年 4 月底，2026-04-29 有 44 只票同时缺）。
4. `asel.security_master` 名称里的 `N`/`ST`/`*ST` 前缀是**源里的当前名称**，会随源更新
   变化，CPT 不做额外标注或推断；不做名称搜索。
5. `cpt_signal_event` 里旧口径信号的**纪元标记**待处理。
6. `cpt_job.js` 里**奇数个 ``` 围栏**会撑破 user 消息的 json 围栏（低危；
   `structured.py` 本就防御式解析）。
7. 导出的结构块**要不要真按时间过滤**（R32 起挂账）—— 决定是「不做」：现在保留完整
   窗口 + 在 `slice` 块里如实声明（`sliced_blocks` / `source_bar_count` /
   `unsliced_blocks_note`）。owner 已拍板「减功能不要做」。
8. `_pkg` 那条 0 下载的通道：**不退役**（在用）。真问题是 `linux7` 包本身缺
   `scripts/collector_linux.sh`，`run7.sh` 是止血不是修包 —— 应由 collector-cn 的主人
   重新打包。
9. `web/__main__.py` + `app.py` 合计 2,294 行承担 CLI 解析 / provider 选择 / HTTP
   handler 装配 / 降级策略四件事，拆分信号明显，但会动到 HTTP 装配路径，
   **风险大于收益**，记此不拆。
10. `?level=abc` 的行为随模式而变（realtime 回 400，demo/fixture 静默回 200）——
    要先决定「demo 模式收到不支持的参数该怎么办」，已写成显式测试钉住现状。
11. **R58 「我的追踪」段 2 + 段 3 已落地（R58-2）**：UI 独立页面 `/cpt/track/` + 推送
    hook 在 `record_signal_event + commit` 之后。**「再讲一次人话」按钮当前是占位**——
    handler 只刷新缓存视图，不调 LLM worker；human 缓存由运维 cron 触发；这是段 2
    已知不完整项（用户没明确要求前端调 LLM）。如要补：新增 `POST /api/dashboard/track/
    {code}/speak` 端点 → application 层调 `cpt.llm` worker → 写
    `cpt_track_snapshot.human_text`。
