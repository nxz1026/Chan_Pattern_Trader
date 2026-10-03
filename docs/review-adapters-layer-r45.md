# 复盘：`cpt/adapters/` 层（R45，2026-10-03）

> 19 个文件 / 5,521 行，**全部审完**。这层是 `architecture.md` §2.1 认定的
> 头号风险层（「唯一出去问别人要数据的一层，外部契约漂移不会让测试变红」）。
>
> 结论：**3 个真 bug，全部已修并验证**；外部契约真机验证 5/5 成立。

---

## 0. 先纠正一件事：这层曾经**没有**被复盘

`architecture.md` §2.1 标的是 ⚠️（未做），比 storage/llm 的假 ✅ 诚实。
R44~R45 在这层改过 `eastmoney_actions.py`（§4 那个 bug）与
`a_share_local.py`（docstring 去快照化），但按 §2.1 自己的定义
「不是被单点改动顺手碰过」，那**不算复盘**。

---

## 1. 三个真 bug

### ① `_dbconfig.py` —— `$DBPORT` 破坏注入式异常契约

本模块的设计是「异常类型由调用方注入」（`a_share_local` → `AShareLocalError`、
`a_share_pool` → `WatchlistError`、`factor_backfill` → `SystemExit`），
三个调用方各自 `except` 自己那个类型。

但原实现直接 `int(cfg.get("$DBPORT", "5432"))`：端口写成 `"abc"` 抛 `ValueError`，
**三个调用方谁都接不住**。契约在这一处是破的。

修法：抽 `_port()`，格式错与**越界**（0/-1/65536）都抛注入的 `exc_type`。
顺带堵了越界 —— 静默去连一个不存在的端口比报错难查得多。

### ② `scripts/run_inspection.py` —— 告警没送出去却 rc=0

`notify_problem` 返回 False 时只 `print("告警发送失败")` 然后 `return 0`。
与 R45 修的 `factor_recompute` rc=0 同病：「该做的没做成」与「做完了」共用
rc=0，cron / 看门狗 / 外部监控都看不出来。
**而告警通道坏掉的时候，恰恰最需要机器来发现它。**

改成 `return 3`（与 `factor_recompute` 对齐）。webhook **未配置**那条分支不动 ——
那是「本来就该降级」。

### ③ `a_share_pool.py` —— 自选文件损坏时 `add()` 静默清空全部条目

三个方法对**同一个条件**（自选文件 JSON 损坏）有三种反应：

| 方法 | 行为 |
|---|---|
| `list()` | 抛 `WatchlistError` ✅ 诚实 |
| **`add()`** | `data=[]` 继续写 → **整个自选股被静默清空** ❌ |
| `remove()` | `return False` ❌ 谎称「没删掉」 |

真机复现：损坏文件上 `add("000002")` 之后，文件里只剩 000002，
600519/000001 **无声消失**，无异常、无日志。

**「猜成空列表」在这里是最坏的降级** —— 自选股是用户数据，猜错就是丢数据。
修法：三方法共用 `_read()`，损坏一律抛，消息明说「**未做任何修改**」。

---

## 2. 外部契约真机验证（这层独有、测试永远给不了）

`scripts/verify_public_contracts.py`（新增，**刻意不进 CI** —— 它依赖公网，
不能让腾讯/新浪的抖动把 CI 弄红；定位是手工巡检工具）。

| 契约 | 结果 |
|---|---|
| `fqkline` 字段顺序 = `[日期,开,收,高,低,量]`（**不是 OHLC**） | ✅ |
| 复权键名 `hfqday`/`qfqday`/`day` | ✅ |
| 逐标的「无 hfq」抛 `AShareAdjustUnsupportedError` | ✅ |
| 新浪快照必须带 Referer | ✅ |
| **交叉校验**：腾讯 hfq 价 vs 本地因子表算出的后复权价 | ✅ **差 0.000%** |

最后一条最有价值：腾讯 hfq（外部源）与东财重算的因子表（本地）是**两条完全独立
的数据路径**，算出的当日后复权价一致 ⇒ 复权口径两边都没漂。

---

## 3. 确认干净、无需改动的

