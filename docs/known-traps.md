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
>
> **R51 已消解**：曾剩下的 `pandas`/`plotly` 裸 import（`canvas_wbt.py:158,345`）
> 随画布 D 与 `report` extra 一并删除。现在 `cpt/` 下**没有任何 pandas/plotly
> import** —— 这同时是 CI 那盏红灯（`test_canvas_d_trust_boundary.py` 因 CI 未装
> pandas 而 `ModuleNotFoundError`）的根因，一并根除。

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

## 8. `docs/pending-wiring.md` 里的"零生产导入"**当时**是产品决策 —— R45 更正

不是死代码，是**尚未接线**。2026-09-25 用导入图可达性独立复核过
（根 = `cpt.web.*` + `scripts/*`）：66 个模块中 46 个可达，20 个不可达 ——
20 个里 16 个在这份清单，另 4 个是 `__init__.py` 包标记，**清单没有漏项**。

风险是产品级的：长期不接线会变成事实死代码。**要删要接，是产品决策，不是清理。**

> ⚠️ **R45 更正（2026-10-04）：「16 个」已经过时。**
> **R22（2026-09-30）接掉了 9 个模块 + 3 个函数**，R41 复核、R45 再次 grep 确认，
> 现在**只剩 1 项**：`cpt/domain/a_share_rules.py::t_plus_one_purchase_allowed`
> （仍无任何生产调用方）。
>
> ⇒ 本条的**结论仍成立**（剩下的那一项确实是有意不接），但**数量必须改口**。
> 照原文的「16 个」去理解，会以为这批模块整体没动过 —— 而实际上
> 一多半已经接进生产了。**这是「数量过时」比「结论过时」更危险的一类漂移**：
> 结论对，容易让人以为整条都还成立。

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

---

## 25. 「确认无数据」被当成「失败」—— 一个 `if` 分支的两种待遇（R45）

**症状**：`asel.ref_adjust_factor` 里 106 只 placeholder，实测分类是

| 类别 | 只数 | 真相 |
|---|---:|---|
| 本地 K 线 < 30 根 | 16 | **正当失败** —— 新股，数据真不够 |
| K 线 ≥ 30 根 | 90 | **不该是占位** —— 至少 666 根全历史都有 |

90 只失败的 note 统一是「eastmoney 无公司行动记录」。

**根因**：`scripts/factor_recompute.py` 里**紧挨着的两行**给了同一种情况两种待遇：

```python
if not actions or all(d < first_bar for d in ex_dates):
    if not actions:
        return CodeResult(code, False, note="...无公司行动记录")   # ← 记失败，不写行
    const = ...                                                   # ← 写恒定行 ✓
```

**而它上面那段注释恰恰写着这个代价有多坏**：

> 拒写的代价是它永远留在占位状态，而且 `failed` 不进 `done` ⇒
> 每天的定时重算都会把它重新查一遍（**每天白烧 74 次东财调用，且永远失败**）。

⇒ 作者修好了 `all(d < first_bar)` 那一支，**把语义完全相同的
`not actions` 那一支留在旧行为上**。不是判断错，是**只改了一半**。

### 为什么现在修它是安全的（不是把故障当成「没分过红」）

风险真实存在：若把「接口挂了」当成「这只票没分过红」而静默写进暂存表，
就是**把故障洗成数据**。所以关键在于**上游已经把两者分开了** ——
`cpt/adapters/eastmoney_actions.py` 只在 `_is_no_data` 命中
（东财明说 `code=9201 返回数据为空`）时才返回空元组，
其余一律 raise 成 `EastmoneyActionError` → fatal。它的判据写得很克制：

> 判据不是「success 为假」，而是「**服务端明说查无此数据**」…
> 宁可漏判（退回 fatal、旧行为）也不误判（把接口故障当成「没分过红」）

⇒ 走到 `not actions` 时，那是**已确认的「没有公司行动」**，
窗口内因子恒定**就是正确答案**，应当写入。

**实测（2026-10-04 04:17 真机，dry-run）**：
`001365 → 96 行 / 0 台阶`、`301292 → 666 行 / 0 台阶`、
`301565 → 556 行 / 0 台阶`，全部从「失败」转为「完成」。

