# 去重判定归档（duplication-triage，2026-09-25）

> **这份文档的用途**：把 `docs/audit/audit-20260925.json` 三个去重字段
> （`dup_names` / `dup_bodies` / `dup_blocks`）的**逐组判定**归档下来，
> 免得下一个接手的人再花一遍时间重新判。
>
> **它不是待办清单。** 结论是：**当前 0 组真重复**。

---

## 1. 复核方法与一个必须知道的警告

### ⚠️ 不要照 `audit.json` 的文件清单核对现状

`audit.json` 是 **2026-09-25 修复前**的快照，里面仍列着**已整层删除**的文件：

| 已删除 | 出现在 audit.json 的字段 |
|---|---|
| `cpt/engine/rebuild.py`、`cpt/engine/realtime.py` | `dup_names` #13 `_resolve_time`、#15 `_bar_to_dict` |
| `cpt/storage/repository.py` | `dup_names` #5 `close` |
| `tests/test_perf_and_hashes.py`、`tests/test_repository_audit_fixes.py` | `dup_blocks` #13/#15/#16 |
| `tests/test_validation_audit_fixes.py` 的对家 | `dup_blocks` #18 |

已核实 `tests/` 与 `cpt/` 里现在**没有任何** `SQLiteRepository` / `cpt.storage` /
`cpt.engine` 的实际引用 —— 全仓只剩 **1 处**，是 `tests/test_replay_integration.py:8`
的 docstring 历史说明（`grep -rn` 实测）。

### 本次复核方法（判结构，不判字符串）

对 `dup_names` 的 27 组，逐组用 `ast.parse` 取出**当前仍存在**文件里的同名定义，
**剥掉 docstring** 后对函数体做 `ast.dump` 结构哈希，再按哈希分组：

* 同一哈希出现 ≥2 次 → **真重复**（函数体逐字同构）
* 多个哈希 → **同名不同义**（各自实现）
* 只剩 1 处或 0 处 → **已消解**

用结构哈希而不是文本 diff，是因为注释/docstring/参数名差异会污染文本比较，
而这几类差异**恰恰不是**重复（见 §6 坑 2、坑 3）。

---

## 2. 结论摘要

| 字段 | audit.json 规模 | 复核后判定 |
|---|---|---|
| `dup_names` | **27** 组 | **4 组已消解**、**23 组同名不同义**、**0 组真重复** |
| `dup_bodies` | **1** 组 | 纯测试样板，**无需动** |
| `dup_blocks` | **21** 组 | 修复前全为结构性误报；其中 5 组随文件删除消失，**1 组（HTTP server 样板）已在 F3-④ 收口** |

---

## 3. `dup_names` 27 组逐组判定

### 3.1 已消解（4 组）

| 符号 | 消解方式 | 证据 |
|---|---|---|
| `upsert_factor_rows` | **本轮 D**（适配器扩成超集、删脚本副本） | 当前仅 `cpt/adapters/a_share_factor.py:265` 一处定义 |
| `_read_dbconfig` | **P0**（解析/校验收口到 `cpt/adapters/_dbconfig.py`） | 当前 **0 处**定义 |
| `_resolve_time` | **P0**（`cpt/engine/` 整层删除） | 当前仅 `cpt/adapters/reference_chanlun.py:143` 一处 |
| `_bar_to_dict` | **本轮 F3-②**（抽到 `cpt/application/_bar_dict.py`） | 当前 **0 处**定义，两处改为 `import bar_to_dict` |

> 注：`upsert_factor_rows` 修复前**并不是**"函数体逐字相同"，而是**分叉**
> （脚本版 9 列 + `dry_run`，适配器版 8 列且不支持 `dry_run`，`source` 还各写一个值）。
> 它属于"同名同义但实现分叉"，比真重复更危险 —— 因为看不出是重复。D 已收口。

### 3.2 同名不同义（23 组）

`+` 连接表示这几处的**函数体结构哈希相同**（即同为"同一种实现"的多份拷贝），
但都落在**不同的类/协议**里，是同一协议的多实现，不是复制粘贴。

