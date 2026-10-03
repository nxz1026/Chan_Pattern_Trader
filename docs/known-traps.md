# 已知陷阱与非缺陷清单（known traps）

> 建立：2026-09-25 仓库坑排查。目的：把**看起来像 bug 但其实是设计/环境使然**的东西
> 集中记下来，避免下一个会话把它们当缺陷去"修"，反而改坏。
>
> 每条都给了**判定命令**——先跑命令，再下结论。

---

## 1. CI 里 28 个测试被 skip，不是缺陷

**现象**：本地 `.venv` 跑 `462 passed`，CI 里却是 `434 passed, 28 skipped`。

**原因**：`czsc` / `wbt` / `ccxt` 三个**可选依赖**只在 extras 里
（`pyproject.toml` 的 `[project.optional-dependencies]` 的 `chan` / `report` / `crypto`），
CI 只装 `requirements-dev.txt`（不含任何 extra），于是相关测试**优雅跳过**。
而 `czsc`/`wbt` 的参考实现在 `references/` 下，**该目录不入 git**
（见 `.gitignore`），所以 CI 结构上就拿不到它们。

**判定**：

```bash
grep -c "^SKIPPED" /dev/null   # 直接看 skip 原因：
.venv/bin/python -m pytest tests -rs 2>&1 | grep SKIPPED | sed 's/.*: //' | sort -u
# -> could not import 'czsc' / 'wbt' / 'ccxt'
```

**要全量覆盖**：`pip install -e ".[dev,chan,report,crypto,db]"`。
2026-09-25 用 python-build-standalone 的真 3.12.14 实测：装上全部 extras 后
**462 passed**，与 3.14 完全一致 —— 所以这不是版本差异。

---

## 2. `pyproject.toml` 的 `dependencies = []` 是有意的

**现象**：`cpt/` 里 import 了 `psycopg` / `czsc` / `ccxt` / `pandas` / `plotly` / `wbt`，
但核心依赖是空的，看着像漏写。

**原因**：这是**刻意的**。核心零依赖；每个可选能力对应一个 extra：

| extra | 包 | 缺了会怎样 |
|---|---|---|
| `db` | `psycopg[binary]` | A 股本地数据层连不上 |
| `chan` | `czsc` | czsc 后端报 `CzscNotInstalledError` |
| `report` | `wbt` | 画布 D 返回 `available=false` |
| `crypto` | `ccxt` | 注册表把该源标成 unavailable |

**判定**：`sed -n '16,40p' pyproject.toml`（注释写明了每个 extra 的降级行为）。

> **已修平的不一致**：`psycopg` 现在在 `a_share_local.py:103,190` 两处都是
> `try/except ModuleNotFoundError` → 抛带安装指引的 `AShareLocalError`，
> `a_share_routes.py:pool_payload()` 捕获后降级为 `db_error` 字段，前端不白屏。
> 剩余 `pandas`/`plotly`（`canvas_wbt.py:158,345`）仍是裸 import —— 但画布 D
> 走 `wbt` extra，装 `.[report]` 时连带装上 pandas/plotly，实际不会触发。

---

## 3. `asel.daily_bar_raw.source` 的两个值是**真·多源**，不要去"统一"

**现象**：`sina` 13,695,168 行 / `tencent` 707,414 行 —— 看着像
`ref_adjust_factor` 那种"同一接口分裂成两个标签"的坑。

**但性质完全不同**：这里是**两个真实不同的数据源**，`source` 就该有两个值。
2026-09-25 已把 `asel.ref_adjust_factor` 的 `tencent_fqkline` 合并进 `tx:fqkline`
（那是同一接口写了两遍），**不要顺手把这个也合并掉**。

**判定**：

```bash
.venv/bin/python -c "
from cpt.adapters._dbconfig import connection_kwargs
import psycopg
with psycopg.connect(**connection_kwargs()) as c, c.cursor() as cur:
    cur.execute('SELECT source, source_url, count(*) FROM asel.daily_bar_raw GROUP BY 1,2')
    [print(r) for r in cur.fetchall()]"
# 两个 source 的 source_url 不同 -> 真多源
```