### 判据

```sql
-- 有 K 线却是占位，且 note 是「无公司行动记录」⇒ 命中本条
SELECT count(*) FROM asel.ref_adjust_factor
 WHERE source IS NULL
   AND code IN (SELECT code FROM public.daily_bar
                 GROUP BY code HAVING count(*) >= 30);
```

> ⚠️ 别把它和 **#15**（窗口内无公司行动 ⇒ 因子恒定是**对**的结果）混起来。
> #15 说的是「恒定是对的」；本条说的是「**正因为对，就该写进去**，
> 不该留在占位状态让每天白烧一次外部调用」。

---

## 26. `A or B` 里如果 `A` 是**列索引 0** ⇒ 它会被当成「没找到」（R45 修的真 bug）

**症状**：某些标的的 Wind 公司行动**静默返回空**（`parse_corporate_actions` → `()`），
上层报「Wind 无公司行动记录」—— 一个**看起来像数据缺失**的错误，
而真实数据就在返回体里。

**根因**（`cpt/adapters/wind_source.py`，2026-10-04 修）：

```python
i_date = _pick_column(columns, "除权除息日") or _pick_column(columns, "分红红股上市日")
#                        ↑ 返回列索引
```

`_pick_column` 返回的是**列索引**，而**日期列常常正好是第 0 列**。
`0 or X` 在 Python 里是 **`X`** ⇒ **索引 0 被当成「没找到」**，
于是 `i_date` 退化成 `None`、函数早退返回空元组。

**为什么特别难发现**：同一份 docstring 明写
「**列集合按标的、甚至按次而变**」——
所以「日期列是不是第 0 个」**随输入而变**：

| 标的 | 列顺序 | 结果 |
|---|---|---|
| A | 日期在第 2 位 | ✅ 正常 |
| B | **日期在第 0 位** | ❌ 静默返回空 |

**同一个函数、同一个版本，有的票正常有的票静默失败。**
而且失败的形态是「没有数据」——**和「真的没有」长得一模一样**。

**修**：显式判 `is None`：

```python
i_date = _pick_column(columns, "除权除息日")
if i_date is None:
    i_date = _pick_column(columns, "分红红股上市日")
```

**判据**

```python
# 任何 `x = f() or g()` 都要问一句：f() 会不会返回 0 / "" / [] / None？
# 这些在真值判断里全部等价于「没找到」。
```

⚠️ 同文件另外四个 `i_pre` / `i_post` / `i_bonus` / `i_transfer` 是
**直接赋值**，所以索引 0 对它们没问题 ⇒ **只有日期这一列中招**。
这也说明「抄四遍同一模式」时，**错的那一遍要单独找出来**。

### 全仓扫过一遍，同类只有这一处（R45）

```bash
grep -rnE "(index|idx|col|pos|offset|ix)\w* *= *[a-z_]+\([^)]*\) +or " cpt/
```

其余命中全是**良性**的，因为左边压根不会是 0：

| 位置 | 形态 | 为何良性 |
|---|---|---|
| `reference_backend:386-390` | `int(f.get("x", 0) or 0)` | 两边都是 0，收敛成默认值本就正确 |
| `a_share_factor:552` | `getattr(act,"share_ratio",0.0) or 0.0` | 同上（None → 0.0 是有意的） |
| `app.py:900` | `find_run(run_id) or _index_row_from_body(...)` | `find_run` 返回 **dict 或 None**；非空 dict 恒为真，None 才回落 ⇒ 正确 |

⇒ **判据不是「用了 `or`」，而是「左边函数的返回值合法地包含 0 / "" / []」**。
这条 grep 只能**筛出候选**，逐个看清左边是什么才算结论。

---

## 27. 装饰性查询放在 `try` **外面** ⇒ 它坏了会带崩主功能（R45 修）

**症状**：`a_share_routes._signal_history`（推荐卡的「信号历史」区，R45 当天新加）
构造客户端时写在 `try` **之前** ⇒ 构造一抛就穿出去。

**后果比看上去大**：调用方 `build_recommendation` 的兜底会把**整个推荐**降级
⇒ 「信号历史」这个**装饰**坏掉，会让**「动作 + 参考价」一起消失**。