| # | 符号 | 当前定义处 | 说明 |
|---|---|---|---|
| 3 | `connection_kwargs` | `cpt/adapters/a_share_local.py:131`(4 句) / `scripts/factor_backfill.py:84`(1 句) | **薄包装**：都转调 `_shared_connection_kwargs(exc_type=...)`，只有异常类型不同（`AShareLocalError` vs `SystemExit`），前者另加 RDS CA 证书 |
| 4 | `fetch_validated_klines` | `a_share_local.py:225` / `binance_futures.py:389` | 两个数据源各自的取数+校验 |
| 5 | `close` | `a_share_local.py:313` / `domain/contain.py:65` | 前者关 psycopg 连接（`self._conn.close()`）；后者是 `_DetailedBar(BarLike, Protocol)` 的**收盘价属性桩**（`-> float: ...`），**语义完全无关** |
| 6 | `_urlopen_bytes` | `a_share_public.py:128` / `binance_futures.py:197` | 各自带自己的异常类型与超时口径 |
| 7 | `_as_float` | `a_share_public.py:134` / `binance_futures.py:244` / `ccxt_source.py:81` | `a_share_public` 与 `ccxt_source` 两份**同构**（都 `float(str(value))`，只有异常类不同）；`binance_futures` 那份**多一层 `bool` 与类型守卫**，是真不同 |
| 8 | `fetch_daily_bars` | `a_share_public.py:176` / `wind_source.py:351` | 两个源的日线接口 |
| 9 | `fetch_klines` | `binance_futures.py:359` / `ccxt_source.py:170` | 直连 vs ccxt 两条链路 |
| 10 | `probe` | `ccxt_source.py:212` / `wind_source.py:428` | 各自探活（Wind 还涉及额度纪律） |
| 11 | `compute_structures` | `czsc_chanlun.py:159` / `native_chanlun.py:30` / `reference_chanlun.py:132` + `:225` | 四个后端适配器，**接口相同实现不同** |
| 12 | `resolve` | `czsc_chanlun.py:187` / `native_chanlun.py:60` | 同上 |
| 14 | `_stamp` | `application/canvas_wbt.py:374` / `domain/signal.py:142` | 前者是把毫秒戳格式化成 `%Y-%m-%d`（缺失回 `—`）；后者是"已有值不覆盖、缺失才写入"的时间戳保留语义。**同名不同义** |
| 16 | `_infer_interval_ms` | `application/dashboard.py:70` / `application/replay.py:327` | 看板与回放各自推断周期，**参数与回落策略不同** |
| 17 | `main` | `application/replay.py:448` / `web/__main__.py:884` / `web/a_share.py:95` / `scripts/factor_backfill.py:220` | 四个入口，**同名不同义** |
| 18 | `_resolve_level` | `domain/bi.py:67` / `domain/trend_type.py:67` / `domain/zhongshu.py:91` | 三种结构各自解析 level |
| 19 | `direction` | `domain/contain.py:121` **+** `domain/models.py:101`（同实现）/ `domain/types.py:84` | 见下方 §3.3 |
| 20 | `_source_ids` | `domain/fractal.py:100` / `domain/trend_type.py:150` | 两种结构各自组装 source_ids |
| 21 | `_overlaps` | `domain/recursion.py:143` / `domain/trend_type.py:102` | 各自的重叠判定 |
| 22 | `select_symbol` | `web/__main__.py:220` / `:496` / `:875` / `web/app.py:56` | 见下方 §3.3 |
| 23 | `snapshot_payload` | `web/__main__.py:242` **+** `:535` **+** `:879`（同实现）/ `web/a_share.py:69` / `web/a_share_routes.py:91` / `web/app.py:23` | 见下方 §3.3 |
| 24 | `snapshot_for_range` | `web/__main__.py:246` / `:539` / `web/app.py:44` | 三个 provider 各自实现 |
| 25 | `snapshot_for_level` | `web/__main__.py:279` / `:594` / `web/app.py:30` | 同上 |
| 26 | `inspect` | `web/__main__.py:313` / `:586` / `web/app.py:37` | 同上 |
| 27 | `force_refresh` | `web/__main__.py:517` / `web/app.py:58` | 同上 |

### 3.3 两个"同实现多拷贝"边界情况（刻意保留）

这两组是**同构多份**，但**判定为不合并**：