---

## 4. 前端测试大量是对 JS/CSS **源码做字符串断言**

`tests/test_dashboard_ashare_contract.py` 等直接
`read_text()` 后断言 `'document.createElement("optgroup")' in src` 这类子串。

**双向代价**：重构前端会**假红**；行为真坏了也可能**照样绿**。
这是刻意的折衷（没有浏览器 DOM 测试基建），但要知道边界：
**别把"字符串断言通过"当成"功能正常"的证据。**

---

## 5. chromium 测试的红绿取决于**沙箱**，不是代码

`tests/test_dashboard_chromium_smoke.py` / `test_dashboard_chromium_interactions.py`
真起 chromium（`--no-sandbox`，`HOME=Path.home()`）：

| 环境 | 结果 |
|---|---|
| CI（无 chromium） | `skipif` **跳过** |
| 本机沙箱收紧（禁写 `/dev/shm`、`~/.config`） | **假红** |
| 本机沙箱放宽 | 通过 |

**判定**：`ls ~/.local/bin/chromium`；再看失败信息里是不是 `Failed to create
shared memory` / `cannot create directory`。是的话就是环境，不是代码。

---

## 6. `deploy/env/cpt-dashboard.env` 不在 git 里 —— 是**故意的**

只提交 `.example`，真实的 `.env` 被 `.gitignore` 挡住（将来可能放 DB 口令）。

**判定**：`git check-ignore -v deploy/env/cpt-dashboard.env` → 应命中 `.gitignore`。

> 历史坑：`.gitignore` 的 `env/` 规则曾经把**整个 `deploy/env/`** 吞掉，连
> `.example` 都提交不进来，导致 fresh clone 走不通 README 第一步。
> 2026-09-25 已用四条规则放行（`env/` + `!deploy/env/` + `deploy/env/*` +
> `!deploy/env/*.example`）。**改这几行时注意**：只写 `!deploy/env/` 而不写
> `deploy/env/*`，会让真实 `.env` 也变成可提交。

---

## 7. `pre-commit` 报 `Executable 'mypy' not found`

不是配置错 —— 是 `language: system` 的 hook 在当前 PATH 上找不到可执行文件，
因为你没激活 venv。

**判定**：`source .venv/bin/activate && pre-commit run --all-files`。

---

## 8. `docs/pending-wiring.md` 里 16 个模块"零生产导入"是产品决策

不是死代码，是**尚未接线**。2026-09-25 用导入图可达性独立复核过
（根 = `cpt.web.*` + `scripts/*`）：66 个模块中 46 个可达，20 个不可达 ——
20 个里 16 个在这份清单，另 4 个是 `__init__.py` 包标记，**清单没有漏项**。

风险是产品级的：长期不接线会变成事实死代码。**要删要接，是产品决策，不是清理。**

---

## 9. 东财的 `BONUS_IT_RATIO` 是**总数**，和 `IT_RATIO` 相加会**大一倍**

**现象**：含送转的票，重算出的除权台阶与生产差 **16%~29%**（000034 +28.84%、
000880 +28.46%、000403 +23.11%、000551 +16.53%）；而**纯派息的票全部对得上**
（±0.5% 以内）。失配 100% 集中在有送转的日子。

**原因**：列语义是

| 列 | 含义 |
|---|---|
| `BONUS_IT_RATIO` | 送 + 转的**总和**（实测恒在） |
| `BONUS_RATIO` | 送股那部分（可为 `None`） |
| `IT_RATIO` | 转增那部分（可为 `None`） |

2026-10-02 打印原始行核对到的三行：