```python
# ❌ 原来
client = AShareLocalClient()      # ← 抛了就穿出去
try:
    ...
except Exception:
    return {"available": False, ...}

# ✅ R45
client = None
try:
    client = AShareLocalClient()
    ...
except Exception:
    return {"available": False, ...}
finally:
    if client is not None:
        client.close()
```

**判据**：**可选 / 装饰性的东西，它的失败路径要包在它自己的 `try` 里**，
不能指望调用方的兜底 —— 调用方的兜底粒度通常比它粗
（这里就是「整个推荐」而不是「历史这一块」）。

⚠️ 这条是 `tests/test_signal_history_fallbacks.py` 的最后一条用例逼出来的，
而那条用例只是**顺手**把 `AShareLocalClient` 换成会抛的假对象。
**假对象越"坏"，越容易照出真问题。**

---

## 28. 测试会往**生产表**写数据 —— 而读路由是「表优先」（R45 修）

**症状**：`cpt_dashboard_run` 里混着测试快照（实测 89 行，最早 2026-10-01），
且 `test_timestamp_falls_back_when_runtime_omits_generated_at` **长期失败**。

**根因**（两层，都是隔离问题）：

1. **写**：web 契约测试用 `tests/conftest.py::served` 起**真 server** 打**真请求**，
   于是真的 `upsert_run` 进生产表。测试与生产**共用同一个库**
   （`~/.dbconfig` 的 `$DBNAME`，没有测试库）。
2. **读**：`/api/dashboard/runs` 是「**表优先**」⇒ 测试读回的是
   **别的测试写的行**，`rows[0].generated_at` 是墙钟时间而不是本例的 `as_of_ms`。

还有第三层：`_RUN_RING` / `_RUN_BODIES` 是**模块级全局**，跨测试残留。

**修**（`CPT_RUN_STORE_PERSIST=0`，读写**两端**都管住）：

- 只关写端**不够** —— 表里还留着历史行，读端照样优先；
- `tests/conftest.py` 加 autouse fixture：关双写 + **每个测试前后清空 ring**；
- **生产默认行为不变**（不设该变量时照旧双写 + 读表）；
- `tests/test_dashboard_runs_persisted.py` 专门验证落库，
  所以它**显式反向打开**该开关 —— 否则它测的是「不写库」。

**清理**：按 `SYM%` 代码 / `data_source in (fixture, native_fixture)` /
`market_24h.reason in (demo_mode_no_upstream, fixture_mode_no_upstream)`
删掉 **54 行**能确证的测试数据，保留 35 行 `db_local`。

### 为什么这条值得单独记

它不是「测试写得不好」，是**测试在改生产数据**。
而症状（一个长期失败的测试）看起来像「代码有 bug」——
我因此把 `test_signal_stats_route_...` 归成「既存基线失败」喊了一整天。

⇒ **一个失败若长期不被修，先怀疑它是不是真问题**，
别用「不是本轮引入」当解释 —— 那是个会传染的标签。

---

## 29. 沙箱里 `git fetch` 静默失败 ⇒ `origin/main` 长期陈旧（R45）

**症状**：三端同步时，本地（沙箱）显示

```
本地 HEAD     f826770   ✅
origin/main   1f7d098   ❌ 落后 100 个提交
```

而 Oracle 与 GitHub 都是 `f8267702` —— **只有沙箱这一端"冲突"**。

**根因**：沙箱出网走 **TLS 中间人代理**（自签
``CN = ack-agent-identity-proxy``），系统 CA 里没有它 ⇒
``git fetch`` 报 ``server certificate verification failed``。
**而 `fetch` 失败时 git 保留旧的 remote ref、不删除它**
⇒ ``origin/main`` 一直停在最后一次成功 fetch 的位置，**看起来像分叉，其实是陈旧**。

**修**：把代理证书取出、只配进**本仓** ``.git/config``（不动全局）：

```bash
openssl s_client -connect github.com:443 -servername github.com </dev/null 2>/dev/null \
  | openssl x509 > .git/git-proxy-ca.crt
git config http.sslCAInfo "$PWD/.git/git-proxy-ca.crt"
git fetch origin        # 之后 origin/main 正常更新
```