| 文件 | 结论 |
|---|---|
| `a_share_public.py` | **范本级**。字段顺序既写进 docstring **又做 OHLC 自洽校验**（真出 bug 会抛错，不静默产坏数据）；「某标的无 hfq」用**独立异常类型**；`normalize_code` 拒绝未知后缀而非静默猜交易所 |
| `a_share_factor.py` | **复用** `a_share_public.TencentKlineClient`（字段顺序单点存放，无分歧风险）；退化防护实测有效（注入脏数据被正确拦下并打出可读告警） |
| `binance_futures.py` | 错误全 raise；URL 不含 key；`_http_error_detail` 三层降级 |
| `wind_source.py` | subprocess `list(argv)` **无 `shell=True`**，参数以 JSON 单参传入 —— 命令注入面关闭 |
| `source_registry.py` | 未知 id 抛 KeyError 不折叠；缓存 key 含 quota、TTL 有界 |
| `feishu.py` | `notify` 刻意返回 bool，调用方会检查 |
| `validators.py` | 两个入口语义清晰、不重排、缺口抛错不填充 |
| `a_share_local.py` | **已经把 R45 的教训学进去了** —— 日历函数 except 里主动 rollback，注释点名 R23；`skipped_no_factor` 有真实消费方（快照层用它区分 `no_factor` vs `no_data`） |

**全层自动扫描零命中**：
- 门禁②（吞 DB 异常）：0
- 「收集了诊断信息却没人看」：0
- 「except 返回 falsy 且无日志」：1（`conn.close()`，finally 里，无害）

---

## 4. 顺带更正一个未解项的**量级**

`handoff-20261003` §5 第 5 项记「`derived_bar` 每月 4 月底约 **1~2%** 的票缺
1~10 天」。实测（见 `known-traps.md` #23）：

- 真实是 **623 只（11.9%）**，平均缺 7.4 天，**最多缺 62 天**
- 缺口在 **`daily_bar`**（行情主数据）里，不在 `derived_bar` ——
  `derived_bar` 缺同一天而 `daily_bar` 有的票：**0 只**
- 缺口日全市场覆盖率 95.6%~99.6%，**666 天没有一天低于 90%** ⇒
  不是系统性故障，是**个体级**（长期停牌 C3 或该票丢单，
  仅凭库内数据分不出这两者）

**为什么一直没人发现**：`validate_ashare_bars` **刻意跳过连续性检查**
（R15/R16，因为 A 股的周末/节假日/停牌缺口合法）。这个设计对 C3/C5 是对的，
代价是**对 ingest 丢数据也一并放行**。要抓它需要**独立于 K 线本身**的判据
（本仓现成有：`public.trade_calendar`）。

---

## 5. 三后端分叉风险：休眠且有防护

`backend_factory.py` 记了一个真陷阱：装了 czsc 的话 `auto` 会**静默切后端**，
「看板上每一根笔都会变」。

实测生产机：`czsc` / `chanlun` / `chanlun_pro` **均未安装**、
`CPT_CHANLUN_BACKEND` 未设 ⇒ 走代码默认 `native`。陷阱休眠。

**本机无法做三后端对比**（czsc 未装；`reference_chanlun` 只有 Protocol 与
`InMemoryChanlunBackend`，是接口定义/测试替身，不是可跑的实现）。
`native` 单独验证过：600519 的 120 根日线 → **37 分型 / 36 笔 / 6 中枢**，
数量正常。`czsc` 未装时正确抛 `CzscNotInstalledError`（设计行为）。

## 6. 一个无碍的小观察

`source_registry.probe_source` 的缓存时间戳取的是**探测前**的
`time.monotonic()`，而冷启动探测要 7.4s（docstring 记的）⇒ 缓存条目
写入时已经「老了」7 秒，有效 TTL 悄悄从 60s 变成 53s。无害，仅记录。

## 7. 遗留（需 owner 决定，已记入 known-traps #23）

1. **623 只票的 K 线缺口** —— 成因在 ingest 侧（另一个项目）。要不要在本仓加
   一个基于 `trade_calendar` 的**只报告、不拦截**的巡检项？Owner 决定。
2. **78 只「分过红但算不出」**（除权日早于 bar 起点 2024-01-02）——
   要治得往前拉 K 线起点，同属 ingest 侧工程。