```
000034 2026-05-19  BONUS_IT_RATIO=4  BONUS_RATIO=None  IT_RATIO=4
000403 2025-06-04  BONUS_IT_RATIO=3  BONUS_RATIO=None  IT_RATIO=3
000034 1996-07-16  BONUS_IT_RATIO=1  BONUS_RATIO=0.3   IT_RATIO=0.7   <- 只有这条是拆分的
```

前两行是「4 送 4 转」的**同一个 4 记了两列**。拿总数当送股、再拿转增加一遍
= 4+4=8（真值 4）。

**判定**（别看因子，看**除权后的实际收盘价**）：

```
000034 2026-05-19  前收 41.57  派息 0.073
  按 10送4 理论除权价 = (41.57-0.073)/1.4 = 29.64   实际收 30.96  ✓
  按 10送8 理论除权价 = (41.57-0.073)/1.8 = 22.99   实际收 30.96  ✗
```

**判定二选一（先打印原始 JSON，别推断列语义）**：把接口返回的一整行打出来，
看哪几列同时非空、值是否相同。

**教训**：「送股列 + 转增列 = 总数」是**直觉**，不是事实 —— 这个源的列里
`BONUS_IT_RATIO` 本身就是总数。凡是**按名称推断语义**，就要用**实际价格**去
校准，且要专门挑**有该字段的样本**验（纯派息的票会把这个 bug 完全掩盖掉）。

**修复后复验**：四只送转票的台阶差从 16~29% 收敛到 **±0.21% 以内**。

---

## 10. 因子台阶「幅度对」不代表「挂的日子对」—— 比倍数看不出日期归属

**现象**：R39 的真机对账说东财算出的单次除权台阶与库里真实跳变 **18/23 匹配、
偏差 ±0.9% 内**，看起来完全没问题。R40 却发现重算侧的后复权价在除权日
**凭空多出一次下跌**（判据：后复权价在除权日应当连续；实测重算侧
**-65.7%**，即把跳空放大了一倍，而生产侧消掉 43.3%）。

**原因**：`factor_from_actions` 从最新一次除权往前连乘，所以 `fmap[s] = Π{ex >= s}`；
一根 bar `d` 要的是「**已经发生**的事件」的乘积 `Π{ex <= d}`。两者互补。
`scripts/factor_recompute.py` 原先取 `Π{ex > d}`，**正好取反** —— 除权日当天
价格已跌，因子却还停在除权前的水平。

**判定**（台阶幅度对不上号时，用这条独立判据，别再比倍数）：

```bash
# 看因子 × 收盘价在除权日的跳空：越接近 0 越对
# 生产 1.0167/1.0265/1.0123/1.0196（向上，抵消除权） vs 原重算 0.9833/0.9769/…
```

**教训**：**台阶幅度对账只能验「倍数」，验不了「哪一天」**。凡是涉及「按日期施加
离散事件」的逻辑，都必须另配一条**独立于推导过程**的判据（这里是连续性）。

---

## 11. `except` 的**顺序**有语义 —— 异常类存在继承关系时

**现象**：把网络超时从「不重试」改成「有界重试」之后，重试**仍然一次都没发生**。

**原因**：

```python
except source.fatal_errors:      # ← 写在前面
    raise
except source.transient_errors:  # ← 永远轮不到
```

`EastmoneyActionUnavailable`（网络）是 `EastmoneyActionError`（解析）的**子类**，
父类分支先吃掉一切。**Python 的 `except` 是自上而下第一个匹配即命中**，
子类永远轮不到自己的分支。

**判定**：加子类时问一句「它的父类在不在另一个 `except` 里」，在就把子类那个
`except` 挪到前面。

**教训**：这类错误**读代码看不出来**（两个 `except` 都"对"），只有注入故障真跑
才暴露。R40 的三个 bug 全是这样。

---

## 12. `getattr(dict, name, default)` 永远返回 default —— 静默失败

**现象**：`latest_run_fingerprint` 读「上一轮算法指纹」时，四项**全为空串**，
于是每一轮结构变化都被误判成「后端变了」。库里那行的四个字段**明明有值**，
日志零告警。