**`direction`** —— `domain/contain.py:121` 与 `domain/models.py:101` 的函数体
**逐字同构**（都是 `close` vs `open` 比较，3 句），`domain/types.py:84` 是
`BarLike` 协议里的桩（`...`）。

* **不合并的理由**：它们是两个**不同 dataclass** 各自实现的同一个协议属性。
  合并等于让 `models` 依赖 `contain`（或反之），而这两个模块在 `domain` 层内是
  平级的；为一个 3 行属性引入跨模块依赖不划算。
* 真正的收口点是 `domain/types.py` 的 `BarLike` 协议 —— 契约已经在那里了。

**`snapshot_payload` / `select_symbol` / `snapshot_for_*` / `inspect` / `force_refresh`**
—— `web/__main__.py` 里三个 provider 类（`_FixtureProvider`、`_RealtimeProvider`、
`_DemoProvider`）各自实现同一批 HTTP 协议方法。

* **不合并的理由**：这些是**协议实现**，不是工具函数。它们看着像，是因为协议
  规定了同一个签名；一旦某个 provider 要多做一件事（`_RealtimeProvider` 就多做了
  降级留痕），合并就会长出 `if isinstance(...)` 分支 —— 那才是真退化。
* 契约在 `cpt/web/app.py` 的 `SnapshotSource` / `SelectableSource` / `InspectProvider`
  等 `Protocol` 上，已由 `lint-imports` 与 mypy 双重钉住。

> **判定口径**：**"看起来一样"不等于"应该合并"**。合并的收益是少几行；
> 代价是把两个**独立演进**的东西焊死。这里 23 组全部属于后者。

---

## 4. `dup_bodies`（1 组）：测试样板，不动

```
tests/test_a_share_local.py:73   def date(y, m, d) -> 4 行
tests/test_a_share_rules.py:20   def date(y, m, d) -> 4 行
```

两处**逐字相同**，都是测试里为了构造 `datetime.date` 写的小helper
（`from datetime import date as _d; return _d(y, m, d)`）。

**判定：不合并。** 理由：纯测试样板、零生产影响；为一个 4 行 helper 让两个测试
文件互相 import，耦合成本高于收益。（若将来第三个文件也要用，再提到
`tests/conftest.py` 不迟 —— 参考 F3-④ 的触发条件。）

---

## 5. `dup_blocks`（21 组）：全部为结构性误报

`audit.json` 的块级重复检测把"**语法形状相同**"当成了重复。逐组看过
（按 `sample` 首行分类），**没有一组是需要合并的真重复**。

### 5.1 按类别

| 类别 | audit 条数 | 当前存活 | 判定 |
|---|---|---|---|
| `CanonicalBar(...)` 构造 | 2 | 2 | 两个数据源各自构造领域对象，**正确** |
| import 块（`export.py` ↔ `replay.py`） | 2 | 2 | 从同一批 domain 符号 import，**正确** |
| `ReferenceChanlunConfig(...)` 构造 | 2 | 2 | `multi_level.py` ↔ `replay.py`，**参数不同** |
| dataclass 字段声明（`contain.py` ↔ `models.py`） | 2 | 2 | 都是 `open_time: int` 等，**两个不同 dataclass** |
| 7 键契约断言 | 2 | 2 | 见 §6，**刻意**在两层各钉一次 |
| 测试夹具 `bars = tuple(` | 3 | 3 | 三个 dashboard/containment 测试的样板 |
| 测试样板 `export_dataset(` | 2 | **1（已不成对）** | 对家 `tests/test_perf_and_hashes.py` 已随 `cpt/storage/` 删除；幸存的那处（`test_validation_audit_fixes.py:131`）已无重复对象 |
| HTTP server 样板（`serve_forever`） | 3 | **0** | **已在 F3-④ 收口**（见下） |
| `SQLiteRepository(path=":memory:")` | 3 | **0** | 三个测试文件随 `cpt/storage/` 整层删除 |
| **合计** | **21** | **16 组仍成对 → 13** | 另 5 组随文件删除消失 |

### 5.2 唯一值得动手的一组：HTTP server 样板 → **已由 F3-④ 消解**