⚠️ 注意这个证书**不能提交进仓**（它是环境产物，且不该让别处的机器依赖它），
所以加进 ``.gitignore`` 的风险是「换台机器又要重配一遍」——
判据写在这里就是为��。

**怎么确认是「陈旧」而不是「真分叉」**：``git rev-list --count HEAD..origin/main``
给出落后数；若同时 ``origin/main..HEAD`` 为 0，就是**单向落后**（陈旧），
双向都有才是分叉。**先看方向再动手**，别一上来就 reset。

---

## 30. 一个未闭合的 `<script>` 标签 ⇒ **功能静默消失**（R45 真踩）

**症状**：「结构判断」卡里那段**说人话**的 LLM 摘要不见了。
但**页面完全正常**：`/cpt/` 200、`/health` 200、推荐接口 200、
**console 零错误、零 pageerror**。

**根因**（拆 `dashboard.js` 时一次批量替换把 `</script>` 落错了位置）：

```html
<script src="./url_safety.js" defer>            ← 丢了 </script>
  <script src="./cpt_job.js"></script></script> ← 多了 </script>
```

浏览器按文档顺序解析：第二个 `<script>` **被当成第一个脚本文本吞掉**。
于是

- `cpt_job.js` **从未成为 script 元素、从未被请求**；
- `window.CPTJob` 恒 `undefined`；
- 轮询函数走「缺它就隐藏」的兜底 ⇒ `box.hidden = true` ⇒ 摘要不显示。

### 为什么这类故障最危险

它**同时满足**「页面正常」「接口 200」「console 干净」——
**所有常规检查都通过**，只有一个功能悄悄没了。

我甚至在 `cpt_job.js` 的注释里写了「缺失时要**响亮失败**，不静默回退」，
而调用方做的恰恰是**静默回退**（`box.hidden = true`）——
**注释写了纪律，代码没执行纪律。**

### 排查弯路（都是老毛病）

1. 先怀疑浏览器缓存 → 加 cache-buster，**没用**
2. 比对本地/线上 md5 → **三处完全一致**，不是部署问题
3. 查 DOM → script 标签**根本不在**
4. `grep -c "<script"` = 11，但按 `src=` 精确 grep 只中 2
   ⇒ **是我的 grep 模式在嵌套引号里被破坏**，量错了还差点归因给服务器

### 修 + 防

- 修 `</script>` 位置，并给 `cpt_job.js` 补上 `defer`（与其余 10 个一致）
- **门禁⑨**（`check_all_claims.py` 的 H 类）：检查 `<script>` 标签
  **是否成对**、**是否吞掉了后续标签**；已给它造已知错例并接进自检套件

### 判据

**「页面正常 + console 干净」不等于「功能在」。**
改完必须**真机打开页面、亲眼看到那个具体功能**，
不能只看 HTTP 200 和控制台 —— 这条今天栽了两次
（另一次是 parity 塞独立 `<script>` 导致画布 D 标签消失）。

## R52：YAML 块标量把 5 个门禁整段吞了（2026-10-05）

**症状**：CI 恒红，但日志里 pytest **绿**、所有门禁输出都打了 ✅，
最后一行却是一条看不懂的：

```
line 33: -: command not found
##[error]Process completed with exit code 127.
```

**根因**（在 `.github/workflows/ci.yml` 自己，不在任何脚本里）：

```yaml
      - name: Static quality gates
        run: |
          python scripts/check_doc_drift.py
          - name: Dashboard bundle in sync      # ← 缩进 10 > run: 键的 8
            run: python scripts/build_dashboard_bundle.py --check
```

`run: |` 是**块标量**：其后所有缩进比 `run:` 键**更深**的行都是字符串内容。
那 5 个 `- name:` 于是不是 step，是 shell 脚本的行 ⇒ bash 执行
`- name: Dashboard bundle in sync` ⇒ 报 `-: command not found` ⇒ 整个
`Static quality gates` 步骤 exit 127。

**后果不是"红"，是"没跑"**：`build_dashboard_bundle.py --check`、
`check_all_claims.py`、`check_doc_counts.py`、`check_enqueue_skeleton_unique.py`、
`check_job_poll_unique.py` 在 CI 上**一次都没真正执行过**。