**原因**：`cpt/storage/run_metric_store.py::recent_metrics` 返回的是
`_row_to_dict` 的结果 —— 是 **dict，不是 `RunMetric` 实例**。
`getattr(some_dict, "config_hash", "")` 恒为 `""`。

**判定**：

```python
# 读一行打出来看类型，别信函数签名
print(type(recent_metrics(conn, limit=1)[0]))
```

**教训**：**在动态语言里，`getattr` 带默认值是静默失败的头号来源**。要么用
`row["x"]`（dict 写错就 KeyError，立刻炸），要么显式判 `isinstance(row, dict)`。
宁可炸也不要静默返回空 —— 「空值」和「不存在」在这条路上是两种完全不同的结论。

---

## 13. 网络不可达与「返回坏数据」是**两件事**，别混成一个异常类

**现象**：一次 15s 读超时就让 **2189 只**的整轮重算直接停止。

**原因**：两者都抛 `EastmoneyActionError`，而分类里「不重试」。但解析失败是
**确定性**的（再发一次还是同样的坏数据），读超时是**瞬时**的（隔几秒再试就成了）。

**判定**：现在已拆成两个类 —— `EastmoneyActionUnavailable`（网络，transient）
与 `EastmoneyActionError`（解析，fatal），见 `cpt/adapters/eastmoney_actions.py`。

**教训**：无额度约束的免费源**没有「额度类」失败**，取而代之的是「网络类」，
而后者**恰恰是最该重试的**。套用付费源的「不重试」直觉会直接掐死长跑任务。

---

## 14. `structure_id` 里**没有标的代码**

**现象**：想给结构事件归因，要拿「上一轮的算法指纹」，而指纹按
`(market, symbol)` 存，而事件表不存 symbol。

**原因**：`structure_id` 的形状是 `{market}:{kind}:{level}:{start_time}`
（见 `cpt/domain/structure_events.py::structure_id_of`）—— **不含代码**。
我一开始写了段「从 id 里反解 6 位代码」的逻辑，那是**永远不可能成立**的死代码。

**判定**：`structure_id_of("cn", "bi", 0, 1700000000000)` → `cn:bi:0:1700000000000`。

**教训**：要从 id 反推字段之前，先把构造函数读一遍并**真机打印一个真实 id**。

---

## 15. 「窗口内没有公司行动」⇒ 因子恒定，这是**对**的结果

**现象**：对账报告把 6 只票判成「重算也没算出来」。

**判定**：真机查了 000016 / 000002 / 000826 / 000615 的东财全史，末次分红分别在
**2022-06 / 2023-08 / 2019-07 / 2018-06** —— 窗口内确实一次都没有（都停发或亏损）。
因子**恒定**才是对的；同期生产侧在 152~400 之间乱跳，**那边才是错的**。

**教训**：「算出来是常数」和「没算出来」是两回事，报告里必须分开命名
（现字段是 `new_constant`）。把「结果碰巧是平凡值」当成「失败」，会让人去修
一个根本没坏的东西。

---

## 16. Windows + PowerShell：内联 Python 会被吞

**现象**：`ssh oracle "... python -c \"...\" ..."` 里带中文/引号的代码要么被
PowerShell 提前求值（`$(...)`、`$f` 被本地展开），要么被 GBK 解码把行尾吃掉、
显示成行合并的乱码。

**纪律**：**复杂命令一律写成脚本文件 → `scp` → 远端 `tr -d '\r'` 归一化 → `bash` 执行。**
已多次因此浪费大量时间。CRLF 归一化那一步也别省 —— 从 Windows `scp` 上去的
文件带 `
`，而仓/服务器规范是 LF。

---

## 17. 远端 `git clean` 不带 `-x` 才安全

**场景**：oracle 上要丢弃「scp 上去但从未提交」的文件。

