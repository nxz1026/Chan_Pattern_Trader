# 共享 A 股表契约（CPT × emotion-core）

> **这是跨仓库契约。** 一份文档管两个项目，因此它不受任一方的门禁保护 ——
> 所以真正的强制机制是**代码侧的检查**，不是这份文档：
> `scripts/check_shared_tables.py`（新鲜度）+ `tests/test_shared_tables_contract.py`
> （把本文件与 CPT 的真实 SQL 双向钉住）。
>
> ⚠️ **改这张表时必须同步三处**：本文件 → `check_shared_tables.SHARED_TABLES`
> → CPT 里新增的 `SELECT`。漏任何一处，那张表断更时**不会报警**，而且没人会发现
> （CPT 仍然正常渲染，只是数据悄悄停在某一天）。

## 1. 拓扑

```
emotion-core  ──写──▶  public.<7 张共享表>  ──只读──▶  CPT
   (唯一写方)              (同一个库 emotion_core)
```

- **零代码依赖**：CPT 不 import emotion-core，反之亦然。emotion-core 甚至不是可安装包
  （无 `pyproject.toml`，靠 systemd 的 `PYTHONPATH=.../src` 运行）。
- **单向**：emotion-core 拥有并写入，CPT 只读。CPT 只写自己的 8 张 `cpt_*` 表。

这个形态是**正确的**，不要为了「合仓」把它改成同仓编译期耦合 —— 那不会消除
耦合（耦合本来就在数据层），只会让 CPT 每读一张表就多一条跨模块依赖。

## 2. 表清单

| 表 | 写入方 | 写入点 | 数据来源 | 期望新鲜度 |
|---|---|---|---|---|
| `daily_bar` | emotion-core | `data/ingest.py:404/418` | 东财 push2delay 全市场快照；502 回退新浪；pytdx 回填 | 收盘后当日 |
| `derived_bar` | emotion-core | `services/derive_service.py:87` | **无外部源**，纯 SQL 派生自 `daily_bar`（涨停/炸板/一字/连板） | 与 `daily_bar` 同 |
| `hot_rank` | emotion-core | `data/ingest.py:602`（当日）/ `:638`（回补） | 东财人气榜 top100（绕开 akshare 字段不兼容） | 与 `daily_bar` 同 |
| `ladder_day` | emotion-core | `data/loader.py:164`（DELETE+INSERT） | **无外部源**，派生自 `derived_bar`/`limit_pool_em` | 与 `daily_bar` 同 |
| `limit_pool_em` | emotion-core | `data/ingest.py:509` | akshare 涨停/炸板/跌停池 | 与 `daily_bar` 同 |
| `strategy_signal` | emotion-core | `services/strategy/runner.py:241` | **LLM**（15 个 YAML 策略） | 与 `daily_bar` 同 |
| `trade_calendar` | emotion-core | `data/trade_calendar.py:69` | akshare 新浪交易日历，**刻意独立于库内行情数据** | 覆盖到今天即算新鲜 |

**「期望新鲜度」的判定**：收盘时刻（北京时间 15:00）已过的**最近一个交易日**。
- 今天休市 / 周末 ⇒ 期望上一个交易日（数据停在那儿是**正确的**）
- 今天交易日但未收盘 ⇒ 期望上一个交易日
- 今天交易日且已收盘 ⇒ 期望今天

这个规则不是洁癖：判错会在**每个假期**误报一次，一周之内这检查就没人看了。

## 3. 谁报警

**CPT（消费方）报警。** 理由：它是唯一必须知道「数据不新鲜」的一方，而且是
7×24 在跑的。emotion-core 的 pipeline 断了它自己未必知道（它只管写）。

- 检查脚本：`scripts/check_shared_tables.py`
  - `0` 全部新鲜 / `1` 有表缺失或过期 / `2` **连不上库或查询出错**
  - `2` 与 `0` 必须分开：返回 0 会让日报写「一切正常」，而真相是「根本没查成」
- 接入点：`deploy/cron/cron-daily-report.sh`（每日 07:00 UTC = 北京 15:00），
  有问题发飞书
- CPT 自己的 A 股快照 `data_quality.severity`（stale/gap/ok）是**给人看**的，
  不是给机器看的 —— 它能算出来，但没人盯着面板

## 4. 已知的上游风险（2026-10-07 实测）

**Oracle 上没有任何东西会写这 7 张表。**

- `emotion-core-daily` / `emotion-core-strategy` 两个 timer 都是 `inactive`/`disabled`
- 全机 timer + active service + user/root crontab 枚举下来没有 A 股 pipeline
- 当天（国庆休市）看不出异常；**2026-10-08 开盘后就会静默断更**

⇒ 本契约存在的意义就是这个：**让断更变成一条飞书告警，而不是几个月后
某天有人发现看板上的 A 股停更了。**

## 5. 明确**不在**本契约范围内的事

- **两套信号的冲突裁决**。ADR 0001 D4 写着「CPT 盘中看到机会 vs emotion-core 判
  `buy_window=NONE` 禁买」这个冲突「明确不设计」。那是个**业务决策**，不是数据契约，
  别混进来一起「顺手解决」。
- **CPT 自己的 8 张 `cpt_*` 表**。那是 CPT 的内部实现，与本契约无关。
- **「把两个项目合成一个仓」**。经核实不成立：emotion-core 的「缠论」是喂给 LLM 的
  提示词（`llm/strategies/chan_theory.yaml`，`instructions: |` 自然语言块），
  `algorithms/` 里没有任何分型/笔/中枢的确定性计算代码 —— **同名不同物**，
  重复实现这个合并的主要理由不存在。