`audit.json` 只列了 3 条（因为它只报"跨文件成对"的块），但实测这段样板在
`tests/` 里有 **8 处、分布在 7 个文件**：

```
tests/test_review_m7_fixes.py:40, :52      tests/test_source_registry.py:173
tests/test_dashboard_http_query.py:18      tests/test_web_canvas_wbt.py:100
tests/test_dashboard_health.py:49          tests/test_web_a_share_routes.py:124
tests/test_dashboard_http.py:22
```

> **交接文档写的是"7 处"，那是按文件数算的；按出现次数是 8 处。**

每份都要自己写 `shutdown` / `server_close` / `thread.join(timeout=2)` 三连，
漏一处就泄漏线程与端口。F3-④ 已统一收进 **`tests/conftest.py` 的 `served(provider)`**：

* 进入时 yield `http://127.0.0.1:<port>`，退出时保证三连回收；
* 仍然绑 `127.0.0.1`、仍然 `port=0`（内核分配，避免固定端口冲突）、
  `join` 超时仍是 **2s** —— **没有改掉任何测试对端口/超时的既有假设**；
* `cpt` 在函数体内**延迟导入**，维持 conftest"不在 collection 阶段拖入被测包"的既有约束；
* 收口后 `grep -rn "target=.*serve_forever" tests/` 只剩 `conftest.py` 那一处定义。

---

## 6. F5 复核：两个 canvas 测试**不重复**（保留两份）

| 文件 | 行数 | 测什么 |
|---|---|---|
| `tests/test_canvas_wbt.py` | 215 | `build_canvas_d_payload()` 等**单元**函数 |
| `tests/test_web_canvas_wbt.py` | 188（F3-④ 收口后；原 203） | 起真 server 测 **HTTP 路由** |

唯一重叠是那个 7 键 `set(payload)` 断言，但它们断言的是**不同 payload**
（一个 `unavailable`、一个 6 根 candles）—— 这是同一契约在**两个层**各自钉住，
不是复制粘贴。**结论：保留两份。**

---

## 7. 本轮（2026-09-25）实际消解的重复

| 项 | 内容 | 落点 |
|---|---|---|
| **D** | `upsert_factor_rows` 分叉收口（适配器成超集、删脚本副本） | `cpt/adapters/a_share_factor.py` |
| **P0** | `_read_dbconfig` / `_resolve_time` 收口 + 删除 `cpt/engine/`、`cpt/storage/` | `cpt/adapters/_dbconfig.py` |
| **F3-②** | `_bar_to_dict` 合并为唯一实现 + 键集合冻结守卫 | `cpt/application/_bar_dict.py`、`tests/test_dataset_hashes.py` |
| **F3-④** | HTTP server 样板 8 处 → 1 处 | `tests/conftest.py::served()` |

### F3-② 的合并为什么是安全的

`_bar_to_dict` 是**唯一**一组真重复（函数体逐字相同）。但它有个特殊风险：
导出那份是**已冻结的 schema v1**，看板那份是 UI 载荷 —— 今天同形是巧合，
合并后"看板顺手加个字段"就会**静默改掉导出格式**。

故合并时同时做了三件事：

1. 新模块 `cpt/application/_bar_dict.py` 顶部写**显式 warning**：改键集合 = 改导出格式，
   必须升 `EXPORT_SCHEMA_VERSION` + 同步 `docs/export-schema-v1.md`；
2. 新增 `tests/test_dataset_hashes.py::test_export_bar_keys_are_frozen_schema_v1`，
   把 bar 键集合钉成 13 个键的**冻结字面量**；
3. **做了反向验证**：往 `bar_to_dict` 里插一个 `__drift_probe__` 字段 →
   该测试立刻 **RED**；还原后 **GREEN**。证明这条守卫不是摆设。

---

## 8. 相关文档

- `docs/audit/audit-20260925.json` —— 原始检测产物（**修复前快照**，勿当现状）
- `docs/audit/cpt-code-audit-20260925.md` —— 原始审核报告
- `docs/handoff-20260925-leftover-fixes.md` —— 本轮交接（F3-②③④ 的来源）
- `docs/export-schema-v1.md` —— 被 F3-② 守卫钉住的 schema 文档
- `tests/conftest.py` —— F3-④ 的 `served()`