**纪律**：`git clean -fd`（**不带 `-x`**）。`-x` 会连被 `.gitignore` 挡住的
`deploy/env/cpt-dashboard.env` 一起删掉 —— 那是唯一存着飞书 webhook 的地方。
执行前后都应核对 `ls deploy/env/cpt-dashboard.env` 仍在。

另外：**先 tar 备份再清**，并核对「备份里的文件数 == `git status --porcelain`
的行数」，数量不符就中止。

---

## 18. PostgreSQL：事务里一条语句失败 ⇒ 同连接后续全部 aborted（R45 实测）

**这是 R45 storage/ 复盘里两个真 bug 的共同根因。**

实测（PostgreSQL 18.6，`emotion_core` 真库）：

```
① 语句失败:  UndefinedTable: relation "..." does not exist
② 后续查询:  InFailedSqlTransaction: current transaction is aborted,
              commands ignored until end of transaction block
```

**所以「catch 住 DB 异常并返回空值」不是降级，是把一次局部失败放大成整页失败** ——
连接留在 aborted 态，这个连接上后面每一个操作都报错。

### 两个由此派生的陷阱

**① store 层吞异常 ⇒ 调用方的 rollback 防御成了死代码。**

`signal_event_store.load_previous_signal` 原来在 `except` 里 `return None`，
而调用方 `a_share_snapshot` **专门写了** `_rollback_quietly`，注释里就写着
「连接留在 aborted 态连累后面所有查询」—— 但函数自己先吞了异常，
调用方的 `except` 永不触发。

**② `_write(fn) = fn() + conn.commit()` 救不了。**

实测：事务在 aborted 态下 **`COMMIT` 不抛，等于 `ROLLBACK`**。所以只要 store
吞掉异常，外层 commit 会静默回滚并原样返回那个假返回值。
`llm_cases._write` 那道「防忘记提交」（R23/R25 各漏过一次）的防线，
**只在 store 抛异常时有效**。

### 纪律

- 读失败**抛**；「库里没有」用 `None`/空元组表示，**两者不能共用返回值**。
- 写失败**抛**；「重复提交」可以用 `False`，但**不能与写失败共用**。
- 确实要降级的（旁路记账），函数 docstring 加 `# gate: allow-silent: <理由>`，
  **且调用方必须自己 rollback**。
- 判定命令（门禁已自动化）：
  ```bash
  python scripts/check_storage_failure_semantics.py
  ```

## 19. 失败的「reason」比粗糙的「reason」更有害（R45）

`llm/structured.py` 的 docstring 写着「最危险的是 B（合法 JSON、形状全错），
不是 D（压根不是 JSON）」。但 R45 之前，**另一族 B 被归成了 `not_json`**：

```
"just a bare string"   ->  not_json      真相：解析成功了，只是类型不对
123 / null / true      ->  not_json      同上
{"code":200,"data":{}} ->  schema_mismatch   用例 B，本来的对
```

`not_json` 的定义是「找不到任何能解析的 JSON 候选」—— **它明明解析成功了**。
这个 reason 会落进审计表与 UI，把排查方向指向「模型没输出 JSON」，
而真相是「输出的是 JSON，只是形状不对」。

**纪律**：分类/状态码必须**说真话**。「粗略」只是信息少，「撒谎」会把人
引向错误的假设 —— 后者更贵。判断一个归类对不对，要问「它的定义是什么」，
而不是「它落在哪个分支」。

## 20. 测试「绿」不等于测到了东西（R45 一天撞见三次）

同一类错误，一天内出现三次，形态各不相同：