**为什么这么久没人发现**：引入于 `1cda27af1`（R45 dashboard 拆分），
此后一直被 pytest 的红灯**挡在前面** —— pytest 先失败，那个步骤根本轮不到执行，
缩进错误零暴露。R51 删掉画布 D 让 pytest 转绿，它才浮出来。

### 防

**门禁⑩ `scripts/check_ci_workflow.py`**：把每个 `run: |` 块标量的**实际内容**
抠出来，若内容里出现 `- name:` / `- uses:` / `- run:` 开头的行 ⇒ step 被吞，rc=1。
**不用 PyYAML**（CI 装了 pyyaml，本仓 `.venv` 没有；门禁不该依赖装不装得上的包）。
已用 `1cda27af1` 的真实坏文件验证能抓到，并接进 `selftest_gates`（9 道 → 10 道）。

### 判据（这一类的通用判据）

> **「CI 绿」不等于「门禁在守」。** 判断一道门禁有没有效，要看三件事：
> ① 它在不在 `ci.yml` 里；② **它那一行到底有没有被执行**（日志里能 grep 到）；
> ③ 它的退出码真的被 `set -e` 传播了吗。
>
> 本仓吃过**三类**同源问题，全部是「只产生看不见、不产生测试失败」：
> R45 的恒返回 0 门禁、R49 的判据依赖本机环境、R52 的 YAML 缩进吞 step。
> 共同点：**每一个都需要人肉去查一次 CI 日志才能发现**。

---

## R53：校验器放行 0 价 K 线，零价线静默进结构计算（2026-10-05）

### 症状

两只不同的坏法，**只有一只有声音**：

| 形态 | 行数 | 现象 |
|---|---|---|
| `close≠0`（上游填了前收盘价） | 17 | `DataValidationError` ⇒ **整只票降级**（`invalid_bars:*`） |
| `close=0`（完全空行） | 18 | **什么都不报** ⇒ 零价 K 线进分型/笔/中枢 |

### 根因

`public.daily_bar` 在 2026-09-28~09-30 三天里一次批量写出 **35 行
`open=high=low=0` 且 `vol=amt=0`** 的废行，横跨 **18 只票**（全在热门池内）。

`cpt/adapters/validators.py::validate_ashare_bars` 的 `low<=open,close<=high`
判据对 `0,0,0,0` **判真**（`0<=0<=0`）⇒ 零价 bar **合法通过**。
`cpt/adapters/a_share_local.py::fetch_validated_klines` 原样透传，不拦。

⇒ 画布上多一根 0 价针，`compute_domain_structures` 照它算分型，
造出**假分型/假笔/假中枢**，而 `data_quality.severity` 仍是 `ok`。

**为什么这么久没人发现**：它**不产生任何失败信号**。没有异常、没有 WARNING、
门禁全绿、快照 `severity=ok`。这是本仓第四类「只产生看不见」的问题，
前两类见 R45/R49/R52，第四类则是**数据本身有毒、而契约判据没覆盖**。

### 修法（R53）

- **adapters 层丢弃**：判据 `op == 0 and hi == 0 and lo == 0` —— **不看 close**，
  正是为了让「校验器会放行」的那一档也被拦下。用原始值判（复权因子再正常，`0 * f` 仍是 0）。
- **不修校验器去「容忍」**：`0` 价 K 线本就不合法，放它进去违反
  `docs/rules.md` §5.3「非交易日不出图、不用 0 填充」。
- **不靠 SQL `WHERE` 静默过滤**：违反本仓「缺失要响亮失败」纪律。
- **可观测**：丢弃的日子进 `AShareFetchResult.skipped_placeholder`（与
  `skipped_no_factor` 分开记，两者对上游的指控完全不同），应用层打
  **WARNING** 带行数与样例日期。
- **全占位** ⇒ 抛专属 `ASharePlaceholderRowsError`，
  reason `placeholder_rows`，**不混进** `no_factor` / `no_data` / `db_error` ——
  查因子表是白查，真凶是采集。

### 判据

> 校验器判「合法」不等于「数据对」。**任何"看起来合法"的数值都要问一句：
> 它在业务上说得通吗？** `0<=0<=0` 成立，但一根 0 价 K 线在业务上没有意义。
>
> `0` 是这类陷阱的高发值：`0` 既是合法的「无成交/无数据」标记，
> 也是「上游忘了填」的默认值，**判据层面无法区分** ⇒ 必须在业务层显式排除。

## R54：门禁验错了属性 —— 行号「没越界」不等于「还指对」（2026-10-05）

### 症状

`python scripts/check_all_claims.py` 报 `✅ 全部断言对得上`（共 1428 条断言），
但文档里的 `file.py:123` 有 **30+ 处**正指着完全无关的代码。实测样本：

| 文档引用 | 那一行实际是什么 |
| --- | --- |
| `docs/pending-wiring.md` 的 R22 接线表十处 | **全错**（`cpt/web/app.py` 整体右移约 280 行） |
| `docs/known-traps.md:669` 的 `app.py:871` | 真身已漂到 `app.py:900` |
| `docs/duplication-triage.md` 约 30 处 | `cpt/adapters/a_share_local.py` 自身从 225 漂到 579 |

### 根因

`check_all_claims.py` 的 L 类判据只有一句（约 `:423`）：

```python
if ln > n:                      # n = 文件总行数
    bad["L 行号越界"].append(...)
```

**只验「行号没有超出文件总行数」，从不读那一行的内容。** 而同一文件的
docstring `:24` 自述 L 类要验「那行还在不在」—— 声明与实现对不上。

### 这是第四类静默门禁失效

| 轮次 | 失效形态 | 表现 |
| --- | --- | --- |
| R45 | 门禁恒 `return 0` | 跑起来永远绿 |
| R49 | 断言依赖本机环境 | 本机绿、CI 红（或反之） |
| R52 | YAML 块标量吞掉 step | 5 个门禁**一次都没执行过** |
| **R54** | **门禁验错了属性** | 跑了、绿了，但查的是一个**弱于声明**的条件 |

四类的共同点：**都不产生测试失败**。所以「CI 绿」只能证明「CI 跑完了」，
证明不了「CI 查的东西是它声称要查的那件事」。

### 防御（门禁⑪ `scripts/check_line_refs.py`）

判据刻意保守，宁可漏报也不造假阳性：

1. **严格解析目标文件**：完整路径优先，否则 basename 全仓唯一匹配；
   **同名多份直接跳过，绝不猜** —— 猜错会造假阳性，假阳性会让门禁被无视。
2. 行号越界 ⇒ 失败。
3. 取**归属本次引用的片段**（上一个引用结束 → 下一个引用开始），在其中找
   「确实被该文件 `def`/`class` 定义」的标识符当锚点；必须**恰好命中一个**，
   0 个（引用的是调用点 / 那段文字里没有符号名）或多个（歧义）都跳过。
   这一步绕开了 `` `a.py:225` / `b.py:389` `` 这类**列表式引用**。
4. 锚点在 `NNN ± 3` 行内出现（**含调用点**，不要求是定义行）⇒ 通过。

历史归档文档（`duplication-triage.md` / `progress-log.md` / `handoff-*.md` /
`review-*-r45.md`）整体豁免 —— 它们的行号是**当时现场的归档证据**，
改写等于伪造记录；但豁免表**每条必须写明理由**，空理由直接 rc=1。

### 判据

> **「门禁绿了」要再问一句：它查的是不是它声称要查的那件事？**
>
> 声明与实现分家的门禁最危险：它比「没有门禁」更糟，因为它给了一种
> 虚假的覆盖感。检查办法很土 —— **拿一个已知的错例去喂它**。
> 这正是 `scripts/selftest_gates.py` 存在的理由，门禁⑪ 已登记其中。

---

## R55：给 ①② 补门禁 —— ①② 此前只有自检「泛泛兜着」（2026-10-05）

R54 把四类静默失效列全了，但其中 **R45（门禁恒返回 0）** 与 **R49（断言依赖本机
环境）** 两类**没有专盯的门禁**，只有 `selftest_gates.py` 泛泛兜着。R55 把这两类
各自的残量缺口找准、补上，并顺手证明了「② 立不出静态判据」。

### ① 的残量缺口：没有东西保证「自检清单」与 CI 同步