| 形态 | 例子 | 为什么绿着 |
|---|---|---|
| **测 mock 不测真路径** | `test_load_previous_signal_failure_does_not_propagate` 把真函数 monkeypatch 成会抛的替身 | 证明「调用方会兜底」，而真函数根本不抛 ⇒ 那段防御是死代码 |
| **断言的契约本身是错的** | `test_enqueue_swallows_db_error` 断言「写失败回 False」 | 名字叫 `_degrades_gracefully`、docstring 写「别 500」，**看起来在保护 UX**，实际把一个会撒谎的契约焊死了 |
| **比较对象里混进了不该比的** | `test_provider_caches_snapshot_within_ttl` 只排除了 `as_of_ms`，漏了同样按墙钟算的 `close_countdown` | 机器空闲时两次调用在 1 秒内完成、一直绿；一有并发负载就跨秒 → 红 |

**纪律**：
- 判据要能**区分**「真路径」与「mock 路径」—— 尽可能走真函数；
- 写「降级」类测试时先问：**降级后调用方看到的是真相还是谎言**；
- 断言相等前先列一遍：哪些字段是**按墙钟/时间/随机数**算的？它们必须被排除，
  否则测试结果取决于机器快慢。

## 21. 自制的验证工具，第一版基本都错（R45 一天三次）

| 工具 | 错在哪 | 给出的错误结论 |
|---|---|---|
| AST 扫层间依赖 | 匹配了**注释/docstring 里**的 `cpt.domain.` 字样 | 报「分层契约被破坏」 |
| `glob("*.py")` 扫外部触点 | **不递归**，漏掉 `llm/providers/` | 「llm 层 0 网络触点」 |
| AST 找死代码 | 引用来源只扫了 4 层，**漏了 `application/` 与 `web/`** | 「24 个死函数」（实际 3 个） |

**纪律**：验证工具的第一个版本**必须用独立手段交叉验证**再采信。
更稳的做法是**用项目已有的权威工具**（本仓的 `lint-imports` 就是 ——
它一次就判定了分层契约 6 kept / 0 broken，比手写 AST 可靠得多）。

另：改完 Python 记得清 `__pycache__`，否则会看到「修复没生效」的假象（R45 踩过）。

## 22. 比对「同一个量」之前，先确认它**真的是同一个量**（R45）

复盘 `a_share_factor` 时，我在同一个点上错了**两次**：

**第一次 —— 拿绝对值比。** 腾讯 hfq/raw 算出 002294 的因子是 `11.41`，
库里是 `1.00`，看着像差了 11 倍。**其实不是**：`anchor_scale` 会把
**最新一根**缩放到「库里原值」，所以库里的绝对水平**从来不等于** hfq/raw 比值。
比绝对值没有意义。

**第二次 —— 改比形状。** 想那就比相邻交易日的比值（形状与口径无关）。
结果 600519 的台阶最大差 **0.73%**，按我设的 0.1% 阈值判成「有问题」。
**而 600519 正是 R45 全量验证里 13091/13091 台阶全对的那只**。

⇒ 腾讯 hfq 序列与本地因子序列是**两条独立数据源**，它们之间有**固有漂移**
（模块 docstring 记着 R36 实测 600519 相对极差 6.25%）。漂移不是 bug。

### 纪律

验证「两个来源一致」时，先回答三个问题：

1. **两边的量是同一个定义吗？**（锚定值 vs 比值 —— 不是）
2. **它们之间的固有噪声有多大？**（R36 早就量过 6.25%，不是 0.1%）
3. **有没有独立于这两个来源的第三方判据？**
   —— 有：**公司行动理论台阶**。`scripts/factor_recompute.py` 算的
   理论台阶（`(1+送转)/(1-派息/前收)`）才是无歧义的那把尺子，
   R44 实测 13091/13091 精确匹配。

**判据要选「与被验证对象无关」的那一个。** 拿两个都可能有偏的量互相比，
只会得到一个既不是 bug、也不是 no-bug 的中间值。

## 23. A 股 K 线缺口的真实量级是 **11.9%**，不是文档记的 1~2%（R45 实测）

`handoff-20261003` §5 第 5 项写「`derived_bar` 每月 4 月底约 **1~2%** 的票缺
1~10 天」。R45 实测**量级差了近一个数量级**。