`selftest_gates.py`（R45 的对策）给每个门禁配一个**已知错例**，先证明它能红。
但它那份 `FIXTURES` 清单是**手工维护**的，而 **没有任何东西**保证它与
`.github/workflows/ci.yml` 同步。于是：

> 新门禁接进 CI、却忘了配夹具 ⇒ 它**逃过体检**，而 CI 依然全绿 ——
> 因为**它没红**。

病根与 R45 的「门禁恒返回 0」是同一个（**全绿只是它什么都没查**），
只是入口从「门禁自己坏」换成了「门禁逃过体检」。门禁⑩（`check_ci_workflow.py`）
只查 YAML 缩进，不管这个。

**防御：门禁⑫ `scripts/check_gate_coverage.py`** —— 三方对齐：

| 集合 | 来源 |
| --- | --- |
| `A` | `ci.yml` 里 `python scripts/x.py` 调起的门禁（跳过注释行，排除自检自己） |
| `B` | `selftest_gates.py` 里 `FIXTURES` 的键（**用 AST 解析**，非正则 —— 要证明那些键确实是夹具） |
| `C` | `scripts/check_*.py` 实际存在的文件 |

三条规则：**R1** `A ⊆ B`（逃过体检）；**R2** 夹具指向的脚本必须真实存在（死夹具）；
**R3** `C ⊆ A`（闲置门禁 —— 写了却从不执行）。豁免 `NOT_A_GATE` 两条，
**每条必须写明理由**（`scan_doc_claims.py` 是一次性普查工具；
`verify_public_contracts.py` 依赖真实上游、只能人工按需跑）。

> 配置即约束：这份清单**跟着代码一起长**。新加门禁必须**同时**改 `ci.yml` 和
> `FIXTURES`，改了半边就会被门禁⑫ 拦住。

### ② 为什么立不出静态判据（两条路都实测否决）

| 试探 | 实测结果 | 结论 |
| --- | --- | --- |
| **名字启发式**：用例名含 `missing\|unavailable\|degrade\|absent` ⇒ 必须有 patch | 命中 99 个，其中 61 个看不到 patch；但逐条看，那些「缺」是**数据缺**（缺一天 / 缺一列 / 缺一个字段，由用例自己构造） | 假阳性高到会让门禁被无视，**否决** |
| **环境探测启发式**：出现 `shutil.which` / `Path.home` / `os.environ` / `platform.*` ⇒ 必须有 skip 守卫 | 只有 7 个模块探环境，5 个已有守卫，余下 2 个是 `subprocess.Popen` 启自家服务（正当）。**今日零违规** | 立起来只是摆设，且**抓不到 R49 真身** —— 那例代码里根本没有探测调用，靠的是「本机恰好没装 Wind」这个**沉默事实**。**否决** |

⇒ ② 的真身是**执行级**的，不是文本级的。**判据只能是「换个环境再跑一遍」。**

**防御：门禁⑬ `scripts/check_cold_environment.py`** —— 以 `PATH=/nonexistent`、
空 `HOME`（其余环境原样保留）重跑整个 `tests/`：

- 有 failed / error ⇒ 该用例的结论是**本机环境的函数** ⇒ 判失败；
- `skip` **不算**失败（「本机没有这个能力」是 skip 的正当用途），但**逐条打出来**
  —— 「绿灯来自没执行」比红更糟。

实测：冷跑 `1083 passed, 11 skipped`（正常跑 `1090 passed, 4 skipped`，
差的 7 条正是 `node 不可用，跳过 JS 行为契约`），耗时 **72~75 秒**。

### 判据

> **同一类病会换入口复发。**
>
> R45 是「门禁自己恒返回 0」，一到 R55 就变成「门禁逃过体检」；
> 形态变了，**病根没变** —— 都是「全绿」代替了「查过」。
> 补门禁时要顺着病根问一句：**这个病还有别的入口吗？**
>
> 另外一条：**判据立不立得住，要用数据试，不要用直觉定。**
> ② 的两条静态路都是写之前先在真仓上量过的（99 个命中 / 7 个模块），
> 量完才知道不能立 —— 若凭直觉立了，得到的是一道天天误报的门禁。