## 实测（2026-10-03 真库，5223 只票 / 666 个交易日）

把「天数不足」拆成两类后：

| 分类 | 只数 | 占比 | 性质 |
|---|---:|---:|---|
| 天数基本齐（≥665） | 4366 | 83.6% | 正常 |
| **新股**（起点晚于 2024-01-02） | 234 | 4.5% | 正常，不是缺口 |
| **中间缺口**（起点齐、但天数不足） | **623** | **11.9%** | ← 真的问题 |
| 尾部截断 | 0 | 0% | — |

中间缺口那 623 只：平均缺 **7.4 天**，最多缺 **62 天**（002731）。

## 关键更正：缺口**不在 `derived_bar`**，在 `daily_bar`

handoff 记的是 `derived_bar` 缺天。实测：

    derived_bar 缺同一天、而 daily_bar 有的票：0 只

⇒ 缺口在**行情主数据** `public.daily_bar` 里。`derived_bar` 只是继承。
成因确实在 ingest 侧（另一个项目）—— 这一点 handoff 猜对了。

## 不是系统性故障（我一度判断错了，纠正）

看到 600289 连缺 7 个整交易周（2024-11-11 ~ 12-27，每周都是周一到周五），
我判断是「批量 ingest 作业失败的特征」。**错的。** 查缺口当天的全市场覆盖率：

    2024-04-30  95.6%     2024-12-02  96.7%
    2024-11-11  96.6%     2025-01-06  96.8%
    2024-11-18  96.6%     2026-04-29  98.4%

**666 天里没有任何一天覆盖率低于 90%** ⇒ 那些天市场正常交易，
缺口是**个体级**的：长期停牌（C3 合法）或该票丢单。
**仅凭库内数据分不出这两者** —— 要分得看停牌公告，CPT 没有这个源。

## 为什么没人发现

`validators.validate_ashare_bars` **刻意跳过连续性检查**（R15/R16）：
A 股的周末/节假日/停牌缺口是合法的，不能被 `DataGapError` 拦下。
这个设计对 C5/C3 是对的，代价是**对 ingest 丢数据也一并放行** ——
623 只票的洞就这么静默进了结构计算。

⇒ 想抓 ingest 丢数据，需要一个**独立于 K 线本身**的判据
（本项目里现成的一个是 `public.trade_calendar`），而不是在 bar 序列里找洞。

## 24. `except` 块**内部**抛出的异常，不会被兄弟 handler 接住（R45 连踩两次）

R45 复盘 `a_share_snapshot` 时，我在**同一处**错了两次。

代码形状：

    try:
        result = fetch()                      # 抛 AShareNoFactorError
    except AShareNoFactorError:
        outcome = _try_on_demand_factors(...)  # ← 在 except 块**内部**
        ...                                    # 这里再出错怎么办？
    except Exception:                          # ← 接不住上面那支！
        ...

**第一次修法（错）**：把内层的 `except Exception: return None` 删掉，
以为「让它冒到下面的 `except Exception` 就能统一处理」。

**结果更糟**：异常直接逃出 `build_ashare_snapshot` —— 原来至少还返回一个
degraded 快照，改完变成 500。

**为什么**：`except` 块内部抛出的异常，**不会**被同一个 `try` 的**兄弟**
`except` 子句捕获（那些子句只匹配 `try` 主体里抛出的异常）。

**正确修法**：在 `except` 块**内部**自己 try/except，就地处理
（记日志 + rollback + 如实报 `db_error`）。

## 判据

改动跨了 `try` 边界时，先问一句：**这段代码在 `try` 主体里，还是在某个
`except` 块里？** 后者的异常流向与前者完全不同。

配套的一条：`scp` 到远端后**必须校验文件真的更新了**（`grep -c` 目标标识符）。
R45 有三轮「改完测试不对」，真因是 scp 静默失败、跑的还是旧文件 ——
比代码 bug 更浪费时间的坑。
