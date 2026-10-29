# 已知陷阱与非缺陷清单（known traps）

> 建立：2026-09-25 仓库坑排查。目的：把**看起来像 bug 但其实是设计/环境使然**的东西
> 集中记下来，避免下一个会话把它们当缺陷去"修"，反而改坏。
>
> 每条都给了**判定命令**——先跑命令，再下结论。

---

## 1. CI 里有一批测试被 skip，不是缺陷

**现象**：本地与 CI 的 `passed / skipped` 数不一样，CI 里多出一批 `s`。

**原因**：`czsc` / `ccxt` 这些**可选依赖**只在 extras 里
（`pyproject.toml` 的 `[project.optional-dependencies]` 的 `chan` / `crypto`），
CI 只装 `requirements-dev.txt` + `.[db]`，于是相关测试**优雅跳过**。
而 `czsc` 的参考实现在 `references/` 下，**该目录不入 git**
（见 `.gitignore`），所以 CI 结构上就拿不到它们。
另有一类 skip 是**数据前提不成立**（库里没有真实轮次 / 没有存量行），
那也不是缺陷 —— 它明确说明了「这条无从验证」。

**判定**（别看数字，看**原因**；数字会随用例增长而变，背数字必错）：

```bash
.venv/bin/python -m pytest tests -rs 2>&1 | grep SKIPPED | sed 's/.*: //' | sort -u
# -> could not import 'czsc' / could not import 'ccxt' / 表里没有真实轮次 …
```

**要全量覆盖**：`pip install -e ".[dev,chan,crypto,db]"`（`report` extra 已随 R51
画布 D 下线删除，不再是有效 extra）。

> 2026-10-29 复核：本条原先写死「`462 passed` / `434 passed, 28 skipped`」，
> 早已与现状无关（当时 CI 还是**全红**的，28 这个数字本身也不可信）。
> 现在只保留机制与判定命令。

---

## 2. `pyproject.toml` 的 `dependencies` 只有一条，且 extras 是**可选能力**

**现象**：`cpt/` 里 import 了 `psycopg` / `czsc` / `ccxt`，但 `dependencies`
里只有 `emotion-cpt`，看着像漏写。

**原因**：这是**刻意的**。2026-10-23 判据内核迁到 Emotion-CPT 之后，
`dependencies` 只剩**一条**——`emotion-cpt @ git+…@v0.8.4`（钉 tag 不钉 main），
因为它是「判据」而不是三方库；其余能力都走 extra：

| extra | 包 | 缺了会怎样 |
|---|---|---|
| `db` | `psycopg[binary]` | A 股本地数据层连不上 |
| `chan` | `czsc` | czsc 后端报 `CzscNotInstalledError` |
| `crypto` | `ccxt` | 注册表把该源标成 unavailable |

（`report` / `wbt` 随 R51 画布 D 下线**已删除**，不要再往这里加。）

**判定**：`sed -n '/^dependencies/,/^\[/p' pyproject.toml`（注释写明了每个 extra
的降级行为与主版本上界）。

> 2026-10-29 复核：本条原写作「`dependencies = []` 是有意的」，在判据内核迁移后
> 已不成立（当时 README 仍写「核心依赖为空」）。以 `pyproject.toml` 为准。

> **已修平的不一致**：`psycopg` 现在在 `a_share_local.py:103,190` 两处都是
> `try/except ModuleNotFoundError` → 抛带安装指引的 `AShareLocalError`，
> `a_share_routes.py:pool_payload()` 捕获后降级为 `db_error` 字段，前端不白屏。
>
> **R51 已消解**：曾剩下的 `pandas`/`plotly` 裸 import（`canvas_wbt.py:158,345`）
> 随画布 D 与 `report` extra 一并删除。现在 `cpt/` 下**没有任何 pandas/plotly
> import** —— 这同时是 CI 那盏红灯（`test_canvas_d_trust_boundary.py` 因 CI 未装
> pandas 而 `ModuleNotFoundError`）的根因，一并根除。

### 运行时锁文件：**故意不锁**，但 extras 必须有主版本上界（R59，审计 L16）

本仓只有**开发/CI** 锁文件 `requirements-dev.txt`（由 `requirements-dev.in` 经
pip-compile 生成）。运行时依赖（extras）**故意不提交锁文件**：生产是
`pip install -e ".[db,crypto,chan]"` 按 `pyproject.toml` 现场解析安装，`chan` 用
`czsc==1.0.1` 精确锁定，`db`/`crypto` 各有独立的降级路径（缺包时对应能力标
`unavailable`，不拖垮实时看板），给 extras 再维护一份跨 Python 版本的锁文件
收益低于维护成本 —— 这是取舍，不是遗漏。

但「不锁」不等于「开区间」：`ccxt` 原先只有 `>=4.0`、`psycopg` 只有 `>=3.1`，
部署时会把任意未来**主版本**直接装进生产。R59 已补主版本上界
（`ccxt>=4.0,<5`、`psycopg[binary]>=3.1,<4`）；跨主版本升级需人工改这里再上。

**判定**：`grep -n 'ccxt\|psycopg' pyproject.toml` 应看到带 `<` 的上界。

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
> 现在**只剩 1 项**：`emotion_cpt.structure.a_share_rules` 里的
> `t_plus_one_purchase_allowed`（仍无任何生产调用方）。
> > （原文这里写成 `模块路径::符号`；R66 删掉该符号后，这个写法会让
> > `scripts/check_all_claims.py` 的 S 类断言把**历史叙述**当成现行符号，故拆开写。）
>
> ⚠️ **R66 更正（2026-10-16）：「只剩 1 项」也已经过时 —— 现在是在册 0 项。**
> 最后那一项经 R66 查清为「**CPT 层接不了**」（依赖消失：账户持仓与「当日已买入」在
> emotion-core 的 `position` 表），owner 拍板**删除**：符号 + `tests/test_a_share_rules.py`
> 两个自证式用例 + `whitelist.py` §4b 豁免**三处同步移除**。
> ⇒ `docs/pending-wiring.md` 与 `whitelist.py` §4 均为空，这就是目标态。
>
> ⇒ 本条的**结论仍成立**（剩下的那一项确实是有意不接），但**数量必须改口**。
> 照原文的「16 个」去理解，会以为这批模块整体没动过 —— 而实际上
> 一多半已经接进生产了。**这是「数量过时」比「结论过时」更危险的一类漂移**：
> 结论对，容易让人以为整条都还成立。
>
> 📌 **而 R66 的更正正是同类漂移的第二个实例**：「只剩 1 项」也是数量，也会过时。
> 凡在文档里写「N 个模块 / N 项」这类**计数**，要么每次轮次收尾重数，要么干脆写成
> 指向 `docs/pending-wiring.md` 的**指针**而不写数 —— 数量是唯一必然过期的信息。

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
（见 `emotion_cpt.structure.structure_events::structure_id_of`）—— **不含代码**。
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
        # R96 已删：_try_on_demand_factors / 按需因子链（腾讯 hfq/raw 相除路径）
        # 此处留作历史参考——"在 except 块内部调可能失败的函数"是反模式
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
| `app.py:1394` | `find_run(run_id) or _index_row_from_body(...)` | `find_run` 返回 **dict 或 None**；非空 dict 恒为真，None 才回落 ⇒ 正确 |

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

## 31. 「读失败降级成空」会毒化**共享连接**，一次降级炸穿整张快照（R56 新增）

`structure_event_store.latest_events` 原来把读失败吞掉返回 `{}`。看着无害，实际
两宗罪叠在一起：

1. **PG 语义**：任何一条语句失败都会把整个事务打成 `aborted`。而 recorder 传进来
   的不是它自己开的连接，是 `a_share_snapshot` **共享**的那条。异常不冒泡 ⇒ 没人
   rollback ⇒ 调用方后续十余处查询全部撞 `current transaction is aborted`，
   整轮快照逐只降级。
2. **更隐蔽**：`previous` 变空 ⇒ `diff_states` 把**每一个**结构都当成首次创建，
   一次 DB 抖动就能把事件流整体重写。

**修法不是「改成抛」** —— 那会把「表还没建」（真·无历史）也变成硬失败。正确切分
是**按 PG SQLSTATE 分流**：`42P01`（表不存在）时「无历史」是真的，降级不撒谎；
其它故障不知道历史长什么样，就不产出事件。

**配套坑**：recorder 的无条件 rollback **不能**写成「每次都执行」。热路径常态是
「无变化、不写库、不 commit」，此时共享连接上可能还挂着**调用方自己的**未提交
事务，无差别 rollback 会把别人的工作销毁。正确形态是**失败时无条件**。

**判据一句话**：共享连接归调用方所有，store 层无权 commit 也有无权回滚；谁碰坏
谁负责救，且只在自己真的碰坏时救。

## 32. 「写完再读自己」⇒ 变化检测恒为 False（R56 新增）

`signal_changed` 原来是这样算的：`_attach_signal_change` 自己查一次
`latest_status(signal_id)` 拿「上一轮状态」，和当前状态比。但调用顺序是
「`_derive_*` 写事件**并 commit** → 建快照 → 才到 `_attach_signal_change`」——
查到的必然是**本轮刚写进去的那一条**。于是 `prev_status == cur_status`，
`signal_changed` 恒 `False`，该字段自 R21 起在生产里从未生效过。

**为什么之前没人发现**：R24 修好了它的 SQL 排序（`event_time` 列根本不存在），
从「查库失败降级成没变化」变成「查到自己刚写的也等于没变化」——**修好了一个 bug，
功能仍然是死的**，而两种死法在日志里长得一样。

**修法与配套纪律**：前值必须在**写之前**取，由写入方回传（`(signal, prev_status)`
元组）。不要把 `prev_status` 做成可选 + 缺省回退查库 —— 那条回退路径**就是本 bug
本身**，留着等于给下一个调用方埋同一个坑。拿不到前值就不许填这个字段。

**通用判据**：任何「与上一轮比较」的逻辑，比较基准必须在**本轮副作用之前**捕获。
写在函数里「顺手再查一次」是最容易复现这个错的形状。

## 33. 两个「笔门槛」量纲不同，且 `bar_index` **不是**去包含后的下标（R56 新增）

`RulesConfig` 里有两个容易混的门槛，config.py 自己的 docstring 早就警告过
「量纲不同、不可混用」：

- `min_bi_len` = 底层笔最少跨度，量纲是**去包含后**的 K 线根数
- `min_elements_for_higher_bi` = 高级别笔包含的**低级别结构元素数**

更隐蔽的是：`Fractal.bar_index` 取的是 `source_indices[0]`，那是**原始** K 线下标。
包含关系会合并 K 线 ⇒ **去包含后根数 <= 原始根数**。拿 `bar_index` 当跨度算，
门槛会比参照后端更严，笔数对不上，而这种偏差在图表上看起来完全正常。

**正确做法**：去包含后的下标本来就在 `source_ids` 的 `merged:` 前缀里，但**不要在
domain 层解析字符串**（那是 `fractal.py` 的私有编码格式）。正解是给 `Fractal` 追加
一个带默认值的 `merged_index: int | None` 字段，由 `detect_fractals` 填
`middle_index`（那本来就是去包含后序列里的位置）。**缺失时直接抛错**，绝不
退化成用 `bar_index` 静默量错单位。

## 34. 已跟踪文件**不受** `.gitignore` 约束（R56 新增）

`cpt` 目录下的 egg-info 构建产物目录（`*.egg-info/` 那一族）明明已经写在
`.gitignore` 里，`git ls-files` 却照样列出 5 个文件。`.gitignore` 只对**未跟踪**
文件生效，已进入索引的文件不会因为新增忽略规则而消失。

**症状**：明明加了忽略规则，`git status` 还是有它 / 提交里还是带着它。
**修法**：`git rm -r --cached <path>`（文件留盘，只出索引），必要时
`git rm --cached` 单文件。

**同批另一个**：`pytest-output.log` 是修 mypy 报错时顺手提交的临时输出。Junit 那
两条规则只挡 `*.xml` 挡不住 `.log` —— 忽略规则要**按产物类型**写，别指望前缀能
覆盖到。

## 35. Windows GBK stdout：门禁自检与 chromium 测试在本机直接崩（R56 新增）

`scripts/selftest_gates.py` 打印 ❌（U+274C）到 stdout，Windows 默认 GBK 编码下
直接 `UnicodeEncodeError` 崩在 `main()` 的打印那一行 —— 门禁自检**根本跑不起来**，
看起来像「门禁坏了」。`tests/conftest.py::chromium_runnable` 同类问题
（`result.stdout is None`）。

**修法（本机复现门禁时）**：`PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1`。
CI 上 Linux 无此问题，所以这个坑**只在本地出现**，容易误判成代码坏了。

**更一般的判据**：任何要打印符号（✅ ❌ ⚠️）或 CJK 的脚本，在异构团队里都要显式
钉住 stdout 编码，否则「本地跑不通 / CI 绿」会被当成 flaky 排查半天。

## 36. 测试夹具比实现宽松 ⇒ 实现的缺陷藏得住（R56 新增，本轮遇到三次）

同一类问题在 R56 一轮里出现三次，都是「夹具不忠实于现实，于是缺陷测不出来」：

| 夹具 | 原来的样子 | 藏住了什么 |
|---|---|---|
| `NoTable` | 抛裸 `RuntimeError`，**没有 `sqlstate`** | 新的按 SQLSTATE 分流判据永远走不到「表不存在」分支 |
| `BARS` | `[{"close": 1.0}] * 40`，**没有 `open_time`**、40 根全同 | 时间锚点解析根本无从验证，「每笔都锚在第 0 根」测不出来 |
| `_install_fake_local` 的假 client | **没实现 `close()`** | 连接泄漏在旧代码下也测不出来 |

共同形状：**夹具只提供「被测代码恰好用到的那几个字段」**。于是当实现开始用
新字段（`sqlstate` / `open_time` / `close()`）时，夹具不跟随，实现的缺陷就藏在
「看起来很全」的绿测试后面。

**判据**：改实现去读新字段时，同步问一句「夹具里这个字段存在吗、形态对吗」。
夹具应该按**真实对象的样子**造，不是按当前实现用到的样子造。

## 37. 门禁自检用替身验自己 ⇒ 「能不能红」从未被证明（R56 新增）

`selftest_gates.py` 的两个夹具（`fx_enqueue_unique` / `fx_bundle_sync`）用 `_write`
**覆盖掉了刚复制进来的真门禁脚本**，自测跑的其实是作者手写的 stub。于是这两道门
「能不能红」这件事，**从来没有被验证过** —— 而 `check_gate_coverage` 只比对键名，
结构上发现不了这件事。

**修法**：夹具不要覆盖真脚本；stub 要写进**不同的文件名**。

**验证自检本身有效性的办法（值得推广）**：把真门禁改成**恒返回 0**，重跑自检，
确认它转成「❌ 漏报」；还原后再确认转绿。只看「自检 13/13 通过」是不够的 ——
替身也能 13/13 通过。

**顺带**：自检夹具里 `re.findall` 缺 `re.M` 导致**静默走 fallback** 造出一个假的
2 文件 ORDER。**静默降级比报错更贵**：它让自检看起来在验东西，实际验的是幻觉。

## 38. 覆盖率要按「行覆盖」看，且**只收口最高危的**（R56 从 R45 待办收敛）

整体覆盖率数字回答不了「这些分支测过吗」。R45 的一次真实教训：`run_metric_store`
长期**零测试覆盖**，而它是生产 cron 每天都在跑的模块 —— 靠「grep 测试里有没有出现
这个模块名」去找零覆盖模块，只会得到「这个文件被碰过吗」，碰过不等于测过。

**做法**：装 `pytest-cov` 先跑一次全量拿数据；门禁**只收口最高危的那几个**
（当前状态未知的先列为 fail，不要为了让门禁变绿去硬改）；补测的优先级按
「零覆盖 **且** 生产每天在跑」排，**不是**按模块名字母序。

**为什么值得单列**：覆盖率是**信号**不是**目标**。为一个数字去补测试，
补出来的是给数字看的测试，不是防故障的测试。

## 39. 「系统查不到」与「系统知道没有」必须能被区分（R56 从 R45 待办收敛）

实测场景：17 只港股的 K 线 < 30 根时，结构计算没有意义。但**展示层**和
**系统**必须能区分两种情况：

- 系统**查不到**（数据源故障、网络问题）
- 系统**知道没有**（确实就这么少）

**做法**：在 `recommendation.build_recommendation` 返回的 `data_quality` 位
（K 线根数 / 是否缺件）里**显式写明**「数据不足，无法判断」，而不是静默降级成一个
看起来正常的空结果。这 17 只票在展示层和信号统计里**不能**被混进正常票。

**通用判据**：任何「降级 / 兜底 / 默认值」都会把两种状态压成一种。凡是下游要据此
做决定的场合（告不告警、算不算信号、显不显示），降级就必须**同时把原因带出来**。
这与 #31（读失败不能降级成「无历史」）是同一条原则的两面。

## 40. 快照「陈旧」判定必须挂在唯一汇合点上，不能挂在某条加载路径上（R57 新增）

看板每 5 秒轮询一次快照，`STALE_AFTER_MS = POLL_INTERVAL_MS * 3`（15 秒）内
没有新鲜数据就显示「数据陈旧」。修复前 `startPolling` 只挂在 `loadSnapshot`
的**无缓存分支**上，于是：

- 首屏无缓存 → 启动轮询 → 之后每轮都命中 3 秒 TTL 的缓存并**提前 return**
- 提前 return ⇒ 够不到重新布防的代码 ⇒ `staleTimer` 永不被重置
- 15 秒到点 ⇒ 页面在数据完全新鲜时谎报「数据陈旧」，顶栏「更新时间」也不走

**做法**：把「启动轮询 + 重置 stale 计时器」收敛到 `markFresh()` —— 它是
**首屏无缓存**与**稳态命中缓存**两条加载路径的唯一汇合点。挂在汇合点上，
以后再新增第三条加载路径也不会漏。

**由此推出的两条可断言的不变量**（`tests/test_dashboard_polling_contract.py`）：

- `POLL_INTERVAL_MS < STALE_AFTER_MS`。轮询慢于陈旧阈值时，「持续轮询」在
  算术上就不可能防住误报。
- 缓存分支**自己**的边界内必须有 `markFresh`。注意这条断言的范围：第一版
  写成「从 `if (cached)` 往后的全部正文」，把后面无缓存路径的 `markFresh`
  也算进去了 —— 把缓存分支那行删掉，测试**依然全绿**。这与 #37 是同一条：
  断言范围比被钉住的行为大，测试就只是在**测得少了**。

**验证这类测试有效，只看「6/6 通过」是不够的**（#37 的推广）：逐个把
`markFresh` 里的 `startPolling`、缓存分支的 `markFresh`、
`clearTimeout(state.staleTimer)`、重新布防的 `setTimeout`、以及
`STALE_AFTER_MS` 的取值分别改坏，确认每一种都转红，再还原。

## 41. 运行时诊断计数器**只能是纯整数**，细节另放一个属性（R57 新增）

为排查 #40 临时加了一对 `data-probe-*` 计数器，本来是把计数和细节拼在同一个
属性里：

```js
el.dataset.probeTick = String((Number(el.dataset.probeTick) || 0) + 1) + "#pinned=" + pinned;
```

第一次写入 `"1#pinned=false"`，第二次 `Number("1#pinned=false")` 得到 **NaN**，
`NaN || 0` 退化成 `0`，于是又写回 `"1"` —— **两个计数器恒显 1**。

后果不是「探针不准」，而是**探针把结论带偏**：恒显 1 看起来正是「轮询跑了一次
就停摆」，据此差点再去改一段已经正确的代码。真相是轮询一直正常（服务端日志里
每 5~6 秒一次、连续不断），只是探针在说谎。

**判据**：任何「读回上次值再累加」的探针属性，值域必须是 `Number` 能完整解析的
纯数字；`#` 后面挂什么都行，但**不能挂在同一个属性上**。

**顺带两条**（同一次排查踩到的）：

- **静默 `.catch(() => undefined)` 会把故障伪装成「没动静」**。后台刷新失败
  应当落到 `setConnection("error", ...)` + `showError(...)`，让人看得见。
  `loadSnapshot` 内部已经吞掉异常并返回 `null`，所以它上面的 `.catch` 基本
  不会触发 —— 写了也测不出来。
- **「页面一直转圈」优先怀疑**观测手段本身（缓存、`Cache-Control`、HTTP/1.0
  无 keep-alive 导致的连接排队、后台标签页被冻结），**再怀疑业务代码**。
  本次先加了服务端请求日志才拿到决定性证据：在日志里看到请求按秒稳定出现，
  才排除了「业务代码停摆」这个假设。

## 42. 子串断言必须先确定「这段文本代表什么」（R57 内连续踩三次，已成模式）

**同一个错误，一天之内犯了三次**，而且三次都是「测试全绿但测得少了」——
不报错、CI 通过、只是没测到想测的东西。

| # | 断言写成 | 实际匹配到 | 变异后是否变红 |
|---|---|---|---|
| ① | `body[body.index("if (cached)"):]` 含 `markFresh(` | 后面**无缓存路径**的 `markFresh` | ❌ 不红 |
| ② | `"!!!!! 清理未完成 !!!!!" in script` | 整行原文（改长提示语就误红） | 反向失灵 |
| ③ | `"dashboard-run-prune-daily.sh" in crontab` | 文件头**文档表格**里的同名引用 | ❌ 不红 |

③ 尤其刺眼：把 `30 4 * * *` 那条调度行删掉，13 个测试**全部照绿**——
因为同一串字符在文档表格里合法地出现了。

**判据**：子串断言之前先回答一个问题——**「我要钉住的是不是这一处出现？」**

- 同一串字符可能合法出现在多处（代码 vs 注释 vs 文档表格）⇒ 先**裁剪**
  再断言（滤注释行、限定函数体、限定分支边界）；
- 或者断言**更强的形态**（调度行首字段必须是分钟数，而不是「文件里出现过这个名字」）。

**配套纪律**（#37 已立，此处再次验证有效）：只看「N/N 通过」不足以证明测试有效，
必须逐个变异回退——本次 ③ 就是在变异 `M4` 下**全绿**才发现的。
若没有变异这一步，这三条断言会一直「保护」着它们其实没保护的东西。

顺带一条：`deploy/cron/crontab` 文件头自己就写明了核对口径是
`grep -vE '^\s*(#|$)'`（滤掉注释与空行再 diff）。**测试里要复刻这个口径，
不要直接对文件原文做子串匹配** —— 仓库里已经写着答案了。

## 43. 文档/注释在**描述**一件它没描述的事（与 #42 互为镜像）

2026-10-06 上线前审计在**四个互不相干的地方**撞见同一个病：文档或注释声称
A，代码做的是 B，而**没有任何门禁会红**。

| 处 | 文档/注释说 | 代码实际 |
|---|---|---|
| `docs/calibration-r56-min-bi-len.md` §2 | 门槛 6 ≈ **p80 / 砍掉最短两成** | 实为 **p95 / 砍掉约 65%** —— 而且**同一节的表格自己写着 65.2%** |
| `cpt/adapters/backend_factory.py` | 「**native 后端忽略本参数**……不接受该门槛」 | `return NativeChanlunBackend(min_bi_len=min_bi_len)`，**真的透传了** |
| 同上 | — | `native_chanlun.py` 自己写着「backend_factory 的 docstring 当时写的是『native 不参与（见后文）』，**后文并没有实现，属于声明超前**」——**这句自白留了整整一天没人改** |
| 校准 md 文件头 | 「文档由脚本从 json 生成，不手写」 | 仓里**没有任何生成该 md 的脚本**；json 才是自动产物 |
| `deploy/README.md` | 「两份脚本开头都有 `set -a; . $ENV_FILE`」 | 全仓 `set -a` 只有**一份**（巡检）⇒ 把 `CPT_PRUNE_*` 写进 env 是**无声无效**的 |

### 为什么门禁全都看不见

| 门禁 | 它验什么 | 抓不到什么 |
|---|---|---|
| `check_all_claims.py` | 路径/符号/路由/表名/键的**存在性** | 值对不对、数量对不对 |
| `check_doc_drift.py` | 「字段名在 rules.md 出现过」+ §2.1 计数 | 字段的**值** |
| `check_doc_counts.py` | §2.1 表 | 散落各处的「N 个文件 / M 行」**只打印不判** |

⇒ 共同盲区是一句话：**名字在 ≠ 值对**。

### 为什么它比真 bug 更危险

- **真 bug 会让测试红**；撒谎的文档**不留任何痕迹**，CI 全绿。
- 危害不是「代码跑错」，而是**让人做出反向判断**：`resolve_backend` 那条会让
  任何人照着它得出「生产门槛根本没生效」，进而怀疑整套 `min_bi_len` 定档工作
  —— 而它明明生效着。
- 更糟的是**文档和代码同时错会互相印证**：消费方照着一份「v1 已冻结」的文档
  写校验，文档少写一个字段，校验就少查一个字段，两边一起错且看起来一致。

### 与 #42 的关系（两者的区别要记清）

- **#42：尺子不准** —— 断言范围比被钉住的行为大（测得太松）。
- **#43：尺子对准的东西本身歪了** —— 依据（文档）不可信，于是断言「正确地」
  验证了一份错误的依据。

两者都不是「没写测试」，而是**验证的前提错了**，所以加门禁不解决，只有改依据。

### 怎么防（都是今天付出代价换来的）

1. **否定式能力声明一律复核**：docstring 里出现「忽略 / 不参与 / 不接受 /
   由 X 决定」这类说法时，去代码里确认它**真的**如此 —— R56 那句「native
   不参与」就是这种声明超前的样板。
2. **同一个数字在文档里出现两次就要自查是否同源**：正文说「两成」、表格写
   65.2%，这种矛盾**肉眼扫一遍就能发现**，但没人扫 —— 因为两份内容挨得很近、
   读起来像重复。
3. **写「由脚本生成」这类流程声明前先 grep 那个脚本在不在**。
4. **改代码行为时，同一个 commit 里搜一遍提到该行为的文档**。
5. 写「甲不做 Y」时，同步检查「乙到底做不做 Y」的文档 —— 今天那四处里有三处
   是「甲文档说做了/没做，乙文档说相反」。
## 44. 产物只拼「首模块的头」⇒ 别的模块头里的声明被静默丢弃（R62 汇总 R61 实测）

- 现场：`dashboard/dash-chrome.js` 的 `// <<<HEAD` 段里加了
  `const SOURCE_LABELS = {...}`，同一文件的 `renderChrome()` 用到它。
- `scripts/build_dashboard_bundle.py` 的 `build()` **只取 `ORDER[0]`
  （`dash-core.js`）的 HEAD 段**，其它模块的头整段丢弃 —— 原实现是 `pass`，
  一声不响。
- 后果：bundle 里没有该声明 ⇒ `ReferenceError` 在 `renderChrome` 中途抛出
  ⇒ 被调用方的 `try/catch` 吞掉 ⇒ **页面正常、console 零报错、
  `window.onerror` 空**，只是其后所有 DOM 写入（当日高/低、成交量、
  实时价标签）全都不执行。
- 定位手法（值得复用）：往 bundle 里插 `window.__M.push("pre-dual")` 这类
  路标 + `window.__RC_CALLS++`，看到「只跑了 1 次、过 `pre-dual` 就没了」
  才敢断定异常被吞 —— 光看 `ERRS=[]` 会得出「没问题」的反向结论。
- 防：共享声明一律加在 `dash-core.js` 的头；`check_quarantined_head()` 在
  构建期 `SystemExit` 硬失败；`tests/test_build_dashboard_bundle.py` 钉住
  该不变量；`tests/test_dashboard_ashare_r61_render.py` 用**真 DOM** 断言
  最终文本。
- ⚠️ 关键教训：单测读的是**源文件拼接**（`tests/conftest.py` 的
  `dashboard_js()`），它看得见那个声明 ⇒ **只跑它永远发现不了这一类**。
  丢失发生在构建，只有产物（或真浏览器）才看得见。
- ✅ **R77 已从结构上消除**：6 个非权威模块的 `<<<HEAD` 段全部清空，只留
  `dash-core.js` 一份。宽容版的 `check_head_declarations()`（只在「多出声明」
  时才失败）换成 `check_quarantined_head()`（**非空即失败**，剥注释后判空）。
  R77 删副本时 `dashboard.bundle.js` **逐字节未变** —— 反证那 6 份副本
  从未进过产物。同族事故还发生过两次：R71 改 `SIGNAL_TYPE_LABELS` 只改了
  权威表、R76 才补齐副本，期间页面一直显示「一买信号」—— 因为渲染走的是
  **副本里的那个绑定**。

## 45. 只守分母、不守分子：盘前 `last=0.0` ⇒ 涨跌幅 −100%（R61 实测）

- 2026-10-14 09:23（北京，开盘前）新浪返回 `last=0.0`（哨兵值），
  `prev_close=1274.89`。
- `_pct_from` 原判据只有 `if prev <= 0`：分母守住了，分子是 0 照样算出
  `(0-1274.89)/1274.89*100 = -100.0%` ⇒ 面板显示一个「跌停」。
- 防：`_is_tradable_price(v) -> float(v) > 0` 同时守分子，命中则
  `reason="realtime_no_trade"` 走降级，**不给一个看起来像真的错数**。
- 推广：凡行情/计数参与运算，**每个输入**都要有域校验。除零才会炸，
  而 0 不会炸 —— 它只是安静地给出一个错答案，这正是它比崩溃更危险的地方。

## 46. `formatNumber` 内部是 `Math.abs` ⇒ 负号必须调用方自己拼

- `dashboard/dash-core.js` 的 `formatNumber(value, digits)` 实现是
  `Math.abs(value).toFixed(digits)`（外加千分位）。
- 于是 `formatPrice(-3.12) === "3.12"`：跌 3.12 元显示成涨 3.12 元。
  返回值**看起来完全正常**，页面无报错，测试也不会红。
- 当时全站只有 `dual-divergence` 一处自己拼了符号
  （`div >= 0 ? "+" : ""`），其它调用点都没有 ⇒ 每新加一个「带方向」的量
  就必然踩一次。
- 防：`scripts/check_value_plumbing.py` 的 R3 判据 —— 「带方向词
  （change/pct/divergence/delta…）的 `format*` 调用 + 同行没有符号拼装」
  一律 error。

## 47. 判 `=== null` 不等于判类型：键**缺失**是 `undefined`（R61 复查发现，note 级）

- 典型写法：`const div = dc.divergence_pct;` 之后
  `div === null ? "—" : div.toFixed(2)`。上游一旦**漏写该键**，走的是
  `undefined.toFixed()` ⇒ TypeError ⇒ 又回到 #44 的「被 catch 吞掉」
  模式（那一行永远显示旧值，且没有任何报错）。
- 现状不可达：唯一生产者（`cpt/application/a_share_snapshot.py` 的
  `_attach_dual_compare`）在同一 `available: True` 分支内必写该键。
  但它与 #44/#45 同族 —— **降级路径只在一种缺失形态下成立**。
- 防：判型（`Number.isFinite`）而不是判 null。R4 判据只把这类记成 note
  （不阻断 CI），且只认**直接从对象属性读出来的局部变量**：函数形参的契约
  由调用方给，`num(x.y)` / 三元赋值的结果只可能是 null 或数字
  （三者都不该报）—— 不收窄到这一步，这条规则就是满屏噪声。

### 本轮的对策（R62）

`scripts/check_value_plumbing.py` 把上面四类收成 R1-R4 四条判据，
发现写进 `docs/audit/probe-findings.log`（故意用 `.log` 不用 `.md`：
`check_all_claims` / `check_doc_counts` 会 `rglob("*.md")`，生成物里的路径
与行号会被当文档断言去验）；CI 门禁⑮ 跑它，`selftest_gates.py` 配了
`check_value_plumbing.py` 与 `check_value_plumbing.py#R4` 两个已知错例夹具，
`tests/test_check_value_plumbing.py` 管判据边界，`check_test_mutation.py`
登记了一条「把 R1 的守卫判据改成恒真」的变异。

## 48. 告警判据「有问题就发」在问题**修不掉**时会退化成噪音源（R38 只收紧了一半，R63 补齐）

R38 把「常态降级」从告警里摘掉了（常态降级占日志 41%，把真信号淹了），但判据留了
一半：`changed or bool(problems)` —— 只要**当前**有问题就发。当时问题项少、修得快，
看起来没问题。

A 股接进巡检（R62）之后这句话的含义变了：那批复权因子断供的标的在链路修好之前一直
都在 `problems` 里 ⇒ 每天推一条**一模一样**的卡片。天天响等于不响：人会开始无视整个
通道，连「新变坏的那条」一起无视掉 —— 与 R38 的「常态降级淹信号」是同一个病，只是
换了个位置。

- 判据改成**只看状态指纹** `_state_key`（全量 problems + degraded + 数据源）；
- 首次巡检发现问题**仍然要发** ——「没有基线」不等于「没有故障」；
- 另一半是**旧账**：A 股水位行加**行龄闸门**（`ASHARE_ROW_MAX_AGE_DAYS = 4`；
  水位表累计 154 只、批次每天只测 50 只 ⇒ 行龄 ≤3 天是正常轮换，超过就是被落下的
  历史快照），同时必须有**流整体停摆**的全局检查（`ASHARE_STREAM_STALL_HOURS = 36`）
  —— 否则逐标的降级会在批处理整体死掉时把告警弄哑，而那正是最该响的时候。
- 三条判据各配变异（`scripts/check_test_mutation.py`），因为它们的失效方式都是
  「巡检与门禁照样全绿，只是行为悄悄退回去了」。

**同一轮触发真告警时现场又发现一条**：第二轨（结构计数突变 / 指纹变化）原来对所有
标的都做「相邻两行相减」，而 A 股的水位行是**不同日期的快照**（一天一批、每批 50 只）
⇒ 相减必然产出几百条「笔数 56 → 33」「dataset_hash 变了」（实测 189 / 433，全部来自
cn）。它们进 `degraded` ⇒ 进 `_state_key` ⇒ **状态永远"有变化"**，刚收紧的"只在状态
变化时发"会被这两行噪音架空。现在第二轨只比加密（30s 一轮、相邻行是连续观测，
R36 装机换后端就是靠它发现的）；A 股的对应诊断在 staleness 那条线上。
教训：**加了新数据源之后，要回头检查那些"按标的比较历史行"的判据在新源的采样
节奏下还成立不成立** —— 采样间隔从 30 秒变成 1 天，同一个判据就从"查突变"变成"查日历"。

## 49. `daily_bar` 有行 ≠ K 线画得出来：复权因子才是咽喉，"重算"与"上线"是两件事（R64）

2026-10-15 的现象：A 股快照里 600519 的「最新价」停在 10-13，而 `public.daily_bar`
当天（10-15）有 5225 行、全市场 5224 只都在。瓶颈不在行情入库，在
`asel.ref_adjust_factor`：K 线循环对每一天 `factors.get(d)`，取不到就
`skipped.append(d); continue` —— **没有因子的那天直接不画**，末根于是停在最后一个
有因子的日子。所以「A 股 K 线停更」要先查因子表，不是先查行情表。

1. **中间缺口与尾部缺口后果不同**：中间某天缺因子只少一根（相邻两根各自带自己的因子，
   不会跳）；**尾根**缺因子会让整条序列停在过去 —— 而「今天多少钱」恰恰是用户唯一要的。
   R64 的兜底因此只治尾部：末根缺因子 ⇒ **整段**按不复权画（因子取 1.0）并把口径标成
   `raw`。绝不能只把尾巴补成不复权：同一张图上前后段口径不同就是 6.39 倍的假跳空
   （600519 的因子是 6.39），看起来像暴跌 84%。
2. **「重算好了」和「上线了」是两件事**：`asel.ref_adjust_factor_v2` 里 5206 只、到
   10-13 的正确因子一直躺着，生产表没人搬（只有 ~~factor_report.py~~ (已删) 读它做对账）。
   一句「切不切换看报告」等于**永远不会切**。修法是 cron 直接 `--promote`：
   `ON CONFLICT` 幂等 + 1e-8 容差只改真有差异的行（实测首轮 93,073 行）。
3. **跨类型比较「看着一样」会骗人**：v2 是 `numeric`、生产表是 `double precision`，
   同一个 Python float 转回来是 `12.4293652541882` vs `12.4293652541882001…` ⇒
   `IS DISTINCT FROM` 报 2,248,057 行差异、容差 1e-9 也有 1,806,097 行，而真实修正只有
   1e-8 那一簇 92,924 行。**比较数值要带容差，不要比字符串。**

## 50. 改了 `dash-*.js` 必须先重建 bundle，再跑渲染测试 / 再看页面（R64）

`tests/test_dashboard_ashare_r61_render.py` 的渲染台把真实 `dashboard/` 目录软链进去，
页面加载的是 **`dashboard.bundle.js`**（构建产物），不是源文件。只改
`dashboard/dash-chrome.js` 就跑渲染测试，测到的是**上一次构建**的行为 —— R64 第一次
跑「不复权兜底」那条断言时正是如此：面板还印着「后复权（因子未知）」，因为 bundle 里
还是旧逻辑，而字符串契约测试（读源文件拼接）却是绿的。
固定顺序：改 `dash-*.js` → `python scripts/build_dashboard_bundle.py` → 渲染测试 →
`deploy/dashboard-sync.sh`。`tests/test_build_dashboard_bundle.py` 只保证「仓内 bundle 与
源一致」，不会替你构建。

## 51. 「推不出去」不报错：告警链路的四种静默（R65）

一次真实漏报（用户收到 600519 的告警、却没收到自己在跟的 000002）把整条链路查了
一遍，四种静默同时存在，**每一种单看都像「今天没信号」**：

1. **没配凭据 ⇒ INFO**：`cpt/adapters/feishu.py` 在 `CPT_FEISHU_WEBHOOK` 为空时
   `return False` 并记一条 **INFO**。INFO 在生产 journal 里等于没有 —— 148 条
   「未配置」横跨 6 天，没有任何人看见。**未送达是事故、不是噪声，必须 WARNING**
   （R65 已改，含 `signal_notify.maybe_notify` 那条「推送未送达」；级别用 `caplog`
   钉住，不靠人记）。
2. **告警能力跟着进程环境走**：同一条 `build_ashare_snapshot` 路径，web 服务
   （`cpt-dashboard.service`）推得出去、每日批处理
   （`cpt-dashboard-ashare.service`）推不出去 —— 差的只是后者没引
   `EnvironmentFile=`。⇒ **新增任何入口（timer / batch / 新 unit）都要先问一句：
   它能不能发告警？** 而且旧 unit 的注释会替这个缺口辩护（原话是「不引是为了避免
   隐式耦合」）—— **注释里写下的理由会过期**，改 unit 时要重读它是否还成立。
3. **cron 的真相源不是仓内文件**：`deploy/cron/crontab` 里有那行 ≠ 机器上装了那行。
   实测 `watchlist-daily-summary` 只在（已被清掉的）root crontab 里装过，ubuntu 的
   crontab 里从来没有它 ⇒ 作业无人执行，而日志文件是上次被 root 跑出来的残留。
   **核对口径：`crontab -u <user> -l` 逐行比，不看仓内文件。** 另外**不能整份重装**：
   本机 ubuntu 的 crontab 有仓内没有的 3 条（jc-ingest / ec-dash-watchdog /
   backup-db），`crontab deploy/cron/crontab` 会一并抹掉，只能追加行。
4. **闸门是 code 级、跨用户**：`track_store.is_tracked(conn, code)` 只问「**有人**
   关注它吗」，不区分 user_id ⇒ 一条 `X-CPT-User: smoke_*` 打真库留下的测试行，
   足以把告警推给**全局**飞书群，而且日志里看起来完全正常。测试数据与生产数据
   同表这件事本身没有任何隔离（R65 清掉 8 条 smoke/ui 行 + 8 条孤儿快照）。
   > **R66 已修（2026-10-16）**：`is_tracked(conn, code, users=None)` 新增可选的 user
   > 白名单；`cpt/application/signal_notify.py` 的推送闸门改为传
   > `_notify_users()` = `parse_notify_users(os.environ.get("CPT_NOTIFY_USERS"))`，
   > **每次调用重读 env、不缓存**。`users=None` 保留 R57 旧口径（任一用户）供通用查询用。
   > 新环境变量 `CPT_NOTIFY_USERS` 默认 `default`，写进
   > `deploy/env/cpt-dashboard.env.example`（生产 env 未改 —— 默认值等价于旧行为）。
   > **`parse_notify_users` 在 None/空/全空白时回落默认，绝不返回空元组**：空元组 =
   > 谁都不认 = 推送永久静默，正是本条第 1 种静默的翻版。
   > ⚠️ **仍未解决的部分**：`cpt_track` 依然是**生产/测试同表**，表结构没动 ——
   > 现在只是「测试行不再影响推送范围」，不是「测试行写不进来」。彻底解法要加来源列
   > 或把测试指向独立 schema（表结构变更，见 `docs/progress-log.md` §9.12）。
   > 回归：`tests/test_track.py::test_is_tracked_scopes_to_allowlist`（真库）+
   > `tests/test_signal_notify.py::test_notify_users_defaults_to_default_allowlist`
   > / `test_notify_users_env_overrides_allowlist`。

配套事实：`cpt_track` 现在只剩 `default/000002` 一行。`is_tracked` 的跨用户口径
~~**本轮未改**（改它会动推送语义），属已知设计缺陷。~~ **R66 已改**（见上，白名单收口）；
剩下的「生产/测试同表」是**表结构**层面的缺陷，仍未解。

---

## 52. 跨仓端点漂移：单侧单测把「自己拼出来的 URL」当成契约钉住（R66 实测）

**现象**：emotion-core 的「关注 / 取关 / 复活」自 S1（`d712ae0`，2026-10-14）交付起
**从未成功过** —— 它每次都 POST `/api/dashboard/track`（裸前缀）+ body `{"action":"add"}`，
而 CPT 只认 `/api/dashboard/track/{add,remove,restore}` 三条**字面量**、**从不读 `action`**，
线上稳定返回 **405**。两侧测试全绿、CI 门禁全绿，UI 上只表现为「加不进去」。

**判定命令（3 秒复现）**：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8010/api/dashboard/track \
     -H 'Content-Type: application/json' -d '{"action":"add","code":"000002"}'      # -> 405
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8010/api/dashboard/track/add \
     -H 'Content-Type: application/json' -d '{}'                                    # -> 400 code required（路径对了）
```

**为什么全绿**：

1. **两侧各自自洽**。emotion 的 `tests/unit/test_cpt_client.py` 用 `monkeypatch` 拦掉
   `urlopen`，断言的是「**实现的 URL 拼得对不对**」—— 等于把自己拼的串当成了契约；
   CPT 的单测只测自己的 `route_for` / `_handle_track_post`。**没有任何一侧去读对方仓里的
   分发字面量。**
2. **失败被降级成语义模糊的 `False`**：emotion 侧那个 CPT 客户端的 `_post_json` 捕获
   `urllib.error.HTTPError` 后返回 `None` ⇒ `track_add` 返回 `False` ⇒
   `watchlist_service` 只说「CPT 不可达：track_add 返回失败」，**看不出是路径错了**。
3. **路由风格撞车**：CPT 其它模块确实大量使用「POST 裸路径 + `{"action": …}`」+ `startswith`
   分发，于是「裸前缀 + action」看着完全合理 —— 但**唯独 track 是三条字面量**，
   只有读 `cpt/web/app.py::_handle_track_post` 才知道。

**修法（R66 已做）**：emotion 侧加常量 `TRACK_ADD_PATH` / `TRACK_REMOVE_PATH` /
`TRACK_RESTORE_PATH`，并加回归门禁
`tests/unit/test_cpt_client.py::TestTrackWritePathsMatchCptLive` —— 从 CPT
`cpt/web/app.py::_handle_track_post` 的**函数体**里
`re.findall(r'path == "([^"]+)"', body)` 抽字面量，断言本仓要用的三条路径 ⊆ 该集合。
**双向反向控制已验**：本仓改回裸路径会红；CPT 把 `/remove` 改成别的字面量也会红。

⚠️ 该门禁的**第一版是错的**：写的是 `f'"{path}"' in app_py`（「这个串在文件里出现过」），
反向控制时**被 bug 放过去了** —— 裸前缀 `/api/dashboard/track` 在 `do_GET` 的
`startswith("/api/dashboard/track")` 里也出现。教训与 **#42** 同源：
**先确定你把哪段文本当成了什么，再断言它存在。**

**通用判据**：跨仓 / 跨服务型单测，断言对象必须是**对方仓里的字面量或契约文件**，
不能是本仓拼出来的串；同仓单测用替身拦 IO **最多只能证明自己自洽**。

---

## 53. 进度账本按「对象全集」记账 ⇒ 每日任务空转，而所有检查都是绿的（R67 实测）

**现象**：`scripts/factor_recompute.py` 的日跑 cron 自 2026-10-15 起变成**空转** ——
因子表 `asel.ref_adjust_factor` 的**批次覆盖日冻结在 10-15**，全市场 5208 只静默退到
不复权价（快照里 `price_basis=raw` / `stale_reason=factor_missing`），面板上看不出异常。
当时 `pytest`、15 道门禁、巡检**全部是绿的**。

**根因**：进度账本记的是「算过的**股票集合**」：

```python
todo = [c for c in priority_codes(conn, scope=args.scope) if c not in done]
...
state.setdefault("done", []).append(code)   # 成功一只，加一只
```

只有**换数据源**或**换 scope** 才重置 `done`，**新增一个交易日不会**。R64 把 scope
从占位换成 `all` 触发过一次重置，那一轮把 5208 只全部跑完并标成 `done` —— 从此
`todo` 永远只剩那 18 只一直失败的票。**任务的输入每天都在变（多了一个交易日），
账本却把「这只票做过没有」当成了「这只票到今天为止做完了没有」。**

**判定命令（约 30 秒，只读）**：

```bash
python -c "import json,pathlib; s=json.loads(pathlib.Path('~/.cache/cpt/factor_recompute_state.json').expanduser().read_text()); print(len(s['done']), s['days'])"
psql -c "SELECT trade_date, count(DISTINCT code) FROM asel.ref_adjust_factor GROUP BY 1 ORDER BY 1 DESC LIMIT 3"
# 2026-10-17 现场：5208 {'2026-10-15': 5246, '2026-10-16': 18, '2026-10-17': 18}
#                  最新批次日 2026-10-15=5208 只，而 10-16 只有 18 只（全是按需补的）
```

**为什么全绿**（三条叠加，缺一条都会有人发现）：

1. 既有检查**全是「单只标的」视角**（水位行、`stale`、K 线行龄），没有一条回答
   「因子表**整体**有没有跟着日历往前走」。
2. 唯一沾边的 `max(trade_date)` 会被**按需补因子**（热池 50 ∪ 自选，每天都在写少量
   **当前**交易日的行）顶到最新 ⇒ 滞后算出来是 0，是**假新鲜**。
   2026-10-17 现场就是这样：`max`=10-16 而**只有 2 行**。
3. 账本语义里「跑过了」与「跑到了今天」长得一模一样，代码读起来完全自洽。

**修法（R67 已做）**：账本改成**按交易日记账** —— `covered: dict[code, trade_date]`，
`coverage_target(conn)` 取 `public.daily_bar` 的 `max(date)`，候选取
`pending_codes(priority_groups(...), covered, target)`（`covered[code] == target` 才跳过），
成功时 `covered[code] = target`；老的 `done` 键**首次运行丢弃并告警**（迫使全量重算一轮）。
配套新增**复发探测**：`scripts/run_inspection.py::_factor_lag` 只认「当日覆盖 ≥
`FACTOR_BATCH_MIN_CODES = 500` 只」的**批次级**交易日（这正是为了防第 2 条的假新鲜），
停摆写进 `problems`。

**这条告警的指纹纪律**：正文**只放停摆时冻结的批次覆盖日**，绝不写 K 线日与滞后天数
—— 那两个量在停摆期间**每天都在长**，写进 `problems` 等于把状态指纹变成每日一变，
一天一条「还是坏的」。这与 **#49** 是同一个坑。

**通用判据**：凡是「按对象逐个处理」的批处理，账本必须记**「处理到哪个输入窗口」**
（水位 / 日期 / 版本），而不是「哪些对象处理过」—— 后者的语义只在对象集合不变时成立，
而**输入窗口每天都变**。同类必查：任何带 `done` / `processed` / `seen` / `synced`
集合的回填、同步、迁移、缓存预热任务。

---

## 54. 交叉核对脚本量的是**另一个口径**，且不一致不进退出码（R67 实测）

**现象**：`scripts/compare_chanlun_backends.py`（仓内**唯一**做 native↔czsc 交叉核对的
地方）报 `native 100/99/18`，而**生产同一只票同一批 K 线是 100/28/4**；脚本还打印
`各后端计数一致: ❌ 存在差异` 却 **`return 0`**。

**根因（三层，各自独立危害）**：

1. **后端是无参构造的**，而笔门槛是**构造参数**：

   ```python
   # cpt/adapters/native_chanlun.py
   def __init__(self, *, min_bi_len: int | None = None) -> None
   # cpt/application/a_share_snapshot.py —— 生产在**调用点**传
   resolve_backend(DEFAULT_BACKEND, min_bi_len=RulesConfig().min_bi_len_for("1d"))  # == 6
   ```
   ⇒ 脚本量到的是「**没门槛的** native」：600519/300 根 **99 笔** vs 生产 **28 笔**（3.5×）。
   两个不同口径互证，结论无论正负都没有意义。
2. **计数不一致不进退出码**：只打印 `❌` 仍 `return 0` ⇒ 在脚本/门禁调用链里永远"通过"。
3. **仓内测试占位算进一致性判定**：`InMemoryChanlunBackend`（按 bar 高低点做极简判定）
   永远不等价于真后端 ⇒ 脚本**永远**报 ❌ ⇒ 报红变成背景噪音，没人再看它。

**口径对齐后的真结论**（`--min-bi-len 6`，4 只 × 300 根）：

```
native 笔 28/30/32/27    czsc 笔 15/24/22/26
```

两个**真**后端在同一名义门槛下系统性不一致 ⇒ `DEFAULT_BACKEND` 硬编码 `native`
（不许回到 `auto` 静默切换）这条**不能松**。这不是脚本的 bug，是脚本终于**量对了**。

**修法（R67 已做）**：门槛统一从 `RulesConfig` 取（`--min-bi-len` 可覆盖，`0` = 关闭
门槛**只用于复现历史数字**）；`PLACEHOLDER_LABEL` 排除在判定外；不一致写进 `problems`
⇒ `return 1`。回归 ~~test_compare_chanlun_backends_script.py~~ (已删)（9 例，含"门槛必须
等于生产那份配置"与"不一致必须进退出码"）。

**通用判据**：拿另一个实现做交叉验证之前，**先证明两个实现收到的是同一份输入** ——
参数写在**构造函数**里还是**调用点**，是两回事；核对脚本必须自己进退出码，否则它只是
一份日志。同类必查：任何 `scripts/compare_*` / `scripts/verify_*` / `check_*`。

---

## 55. 进程内锁当跨进程锁 + **固定**临时文件名（R67 实测）

**现象**：6 进程 × 40 次 `handle_trade_results_post`（`CPT_TRADE_DIR` 共享），
240 个幂等键只剩 **129**（丢 111）、3 次 `503 trade state unavailable`、约 30 条

```
processed 落盘失败（结果已存，幂等会失效）: [Errno 2] No such file or directory:
  '/tmp/cpt-trade-race-.../state.json.tmp' -> '/tmp/cpt-trade-race-.../state.json'
决策状态文件损坏: /tmp/cpt-trade-race-.../state.json
json.decoder.JSONDecodeError: Extra data: line 68 column 2 (char 901)
```

**根因**：`_state_lock = threading.Lock()` 只挡得住**同进程的线程**，而 `state.json`
是**磁盘上**的共享状态；`_save_state` 又写**固定**临时名 `state.json.tmp` 再
`os.replace`：

- 两个进程同时 `write_text` 同一个名字 ⇒ 后者的内容被**追加** ⇒ 两段 JSON（`Extra data`）；
- `os.replace` 的源文件可能已被对方移走 ⇒ `ENOENT`，回执落盘失败、**幂等键丢失**（重复下单）。

注意 `_prune_processed` 的 docstring 早就写对了：「裁剪**必须在锁内、且与 `_save_state`
同一次写**完成，否则并发回执会用陈旧副本把别的 batch 盖回去」—— **文档写对了，实现只做了一半**。

**为什么生产没炸**：`cpt-dashboard.service` 只有一个 ExecStart，`_BoundedThreadingHTTPServer`
只多线程 ⇒ 单实例下线程锁够用，属**潜在**故障。判严重度**先看拓扑**，别只看代码。

**修法（R67 已做）**：`_state_guard()` = `threading.Lock` + `fcntl.flock(LOCK_EX)`
（锁文件 `CPT_TRADE_DIR/.state.lock`），读-改-写全程持锁；`_save_state` 临时名改成
`.state.json.{pid}.{uuid8}.tmp` 且失败即清理。修后同一压力：**240/240、0 错误、0 残留**。

**⚠️ 写复现脚本时踩到的坑（值得单独记）**：Python **3.14 起 Linux 的 multiprocessing
默认 start method 从 `fork` 换成 `forkserver`**，子进程会**重新 import `__main__`** ——
我第一版复现脚本在模块级算 `D = f"/tmp/...-{os.getpid()}"`，于是**每个子进程各写各的目录**，
跑出来"0 错误、文件可解析"，其实**根本没有竞态**（假绿）。复现脚本必须：
① 共享目录**显式传参**；② 显式 `mp.get_context("fork")`；③ 对照组要能证明它**真的在竞争**
（这次靠"修复前丢 111 个键"反向确认）。

**通用判据**：共享状态只要落在磁盘 / DB / 管道，「进程内锁」就不构成保护；**临时文件名
固定 = 并发写入的必修 bug**。同类必查：任何 `state.json` / `*.tmp` / 缓存文件 /
`os.replace` 写入。并发验证的正确姿势是**多进程 + 断言"一个都不许丢"**，不是"跑完没报错"。

---

## 56. 门槛路径的端点折叠**不重裁极值**，而它引出的「修好它」代价是笔数腰斩（P2-12 / R67 实测）

**现象**：`emotion_cpt.structure.bi` 的 `_build_bis_gated` 在 `end_idx += 2` 时把与起点**同类**的
锚点夹进窗口并跳过，但从不重新裁定极值 ⇒ 笔端点可以**不是**该笔窗口内同类的极值点。

| gate | 笔 | 中枢 | 端点非极值（起/末） |
|---|---|---|---|
| `None` | 326 | 33 | **0 / 0** |
| 6（生产） | 98 | 21 | **53 / 49** |

**判定命令**：

```bash
.venv/bin/python -m scripts.audit_p2_12_evidence   # 扫三 fixture，自带合成自审
```

**为什么 `gate=None` 是干净的**：`anchors` 折叠（`bi.py:144-146`）只在**输入相邻**的同类
分型间比较；一旦 `anchors` 严格交替，两个同类端点间必隔一个异类端点，**再也不会被比较**。
`end_idx += 2` 让它们重新相邻，却不重新裁定 —— 缺陷**完全由门槛路径引入**，与折叠无关。

**为什么没修**：修好它的代价被 **DP 证明**过。在 native 自己的分型集上求「满足端点极值 + 跨度≥gate」
的最长合法端点链，gate=6 时为 **41/53/38**（现行 98/104/110），即**砍掉一半以上笔数**；
且唯一能归零的路径是**全局 DP 选择**，与 `bi.py:127` 承诺的「合并端点、不丢弃」**语义冲突**
（局部合并 → 全局选择），落地要升 `SCHEMA_VERSION` 到 v2 并重建全部 fixture。
报告 §4 建议的两个修法实测**都失败**：①「start 前移到 end-1」只把违规降到 38/49/54（一个都没归零），
且破坏端点连续；②「high/low 取区间极值」**端点完全不动**、违规原样，还把中枢从 21 压成 5。
完整证据：[docs/audit-p2-12-endpoint-extreme.md](audit-p2-12-endpoint-extreme.md)。

**⚠️ 量测本缺陷必须避开两个坑（都制造过互相矛盾的数字）**：

1. **端点映射必须 start↔start、end↔end**。`Bi.start_time` 取起点分型的 `start_time`，
   但 `Bi.end_time` 取终点分型的 **`end_time`**（`_make_bi`）。用 `f.start_time == b.end_time`
   找终点**恒不匹配** ⇒ 末端检查被**静默跳过**，会得出「末违规恒为 0」的假结论。
2. **跨度必须按 `merged_index` 差算**，不是分型列表下标差。门槛的量纲是「去包含后 K 线根数」，
   分型在合并序列里稀疏 —— 实测 `2024-02-01` gate=5：下标差得 25、`merged_index` 差得 **57**。
   用错会把约束的代价低估一半以上。

**通用判据**：凡是「跳过输入、只用手头保留的元素重算」的折叠/降采样逻辑，都要问一句
**被跳过的东西里有没有更极端的**；如果「修好它」和「保住产量」冲突，先证明冲突有多硬
（DP/穷举上界），再决定改不改 —— 别让「改了更像对的」变成一笔隐性重校准。

---

## 57. 改了「确定性 id」的**列**，没改 payload 里那份**冗余副本**（R67 实测，潜伏两个版本）

**现象**：`public.cpt_structure_event` 里 `structure_id` 列与 `payload->>'id'` **不一致**，
727 行（距今两个版本，从 R27-4 起潜伏）：

```bash
# 判定命令
.venv/bin/python - <<'PY'
import psycopg
from cpt.adapters._dbconfig import connection_kwargs
with psycopg.connect(**connection_kwargs()) as c, c.cursor() as cur:
    cur.execute("""select array_length(string_to_array(structure_id,':'),1) seg,
                          count(*) filter (where payload->>'id' = structure_id) ok,
                          count(*) tot
                     from public.cpt_structure_event group by 1 order by 1""")
    for seg, ok, tot in cur.fetchall():
        print(f"  {seg} 段：payload.id 一致 {ok}/{tot}")
PY
# 迁移前实测 -> 4 段：一致 19396/20123（**727 行不一致**，全是 payload.id 停在 3 段）；5 段：84/84
```

**根因**：`state_to_payload` 把 `state.id` 也写进 jsonb（`emotion_cpt.structure.structure_events:371`），
于是同一个 id 在表里有**两份**：`structure_id` 列 + `payload['id']`。R27-4 那次的迁移
（`scripts/migrations/2026-10-06_r27_market_prefix.sql`）只 `UPDATE ... SET structure_id`，
**没同步 payload** —— 于是这 727 行的 payload.id 停在 `bi:5:1788…` 这种 3 段形态，
连 `crypto:` / `cn:` 前缀都没有。

**为什么两个版本都没人发现**：读路径**没有任何判据读它**。

- `latest_events` / `timeline` / `recent_events` 全部按 **`structure_id` 列**等值匹配
  （`WHERE structure_id = ANY(%s)` / `= %s`），不解析 payload；
- `state_from_event` 里 `id=_as_str(payload.get("id"), event.structure_id)` —— payload.id
  只是**返回值的一个字段**，没有任何 `if` 拿它做分支；
- HTTP 出口与前端（`dashboard/dash-structure.js:274`）用的都是 `event.structure_id`。

⇒ 这个字段错得**完全安静**：不影响任何判定，只影响「如果你去读它，会读到一个假的 id」。
这正是它活了两个版本的原因 —— **没有判据的地方，错误不会自己冒出来**。

**发现它的过程本身值得记**：不是靠读代码，而是靠**迁移↔回滚的逐字节往返比对**。
R67 的回滚脚本第一版用 `jsonb_set(payload, '{id}', structure_id)` **就地重算**，
跑出来行数完美（20207 → 1990 → 20207）却**480 行对不上** —— 差分只藏在 jsonb 里，
`count(*)` 永远看不见。改成「备份原始 payload、回滚取原值」后才归零。

**修法（R67）**：迁移顺带把 payload.id 治成完整 id（`SET payload = jsonb_set(...)`），
回滚靠备份表里存的 `old_payload` **还原成原来那个错的样子** —— 回滚的语义是回到原状，
**包括原状里那个不一致**；用「重算」代替「还原」会让往返比对永远差那几百行。

**通用判据**：凡是「一份数据在主列和 jsonb/payload 里各存一份」的表，**改格式时必须同时改两处**，
并且**给它加一条判据**（断言 `payload->>'id' = structure_id`），否则这个副本会永久幽灵化。
同类必查：任何 `payload['id']` / `payload['version']` / 冗余 `status`（本表就有 `status` 列
冗余自 payload，R26 迁移文档明确说是「查询目标」—— 那种冗余是**被读的**，所以它不会腐坏；
**不被读的冗余才会**）。

**验收工具（R67 新增）**：任何一次性数据迁移都应跑

```bash
.venv/bin/python -m scripts.verify_migration_roundtrip \
    scripts/migrations/<迁移>.sql scripts/migrations/rollback/<回滚>.sql
```

它剥掉脚本自带的事务控制、在**单个外层事务**里跑 迁移→快照→回滚→快照，最后只
`rollback()`（无 commit 路径），逐行逐字段比对并给出 sha256。**只看行数的验收会骗过自己**。

---

## 58. 合成 bar 的时间轴取在**未来**，于是 `occurred_at` 卖掉了整块面板（R67 实测）

**现象**：`public.cpt_structure_event` 里 **53 行 `occurred_at > now()`**，最远
`2026-12-14`。而 `recent_events(conn, limit=10)` 按 `occurred_at DESC` 取前 10 条 ——
**面板顶部 10 条全是未来日期**，真事件被压到看不见。用户看到的是「12 月 14 日有个笔」。

**定性过程（先后猜错一次，记下来）**：初判是「A 股快照吃进了未来日期的 bar」，
依据是这 53 行**全部带 `payload['cause']`**（= 走过 A 股记录路径、当轮有水位行），
且落在与真实历史批次同时段（`created_at = 2026-10-06 10:29:59.642405` 等）。
这个推断**错了**。决定性的反证是**把测试的合成序列喂进 `detect_fractals` 复算**：

```bash
.venv/bin/python - <<'PY'
from datetime import UTC, datetime, timedelta
from cpt.domain.fractal import detect_fractals
from cpt.domain.models import CanonicalBar
DAY_MS = 24 * 3600 * 1000
start = datetime(2026, 6, 1, tzinfo=UTC)          # ← tests/test_ashare_placeholder_rows.py 的起点
bars = []
for i in range(200):                                # ← 同一个 200 根
    base = 10.0 + (i % 7) * 0.1
    ms = int((start + timedelta(days=i)).timestamp() * 1000)
    bars.append(CanonicalBar(open_time=ms, open=base, high=base + 0.3, low=base - 0.3,
        close=base + 0.1, volume=1000.0 + i, close_time=ms + DAY_MS - 1,
        quote_volume=2000.0 + i, trade_count=0, taker_buy_base_volume=0.0,
        taker_buy_quote_volume=0.0, is_closed=True))
fut = [f for f in detect_fractals(tuple(bars)) if f.start_time >= 1792886400000]
print(sorted({datetime.fromtimestamp(f.start_time / 1000, UTC).date() for f in fut}))
PY
# 输出与生产里那 16 个未来 start_time **逐日完全一致**（10-25/26、11-01/02、…、12-13/14）
```

⇒ 那 53 行**全部**是 `tests/test_ashare_placeholder_rows.py::_bars(200)` 的产物：
`2026-06-01` 起连续 200 个**日历日**，末端到 `2026-12-17`，比今天晚两个月。
假 client 的 `_get_conn()` 返回 `None`，`record_structure_events` 于是自开生产连接
（这个渗漏已由 `cpt/application/persist_gate.py` 修掉）。

**两个独立错误，得分开记**：

1. **测试的合成时间轴取在未来**。`_bars(200)` 的注释解释了「用连续日历日」的理由
   （A 股允许日历缺口，不必造交易日历），但**没解释为什么起点是 6 月 1 日**。
   200 个日历日跨 6.5 个月，从 6 月起就必然越过「今天」。合成序列应当**锚在
   `now` 往回推**，而不是锚在一个写死的日期往后数。
2. **`occurred_at` 被赋予了「数据里的时间」的语义**。`record_structure_events` 用
   `state.end_time`（bar 时间）当 `occurred_at`，这是**对的**设计 —— 事件发生在
   行情上，不在写库那一刻。但它意味着**只要喂进来的 bar 在未来，事件就在未来**。
   于是「时间戳荒谬」这件事没有任何一层会拦：没有 `CHECK`、没有应用层判据、
   `recent_events` 也不过滤。

**为什么面板受害而不是数据受害**：`recent_events` 是唯一按时间排序的读接口
（`ORDER BY occurred_at DESC LIMIT %s`），其它读路径都是按 `structure_id` 等值匹配，
所以这个错误**只在一个地方可见**，而且看起来像「功能正常、只是内容奇怪」。

**通用判据**：
- 合成/夹具数据的时间轴**必须锚在 `now` 上**（`now - n*interval` 往回推），
  不能锚在一个写死的历史日期往后数 —— 后者会随时间推移**必然**跨进未来，
  今天不炸、几个月后炸，且炸的时候没人会联想到那个测试。
- 一个「时间来自数据、而不是来自写入动作」的列，要问一句**数据有没有上界校验**。
  没有的话，上游喂什么它记什么，包括 2099 年。
- 排序型读接口（`ORDER BY <时间> DESC`）是这类错误的**唯一放大器**：
  它在别处全静默，只在这里把假数据顶到第一页。

**处置（R67）**：三件事一起做，缺一件都会复发。

1. **渗漏本身**由 `cpt/application/persist_gate.py` 关掉（自开连接前就问开关）。
2. **那 53 行**随 `scripts/migrations/2026-10-19_r67_symbol_in_id.sql` 一并删除
   （它们全在 cn 段，与「被证明互相污染的 cn 历史」同一批）。
3. **合成时间轴改成锚在「今天」往回推**：`tests/test_ashare_placeholder_rows.py::_bars()`
   的 `start = datetime(2026, 6, 1, tzinfo=UTC)` 改为
   `today = datetime.now(UTC).replace(hour=0, ...); start = today - timedelta(days=n - 1)`
   —— 仍是连续日历日（该夹具刻意的性质），但**永远不越过今天**。
   光靠闸门不够：闸门管的是「写哪儿」，锚法管的是「造什么数据」。前者一旦被放开
   （比如有人为了跑集成测试把开关打开），后者会立刻再造一批未来行。

两条钉子：
- `tests/test_ashare_placeholder_rows.py::test_synthetic_bars_never_reach_into_the_future`
  ＋ `test_synthetic_timeline_is_contiguous_calendar_days` —— 直接钉住这个夹具的时间轴
  （**测的是测试文件自己**，不是被测代码；锚法被改回去就会红）。
- `tests/test_migration_r67_symbol_in_id.py::test_invariant_holds_for_the_live_table_after_migration`
  —— 把 `occurred_at > now()` 是否为零写成不变量断言，迁移后任何漏网的未来行都会暴露。

---

## 59. 「不限」的哨兵值 `0` 被库当成「未指定」，静默回落到默认上限 —— 而它砍掉的是**上游**数据（R68 实测）

**现象**：本仓三处写 `czsc.CZSC(raw, max_bi_num=0, min_bi_len=6)`
（`cpt/adapters/czsc_chanlun.py` 生产 parity 侧、`scripts/audit_p2_12_evidence.py`、
两个测试文件），字面语义是「不限笔数」。实测 czsc 1.0.1：

```python
CZSC(raw, max_bi_num=0).max_bi_num      # -> 50   ← 不是「不限」，是默认上限
CZSC(raw, max_bi_num=50).max_bi_num     # -> 50   ← 同一条路径
CZSC(raw, max_bi_num=10**9).max_bi_num  # -> 1000000000
CZSC(raw, max_bi_num=-1)                # -> OverflowError: can't convert negative int to unsigned
```

**根因**（`crates/czsc-core/src/analyze/mod.rs`）：

```rust
// :88 —— 0 落进「未指定」分支
pub fn resolve_max_bi_num(explicit: usize) -> usize {
    resolve_env_usize(explicit, "CZSC_MAX_BI_NUM", "czsc_max_bi_num", 50)
}
// :65 —— 第一条就是 if explicit > 0
fn resolve_env_usize(explicit: usize, upper: &str, lower: &str, default: usize) -> usize {
    if explicit > 0 { return explicit; }
    ...  default
}
```

**为什么贵**：截断在 `:229` `self.bi_list.drain(0..start_idx)` —— 但 `bars_raw`
**也跟着从头 drain**（`:243` 用 `bi_list.first().fx_a.elements[0].dt` 反推），
于是**分型集合一起被截**。1000 根 fixture 实测：

| | `max_bi_num=0`（实际 50） | `1e9`（真不限） |
|---|---|---|
| `bars_raw` | 593 | 1000 |
| `fx_list` | 202 | 322 |
| `bi_list` | 50 | 88 |

⇒ 被砍掉的是**最早那半段历史**，不是「多余的部分」。

**两个被这个哨兵值污染了数月的结论**：

1. `docs/reference-audit.md:86` 的「czsc 分型 **202**」—— 真实值是 **322**；
2. `docs/audit-p2-12-endpoint-extreme.md:81` 的「native 分型集比 czsc 大
   **50%~75%**（327 vs 202）」，并被当作「先修 `bi.py` 是在错的地基上施工」的
   论据。**放开后两套分型在共同窗口内下标与种类完全相同**（322/322、356/356、
   348/348）—— 分型层根本没有差异，`bi.py` 就是该修的那一层。

**副作用是反的**：中枢 parity 测试原本**因为两侧都被截断而通过**；改成真不限后，
CPT 中枢与 czsc `zs_list` 从「0/10 逐项相同」变成 **10/10、8/10、9/10**。
即修对之后，**对齐度大幅提升**，顺带把两条旧基线（`(202, 50)` / `min_width` 232.7
与 307.5）也一并更正。

**通用判据**：
- 任何 `max_*` / `limit` / `top_n` 参数，传「不限」之前先验证库对哨兵值的解释 ——
  0、-1、`None`、空串在不同库里含义相反（`0` = 不限 / `0` = 用默认 /
  `-1` = 不限 / 负数报错）。**写下来源**（本仓落成常量
  `cpt/adapters/czsc_chanlun.py:MAX_BI_NUM_UNLIMITED`，带完整根因注释）。
- 截断类参数的危险不在「少给结果」，而在**它改的是上游中间产物**：笔数被限制，
  但连带把 `bars_raw` 和分型集合也砍了，于是「基于分型的交叉验证」全部失真，
  且失真是**静默**的（结果依旧合法、格式正常）。
- 交叉验证两侧**必须同口径**，且口径要有**单一来源**。原先测试各自写死 `0`，
  适配器也写死 `0` —— 三份拷贝没有一处是「真不限」，所以谁也没发现。
- 「哨兵值 + 默认值」的组合要专门测：本仓钉子
  `tests/test_audit_r68_czsc_max_bi_num_truncation.py` 同时钉住
  `0 → 50` 的语义、截断前后两套计数、以及**放开后分型集合相同**。

**回归钉子**：`tests/test_audit_r68_czsc_max_bi_num_truncation.py`（9 条）。


## 60. `bigserial` 序列落后于 `max(id)`：不会自愈，每次跑批都炸（R72 实测，生产三次）

**现象**（同一症状出现三次，2026-10-20 / 10-21 / 10-22）：

```
psycopg.errors.UniqueViolation: duplicate key value violates unique constraint
"cpt_structure_event_pkey"
DETAIL:  Key (id)=(900008) already exists.
```

**根因**：`id` 是 `bigserial`，插入不写 id，取号靠序列。但仓里有代码**带 id
显式写入** —— 迁移的 `INSERT ... SELECT d.id`、测试的 `setval` 回滚。
一旦显式 id 超过序列当前值，后续每次 `nextval` 都落在已占用区间上：

- **不会自愈**：序列不会自己跳过已占用的号；
- **每次都炸**：不是偶发，是恒定失败，跑批逐只降级；
- **修了又坏**：手工 `setval` 之后，只要再跑一次带 id 的脚本就复发。
  第三次复发时 `last_value=900007`，而 `max(id)=903451` —— 落后 3444 个号。

**为什么手工修不管用**：手工 `setval` 修的是**当前状态**，不是**下一次**。
只要有第二个人（或第二个脚本）带 id 写入，落后就重新发生。所以修法必须是
运行时的、幂等的。

**修法**（`cpt/storage/structure_event_store.ensure_sequence_at`）：

```python
# 每次批量写之前，幂等地把它推到表尾
cur.execute("SELECT last_value FROM pg_sequences "
            "WHERE schemaname = %s AND sequencename = %s", (...))
cur.execute(f"SELECT max(id) FROM {table}")
# 只在真落后时才 setval —— 不然每批都要拿序列上的锁
cur.execute(f"SELECT setval('{sequence}', %s, true)", (max_id,))
```

四个实现要点，每个都有踩坑成本：

| 要点 | 不这么做会怎样 |
|---|---|
| 第三参必须是 **`true`** | `false` 会让下一个 `nextval` **重发 `max(id)` 本身** ⇒ 必撞 |
| `pg_sequences.last_value`，不是 `SELECT last_value FROM <seq>` | 后者要序列的 SELECT 权限，生产账户通常只有 USAGE ⇒ `insufficient_privilege` |
| `last_value` 为 NULL 按「要修」处理 | 无 SELECT 权限时它是 NULL；当成「已对齐」就会**静默漏修** |
| 放在 **INSERT 之前** | 撞了再修 = 这批已经废了，还得重跑；且失败语句已让事务 aborted |

**门禁⑯**（`scripts/check_bigserial_sequence.py`，R1–R4）：自愈必须在
`executemany` 之前调用（AST 判，不看字面量）、只允许一份实现、`setval`
第三参必须是 `true`、白名单里每张 bigserial 表都得接上。自检夹具
`fx_bigserial_sequence` 把自愈挪到写入之后 —— 证明它**能红**。

**通用判据**：
- 任何 `bigserial` / `serial` / `IDENTITY` 主键的表，只要仓里**存在**带 id 的
  写入路径（迁移、回滚、测试重置），就必须有运行时自愈。**「我们能保证不带 id
  写」不成立** —— 管得住 `cpt/`，管不住 `scripts/migrations/`。
- 测试替身（duck-typed cursor）在 store 层加了探测查询之后**必须同步补方法**：
  R72 加 `fetchone` 之后，两个测试文件的桩各有 `fetchall` 没有 `fetchone`，
  6 个测试同时红。替身与真实驱动的接口差距是**测试自身的缺陷**，修法是补桩，
  不是把生产代码降级成 `fetchall`。
- 断言「只开了一个 cursor」这类**计数**断言，一加查询就会碎。优先按语义定位
  （R72 里 `_insert_call` 按 `INSERT INTO ...` 子串找），而非钉下标/计数。

**回归钉子**：`tests/test_structure_event_store.py`（`ensure_id_sequence` 7 条）
+ `scripts/check_bigserial_sequence.py` + 自检夹具。


## 61. editable 安装指向已删除的路径：**只有 cron / systemd 会炸**（R74 实测，连续两天）

**现象**：两个作业每天按时触发、每天失败在同一句：

```
ModuleNotFoundError: No module named 'cpt'
```

- `cpt-dashboard-ashare.service`（timer 每天 08:00 UTC）失败 2 天，最后一次成功 10-19；
- `watchlist-daily-summary` cron（10:10）失败 2 天；
- 而同一台机器上 `pytest`、`python -m cpt.web`、所有手工跑批**全部正常**。

**根因**：`.venv` 的 editable 安装指向了一个已经不存在的目录。

```
.venv/lib/python3.14/site-packages/__editable___cpt_0_0_0_finder.py:9
MAPPING: dict[str, str] = {'cpt': '/home/ubuntu/work/.morning/Chan_Pattern_Trader/cpt'}
                                          ^^^^^^^^^^^^^^^^^^^^^ 该目录不存在
```

finder 文件 mtime 2026-10-14；`/home/ubuntu/work/.morning/` 早已删除，
唯一活着的仓库副本是 `/home/ubuntu/DSH/Chan_Pattern_Trader`。

**为什么两周没被发现**（三层遮蔽，缺一层都不会漏）：

| # | 遮蔽物 | 为什么绕过故障 |
|---|---|---|
| 1 | 测试与跑批一律写 `PYTHONPATH=.` | 显式把仓根塞进 `sys.path`，editable 安装根本不参与 |
| 2 | 服务用 `python -m cpt.web` | `-m` 会把 **CWD** 放进 `sys.path[0]`，而 CWD 恰好是仓根 |
| 3 | cron / systemd 用 `python /abs/path/scripts/x.py` | `sys.path[0]` 是**脚本所在目录**（`scripts/`），CWD 不参与 —— 只有它们炸 |

**一分钟复现**（这条命令要成为习惯）：

```
cd /tmp && env -u PYTHONPATH /home/ubuntu/DSH/Chan_Pattern_Trader/.venv/bin/python -c "import cpt"
```

**修法**（在仓根执行，`--no-deps` 避免动依赖）：

```
.venv/bin/python -m pip install -e . --no-deps
```

**教训（通用）**：**带 `PYTHONPATH=` 的验证都绕开了真实故障路径**。
凡是「服务能跑但定时任务跑不了」的分歧，第一件事是**从仓外的目录、不带任何
环境变量**再 import 一次 —— 而不是去看脚本日志里那句被反复刷屏的报错。

**门禁⑰**（`scripts/check_editable_import.py`）：在**仓外临时目录 + 清空
`PYTHONPATH`** 的条件下，用仓内解释器跑 `find_spec('cpt')`，要求成功**且落点
在本仓内**（防止导进了另一份旧 clone —— 那正是本 bug 的形态：import 不报错，
只是导错地方）。判据用子进程实测，**不用 grep 路径**，因为改一行字符串就能骗过
grep。自检夹具 `fx_editable_import` 在临时仓库里造「有 `cpt/`、有解释器、
但这解释器不认识它」的局面 —— 证明门禁**能红**。

**同类隐患**：凡是要被 cron / systemd 直接执行的脚本，都不该依赖
`PYTHONPATH` 或 CWD（`factor_backfill.py` 曾有此缺陷，R96 已删除）。

## 62. `setval` 不受事务回滚：测试看起来干净，序列已被永久污染（R81 实测）

**现象**：`#60` 的 `UniqueViolation` 修了四次都复发。每次都是同一条
`Key (id)=(900008) already exists`，每次 `setval` 之后能好一阵，跑几次测试又坏。
R72 的自愈机制上线后**仍然复发**（第四次：`last_value=900007 < max(id)=910525`）。

**排查走错的路**（记下来，下次别重走）：

| 假设 | 怎么排除的 |
|---|---|
| 生产代码在带 id 写 | `grep "INSERT INTO public.cpt_structure_event"` → 两个 store 的列清单里**都没有 `id`** |
| 迁移脚本在带 id 写 | 那几处 `INSERT ... SELECT id` 只在**已执行过**的一次性迁移里，不会每轮跑 |
| 有别的服务在写 | `cpt_signal_event` 的序列始终对齐，只有结构事件表落后 |

**真根因**：`tests/test_migration_r67_symbol_in_id.py` 的夹具。它为了给合成行
（id 900001~900007）让路，在事务里执行：

```python
cur.execute("SELECT setval('public.cpt_structure_event_id_seq', max(id), true)")
```

紧邻的注释写着「**随回滚消失**」。**那条注释是错的。**

PostgreSQL 文档明确：`setval` 的变更**不会被 `ROLLBACK` 撤销**。实测（R81）：

```
序列 = 910570 → setval(900009) → rollback() → 序列 = 900009
```

于是每跑一次那个测试文件，序列就从 91 万被拉回 900009，而表里真实行在 91 万级
⇒ 下一次 `append_events` 的 `nextval` 落在**已占用**区间 ⇒ 必炸。

**为什么这么难查**：pytest 的 `rollback` 让这个测试在**字面上**是干净的 ——
表里没有残留行、序列之外的一切都复原了。污染只体现在一个**不被事务保护**的
全局对象上，而且爆炸点在**离真凶几百行之外**的生产跑批里。

**正确的夹具写法**（`tests/test_migration_r67_symbol_in_id.py::_install_fake_pre_migration`）：

```python
cur.execute(
    "SELECT COALESCE((SELECT max(id) FROM public.cpt_structure_event), 0), "
    "(SELECT last_value FROM pg_sequences "
    " WHERE schemaname = 'public' AND sequencename = %s)",
    (_SEQ_NAME,),
)
max_id, seq_value = cur.fetchone()
base = int(max(max_id or 0, seq_value or 0))
rows = [(base + i, *row[1:]) for i, row in enumerate(_FAKE_ROWS, start=1)]
```

合成行放到 `max(max(id), last_value)` **之后**，**完全不动序列**。
万一算出的起点仍低于序列（环境不干净），`pytest.skip` 并给出原因，
而不是硬插一个必然撞主键的行。

**判定命令**（任何时刻可跑）：

```bash
# 序列是否落后 / 是否被污染
psql -c "SELECT max(id), (SELECT last_value FROM pg_sequences
         WHERE sequencename='cpt_structure_event_id_seq')
         FROM public.cpt_structure_event"
```

判据不是「相等」——`nextval` 会跳过一些号，`last_value > max(id)` 是**正常**的。
只有 `last_value < max(id)` 才是病。

**门禁⑯ R5**（`scripts/check_bigserial_sequence.py`）：`tests/` 里不许出现
`setval` 调用。判据必须**两样都查**：

1. **SQL 字符串字面量**里出现 `SELECT ... setval(` —— psycopg 的常态，
   `setval` 在字符串里，AST 里根本没有调用节点；
2. **裸调用** `setval(...)`。

⚠️ R5 第一版只查 AST 调用点，被反向注入实测打脸：真实形态是
`cur.execute("SELECT setval(...)")`，门禁**静默放行**。第二版只查行文本，
又被**自己的 docstring** 骗（里面写着「原实现有一行 `setval(...)`」）。
最终判据 = 字符串字面量 + AST 调用点。

**教训（通用）**：**测试的 `rollback` 不能证明它没留下副作用**。凡是操作
事务外全局对象（序列、 advisory lock、文件、外部服务）的测试，都要单独问一句
「这个副作用回滚了吗」—— `setval` / `nextval` / `pg_advisory_lock`（会话级）
都不在事务保护范围内。

## #63 「测试全绿」不等于「测试有效」：阈值类断言必须凑够阈值（R89 实测）

**现象**：给 `intraday_status.collect_status` 写了条测试
`test_empty_and_skipped_are_not_failures`，断言「empty/skipped 不算失败」。
21 条测试全绿。然后把 `empty` 注入成「算失败」，**21 条还是全绿**，一条没抓。

**根因**：那条测试只放了 **2 条** empty 记录，而「连续失败」的门槛是 3。
即使把 2 条都误算成失败，也到不了阈值，`state` 依然是 `running` ——
测试断言的东西（state）根本没被触发。

**通用判据**：凡是 `n >= 阈值` 才改变行为的逻辑，测试**至少**要喂到阈值那一条。
写 `max_failures=3` 的测试，输入就得是 3 条。

**怎么发现**：只有反向注入能发现。正向全绿 + 肉眼读代码都不行 ——
代码是对的，测试看着也像对的，错在**输入的规模**。

**这条的变体**（同一轮踩过的）：
- 断言 `not_tradable == 1`，实际是「每个 horizon 各计一次」= 4。
- 正则搜全文判「有没有 import 某模块」，文档字符串里提一句就误判 → 改用 AST 节点。

## #64 systemd `OnCalendar` 的 `/STEP` 是**小时**步长，不是分钟（R89 实测翻车）

**现象**：给盘中采集写了

```
OnCalendar=*-*-* 09:30/2:00 Asia/Shanghai
OnCalendar=*-*-* 13:00/2:00 Asia/Shanghai
```

本意「从 9:30 起每 2 分钟一轮」。装上后 `systemctl list-timers` 显示
**Next 是两小时之后**，第一批数据只在 09:30 / 11:30 / 13:30 各来一次。

**根因**：`HH:MM/STEP` 的 `STEP` 修饰的是**小时字段**。
`systemd-analyze calendar "*-*-* 09:30/2:00 Asia/Shanghai"` 打印
`Next elapse: Fri 2026-10-23 01:30:00 UTC`（明天），一眼可见它没在按分钟走。
写 `09:30/2` 也一样 —— 规范化结果仍是 `09:30/2:00`，`/2` 默认按小时解释。

**正确写法**：把**小时**写成范围、分钟的步长写在冒号后面：

```
OnCalendar=Mon..Fri *-*-* 09..11,13..14:00/2:00 Asia/Shanghai
```

规范化成 `09..11,13..14:00/2:00`，实测 Next elapse 是 2 分钟后。

**顺带作废的一条旧结论**：R89 第一版注释写「一个 OnCalendar 里塞两个时间点
（`01:30/2,5:00/2:00`）解析不了，必须拆两行」。那是**同一误解的副作用** ——
失败的不是「两个区间」，是把 `/2:00` 当分钟用。用 `9..11,13..14` 这种小时
列表一行就够。

**怎么提前发现**：任何新 timer 装之前先跑
`systemd-analyze calendar "<表达式>"` 看 **Next elapse**，而不是等
`list-timers` 出结果。`list-timers` 也会显示，但要等到装上才发现。

## #65 「有没有 import」不是判据，「有没有调用」才是（R89 第三次踩同一坑）

**现象**：给 `intraday_collectd.trading_day` 写了条防回归测试，断言函数体里
「查的是 `trade_calendar` 而不是 `daily_bar`」。实现方式是 AST 找 import +
字符串常量。然后反向注入把 `verdict = is_trade_day(conn, today)` 改成
`verdict = None`（**不再查日历**），**20 条全绿没抓到** —— 因为 import 还在。

**根因**：`from cpt.adapters.a_share_local import is_trade_day` 这一句没删，
测试只看「导入列表里有 `is_trade_day`」，它就在。至于结果有没有被用，没查。

**通用判据**：用 AST 做「某函数有没有真的做某件事」时，三档判据强度是

| 判据 | 强度 | 骗过它只需 |
|---|---|---|
| 搜全文文本 | 最弱 | 注释/docstring 里提一句 |
| AST 找 import | 中 | 保留 import 不调用 |
| AST 找 **Call** 节点 | 够 | 只能真删调用 |

`ast.Call` 的 `func` 是 `ast.Name` 时取 `.id`，是 `ast.Attribute` 时取 `.attr`，
两种都要覆盖（`is_trade_day(...)` 与 `self.is_trade_day(...)`）。

**同一轮踩的三个变体**（记在一起，因为它们是一类）：
1. 搜全文判 import —— `intraday_analysis.py` 的 docstring 提了一句 `psycopg`
   就被判成「接线了」。
2 判 docstring 时用 `ast.get_docstring(fn)` 的返回值去比对 `ast.Constant.value`
   —— 前者是去缩进后的文本，**永远不相等**，按值排除会漏掉 docstring。
   必须按**节点身份**（`fn.body[0].value is n`）排除。
3. `ImportFrom` 的被导入名字在 `n.names` 里，不在 `n.module` 里。只看
   `module` 会得出「没导入 is_trade_day」的假结论。

## #66 测试直接写进了生产表：`_persist()` 没被隔离，19 行假数据晚于所有真实轮次（R91 实测）

**现象**：`public.intraday_seal_snapshot` 117 行里有 19 行是
`code='600519', sealed=true, bid1_lots=50000.0, limit_price=1100.0`，时间戳
UTC 11:51–15:05，**全部晚于** 3 个真实轮次（09:20:36Z / 09:20:55Z / 09:22:13Z）。
后果不是「多几行噪音」—— 盘中面板用 `at = (SELECT max(at) ...)` 取最新一轮，
于是它读到的是那 1 行假的：**显示「1 只封板、封单 5 万手、强度正常」，
33 只真实票被完全遮蔽**。

**根因**：`tests/test_intraday_collectd.py` 的隔离夹具只做了**一半**。

```python
# 隔离了日志……
monkeypatch.setattr(collectd, "LOG_PATH", tmp_path / "intraday.log")
# ……但没隔离数据库。run_round() 末尾无条件调 _persist()，
# DB_WRITE_ENABLED 默认为 True，于是假 scan() 的结果真的落库了。
```

那两条用例（`test_no_candidates_is_empty_not_error`、`test_ok_round_carries_counts`）
喂的是 `600519 / 50_000.0 / 1100.0` 这种一眼可辨的假值，**它们自己写得没错，
错在没意识到 `run_round()` 有副作用**。

**三层防线**（只做一层不够）：

| 层 | 做法 | 抓得住什么 |
|---|---|---|
| 夹具 | autouse `_no_real_database`：`DB_WRITE_ENABLED = False` | 所有走 `run_round()` 的用例 |
| 读取端 | `_REAL_ROWS = "code NOT LIKE '\x01%%'"` 加进三个读取函数 | 万一有哨兵行漏进来，也读不到 |
| 真库测试 | 一个事务 + `finally: rollback()`，哨兵代码 `\x01` 开头 | 验证过滤器真的生效（反向注入 `_REAL_ROWS="TRUE"` → 4 failed） |

**通用判据**：写测试前问一句「**这条用例调到的那条路径，会写到哪里**」。
`run_round()` 名字像纯函数，实际做 HTTP + 写库两件事 —— 名字不承诺副作用。

**同族事故（CPT 第 3 次，都是「测试的副作用越过了只影响自己」）**：

| 次 | 事故 | 事后防线 |
|---|---|---|
| R81 | 测试用 `setval` 改序列，`rollback` 撤不掉 | 门禁⑯ R5：`tests/` 里不许出现 `setval` |
| R90 | 测试 `pop` `sys.modules`，重导出造出第二份异常类 | 回归钉子：断言「执行前后是同一个模块对象」 |
| R91 | 测试直接写生产表 | 夹具关写 + 读取端过滤 + 真库回滚测试 |

清理方式：先按**可判定特征**定位（`code` + `bid1_lots` + `limit_price` 三个一起），
再两项对照确认（这些 `at` 上总行数 == 19 ⇒ 确为单票轮；真实轮里该代码从未出现
⇒ 不会误删），备份成 CSV 再 `DELETE`。

## #67 用文本锚点插代码，锚点落在**函数之间的空白**：上一个函数被截断却不报错（R91 实测三次）

**现象**：给 `scripts/watch_period_check.py` 加 `_MARKET_TZ()` 和 `check_seal_coverage()`。
跑巡检直接崩：

```
TypeError: 'NoneType' object is not subscriptable
  problems = [p for c in checks for p in c["problems"]]
```

`check_service()` 返回了 `None`。

**根因**：用 `def _now() -> datetime:` 当锚点插入 —— 这个锚点**也出现在
`check_service` 的函数体之前**，而插入时把新函数塞进了：

```python
def check_service() -> dict:
    result = {...}
                            # ← 新函数塞在这里，check_service 到此结束
def _MARKET_TZ() -> Any:
    ...
    return ZoneInfo("Asia/Shanghai")
    # unit 状态：……            # ← 原 check_service 的剩余全部变成死代码
    if state != "active":
        ...
    return result
```

Python 不报错（死代码合法），`ruff` 不报错（没有语法问题），
**只有真的跑一遍才发现**。

**判据**：改完一个文件，至少跑一次「会用这个文件的那条真实路径」。
单跑 `check_intraday()` / `check_seal_coverage()` 都是 dict，看起来全对 ——
崩的是我没单独测的 `check_service()`。

**同轮第 2、3 次**：

- 给 `cpt/web/app.py` 插 `_seal_board_payload`，锚点 `def _latest_market_day() -> Any:`
  命中两处，把 `_intraday_log_path` 的函数名盖掉了 → `ruff` 的 `F821 Undefined name`
  是唯一抓手，且表现为「全量红、单跑绿」。
- 反向注入后想撤销，用 `git checkout <untracked 文件>` —— 报错且什么都没还原，
  随后手工 `replace` 又把一行删多了。

**三条可直接执行的规矩**：

1. 插入代码前先 `grep -n "<锚点>"`，**确认命中数**。命中 ≥2 就必须换更长的锚点
   （带上函数上一行的注释或签名全行）。
2. 改完跑一次**真实路径**，不是只跑单测。函数返回 `None` 这种错单测很难覆盖到。
3. **untracked 文件不许用 `git checkout` 撤销** —— 它本来就不在版本库里。
   改之前先 `cp` 一份备份，或者精确 `replace` 回去。

---

## #68 临时对象取连接：表达式一结束 `__del__` 就把连接关了（R94 实测，2026-10-25）

**现象**：不是超时、不是网络、不是 RDS 故障转移。是 `psycopg.OperationalError: the connection is closed`，
而且**两条路表现完全不同**：

| 调用方 | 看到的东西 |
|---|---|
| 测试 | 26 条红（18 ERROR + 8 FAILED），看着像「数据库连不上」 |
| `GET /api/dashboard/track` | `200` + `{"available": false, "reason": "OperationalError: the connection is closed"}` —— **优雅降级，不报错不告警不重启** |
| `scripts/compare_chanlun_backends.py` | 直接 `Traceback` 崩在第 132 行 |

**根因**：`cpt/adapters/a_share_local.py` 的 `_get_conn()` 是 **per-instance 缓存**
（`if self._conn is None: self._conn = open_conn()`），而同一个类为了修连接泄漏加了：

```python
def __del__(self) -> None:
    self.close()          # close() 里 conn, self._conn = self._conn, None; conn.close()
```

于是 `AShareLocalClient()._get_conn()` 这种**临时对象写法**命中了 CPython 的引用计数：

```
AShareLocalClient()   ← 实例只在表达式里存活
    ._get_conn()      ← 交出一条连接
                      ← 表达式结束，实例引用计数归零 → __del__ → close() → 连接被关
调用方 cur = conn.cursor()  → OperationalError: the connection is closed
```

**一行判据**（决定性，秒出）：

```python
AShareLocalClient()._get_conn()          # status=1 (BAD)，cursor 报 the connection is closed
c = AShareLocalClient(); c._get_conn()   # status=0，cursor ok
```

**为什么能潜伏**：
- 两处修法各自都对 —— 加 `__del__` 修了「cpt-dashboard 持 103 个 socket 打满 max_connections=100」；
  `_get_conn()` 缓存修了「每请求新建连接」。**两条凑一起才出错**。
- 报错被最外层 `except Exception` 接住，写进 `reason` 字符串，页面只是少一块内容。
- 单跑 `tests/test_track_maintenance.py` 时**同样的代码是绿的** —— 因为该文件里 `_open_pg_conn()`
  恰好被 GC 时机错开了（`client` 变量在函数栈上多活了一帧）。**「单跑绿、全量红」不一定是测试污染**，
  也可能是 GC 时机。

**修法（R94，方案 A）**：给模块加一个**不经过 client 生命周期**的入口：

```python
def open_conn() -> Any:
    """开一条新连接。调用方负责 close()。"""
    return psycopg.connect(**connection_kwargs())
```

`_get_conn()` 内部改成 `self._conn = open_conn()`（DRY），8 处「临时对象取连接」全部改成 `open_conn()`：
`cpt/web/track_api.py`（**生产代码**，12 处 `_conn()` 共用）、`scripts/compare_chanlun_backends.py`、
`scripts/verify_public_contracts.py`、4 个测试文件。

**为什么不用「只改调用点、让 client 活到用完」**：那是把陷阱留给下一个人。
`_get_conn()` 的问题是**既交出连接又保留所有权** —— 缺陷在接口，不在调用方。

**三条通用规矩**：

1. **交出去的东西，不要保留所有权。** 一个方法要么返回「你自己管」，要么返回「我管着」，
   不能两者都是。`__del__` 里关闭 `self._conn` 时，就假定它还归自己 —— 这个假定必须有接口保证。
2. **`__del__` 里只关闭「确认还持有」的资源。** 加了 `__del__` 之后，所有「临时对象」写法都会变成本 bug。
   加之前先 `grep` 一遍该类被实例化的地方。
3. **降级路径要能被看见。** `available: false` + `reason` 是好设计，但**没有告警**就等于是
   「静默失败」。这条 API 恒返 false 了至少一天，靠人肉点页面才发现。

## #69 腾讯 `hfq` 与它自家的 `qfq`/`bfq` 收益对不上：拿它当标尺会得出伪结论（R95 实测）

**现象**：`scripts/verify_public_contracts.py` 的「腾讯 hfq vs 本地因子表」交叉校验报
「两侧后复权价差 8.37%」（600519：腾讯 9316.39 vs 本地 8536.19）。

**第一版判断（错的）**：原始价两侧完全一致，差异全在复权因子 ⇒ 判定为「常数级锚点差，
常数倍缩放不改变收益率，对策略无影响」。看着很像，其实是把**序列本身有问题**读成了
**锚点不同**。

**实测（600519，40 个交易日，同一时刻同一接口）**：

| 比较 | 结果 |
|---|---|
| 腾讯 `qfq` vs 腾讯 `bfq` 价格 | 逐日**完全相同**（`qfq==bfq==1335.00`），收益差 `0.00e+00` |
| 腾讯 `hfq` vs 腾讯 `bfq` 收益 | **39/39 天全部不一致**，最大差 `5.03e-03` |
| `tencent bfq` vs `local daily_bar.close` | **最大差 0.0**（逐日零误差） |
| `local_ret` vs `bfq_ret` | 完全相同（本地因子窗口内恒定 ⇒ 乘常数不动收益） |
| 腾讯 hfq 隐含因子（`hfq/bfq`） | **逐日漂 6.978~7.088**（±0.2%） |
| 本地 `asel.ref_adjust_factor` | **恒定 6.39414671** |

跨股「大涨大跌日 `hfq_ret/bfq_ret` 中位」（n≈24~29）：600519=`0.801`、000001=`0.768`、
600036=`0.721`、002249=`0.745` —— **每只票各自的常数，不是全局常数**。

**结论**：腾讯 `hfq` 是一条**自相矛盾**的序列（与自家 `qfq` 收益都对不上），
它的「后复权」不是「原始价 × 常数」这种结构。⇒

- **不能**拿腾讯 `hfq` 的绝对价或收益率去校验别的源的复权口径；
- **不能**用 `hfq/raw` 反推复权因子（会得到逐日漂移的因子）；
- **能**用的是腾讯 `bfq` / `qfq` —— 它与本地 `daily_bar.close` 逐日零误差。

**现在的判据**（`c5_hfq_matches_local_factor`）：原始价零误差（硬）+ 本地因子窗口内
恒定（硬）+ 只有腾讯 `hfq` 自身自洽时才拿它当标尺（条件）。

**遗留**：`asel.ref_adjust_factor` 3,484,374 行里 **28,569 行（0.8%）** 的 `source`
是 `tx:fqkline`，即由腾讯 `hfq/raw` 反推出的因子 —— 与上面的发现直接冲突，值得复查。

**三条通用规矩**：

1. **「差异是常数」要实测，不能从「没有阶跃」推断。** 我先看 400 天 ratio 在
   1.074~1.121 之间漂、判「没有阶跃是常数」—— 4.4% 的漂移范围本身就不叫常数。
2. **交叉校验前先验标尺自洽。** 拿 A 判 B 之前，先确认 A 与 A 自己的另一条腿对得上。
   腾讯 `hfq` 与 `qfq` 不自洽这一步，只要两条序列比一次收益就能发现。
3. **两个源对不上时，先问「谁的内部是自洽的」**，而不是「谁的锚点是什么」。
   本地侧（raw 零误差、因子恒定、收益一致）三条全对；腾讯侧一条都不对。

## #70 用「供应商后复权 ÷ 不复权」反推因子：把漂移固化成台阶（R96，2026-10-26 实测）

**现象**：`asel.ref_adjust_factor` 348 万行里有 28,569 行（0.8%）`source='tx:fqkline'`，
即由 `腾讯 hfq 收盘 ÷ 不复权收盘` **逐日相除**得到的因子。同一条 `(code, trade_date)`
序列上，因子值会**天天变**（600825：144 行里 83 种不同因子；605577：123 种）。

**为什么这是错的**：后复权因子按定义是**分段常数** —— 两次除权之间必须逐位相同。
逐日相除得到的是一条不断漂移的曲线，等于把漂移当成真台阶固化下来。
`factor_recompute.py` 的 docstring 从 R37 起就写着「唯一真值是公司行动本身」。

**R95 补上了「为什么腾讯 hfq 不能当标尺」的实测**（600519，40 个交易日、同一接口同时刻）：

| 比较 | 结果 |
|---|---|
| 腾讯 `qfq` vs 腾讯 `bfq` 价格 | 逐日**完全相同**，收益差 `0.00e+00` |
| 腾讯 `hfq` vs 腾讯 `bfq` 收益 | **39/39 天全部不一致**，最大差 `5.03e-03` |
| 腾讯 `hfq` 隐含因子（`hfq/bfq`） | 逐日漂 **6.978~7.088** |
| 东财口径因子 | **恒定 6.39414671** |

⇒ 腾讯 `hfq` 与它**自家**的 `qfq`/`bfq` 都对不上。它既不能当标尺（见 #69），
也不能反推因子。

**实际影响面比 0.8% 小得多 —— 但方向不是「没问题」，是「数量级错」**：

- `public.daily_bar` 最早只到 **2024-01-02**；28,569 行 tx 因子里**只有 251 行**
  落在有日线的日期上，其余 28,318 行是 2023-06-02~2024-01-01 的**孤儿行**
  （永远匹配不上日线、永远不会被消费）。
- 但这 251 行的问题不在小数级而在**量级**：源切换处相邻日因子跳变最大 **+97.17%**
  （000002 在 2024-01-02：tx `185.459806` → 东财 `363.057863`），
  720 处源切换里 **172 处 >1%**。
- 一旦将来把日线补到 2023，这批错 2 倍的因子会立刻变成 100% 的伪收益。

**修法**（`scripts/migrations/2026-10-26_r96_repair_tx_factor_rows.sql`，可重复执行）：

1. 备份 28,569 行到 `asel.ref_adjust_factor_tx_backup_20261026`；
2. 有日线的 251 行：因子与 `source` 都改成暂存表（东财口径）—— 实测改前就已逐位相同，
   这一步只是把过时的 `source` 标签改对；
3. 无日线的 28,318 行：锚点换成**该票最早一个有日线那天**的东财因子（前推恒定），
   `source` 明写「缺除权前收盘，不做台阶」—— 2023 段取不到除权前收盘，而
   `factor_from_actions` 的原则是「宁可少一个台阶，也不用猜的收盘价」。

**验收**：跨界最大跳变 **+97.17% → 0.000000%**（219 只）；251 行与东财不一致数 **0**；
剩余 tx 行 **0**；表总行数 3,484,374 → 3,484,374（未增未删）。

**⚠️ R96 已删掉产生 tx 行的路径**：`_ensure_factors_and_persist` / `fetch_factor_rows()` /
`upsert_factor_rows()` / `factor_ensurer_from_env()` 整套按需因子链已删除（腾讯 hfq/raw 相除
路径不复存在，`a_share_routes` 不再接受 `ensure_factors` 参数）。

**三条通用规矩**：

1. **「有的行有问题」要先问「这些行有没有消费方」。** 28,569 行里只有 251 行落在有日线的
   日期上。不先算这一步，会把 1% 的问题报成 100% 的问题（我第一版就报了「0.8% 行可疑」，
   深挖后才看清 99.1% 是孤儿行、剩下的问题不在数值而在量级）。
2. **常数级差异和量级级差异要分开判。** 「差 8.4%，是常数倍，不影响收益率」这种话
   必须先实测「是不是真的常数」—— 实测结果是相邻日跳 97%，那就完全不是常数。
3. **数据修复要落成可重跑的 SQL 文件**，附带备份表 + 自检 `RAISE EXCEPTION`。
   在 psql 里手动改完就走的修复，下次没人知道做过什么、做了多少。

---

## 63. CI 红在**第一步**，后面 17 道门禁全部变成「没跑过」（2026-10-29 实测，连续 12 天）

**现象**：`gh run list -L 100` → **93 failure / 7 success**，最后一次绿是 2026-10-17。
每次红都停在 `Run unit and integration tests`，它之后的
`Gates self-test / Static quality gates / Doc claims / Doc counts / Dead-code audit …`
在 Actions 页面上显示为 `-`（skipped）。

**为什么这是最坏的一种红**：门禁全绿的日志与门禁没跑的日志，**在退出码上一样**。
于是这 12 天里 `ruff` 攒了 11 条错误、6 个文件没格式化、vulture 3 条、
`application/` 的行数断言漂了 325 行 —— 没有任何一道门禁报警，因为它们压根没启动。

**四个独立根因**（每个都足以让 CI 红）：

| 根因 | 位置 | 判据 |
|---|---|---|
| 硬编码 `.venv/bin/python` | `tests/test_seal_board_wiring.py` | runner 上没有 `.venv` → `FileNotFoundError` |
| 空库没有表 | CI 只起 Postgres、从不建表 | `live_conn` 用例 `relation ... does not exist` |
| 依赖兄弟仓 | `tests/test_intraday_rules.py::test_constants_match_adr` | `import emotion_core` → `ModuleNotFoundError` |
| 门禁自己钉死 `.venv` | `scripts/check_editable_import.py` | 解释器不存在 → 恒 `return 1` |

**修法**：测试/脚本一律 `sys.executable`（或 `CPT_PYTHON`）；CI 加
`Bootstrap CPT tables in test DB` 一步，按文件名顺序跑「建表/加列」迁移；
对账类用例统一走 `_emotion_core_intraday()`，拿不到兄弟仓就 `skip`。

**判定命令**（改完必跑）：

```bash
gh run view --job <id> | sed -n '/JOBS/,$p'   # 看「X 在哪一步」+ 后面是不是全 `-`
```

**通用规矩**：**门禁链的每一步都必须是独立 job，或至少「失败不阻断后续」**。
现在这种「pytest 一红、后面全跳过」的串联，等于把 18 道门禁绑在一根绳上。

---

## 64. 假游标的行形状与真 SQL 不同步：单测全绿，真库必错（2026-10-29 实测）

**现象**：`cpt/storage/intraday_seal_store.py::load_seal_track` 在真库上返回
`sealed='600293'`（代码字符串，恒真）、`bid1_lots=True`、`limit_price=847522.5`（封单量）。
单测 `test_seal_track_is_ordered_and_scoped` 一直绿。

**真根因**：`_SELECT_COLS` 在 R93 从 4 列扩到 9 列、`code` 插在第 2 位，
而读侧的映射仍是 `sealed=r[1] / bid1_lots=r[2] / limit_price=r[3]`（**错一位**）。
测试的假游标喂的是**旧的 4 元组**，索引 1/2/3 恰好落在 sealed/bid1_lots/limit_price 上
⇒ 形状对不上，但断言看不出。

**判定**：喂一条**与 `SELECT` 逐列同序**的真实行给假游标，断言每个键的值。

```python
rows = [(AT, "600293", True, 847522.5, 5.23)]   # at, code, sealed, bid1_lots, limit_price
assert out[0]["sealed"] is True                  # 错位时这里是 '600293'
```

**修法**：`_SELECT_COLS` 收回 CPT 真正读的 5 列（那 4 列归 emotion-core，契约 §2.1
另有一份 9 列 SELECT）；映射改 `r[2]/r[3]/r[4]`；测试假行改成 5 元组。

**通用规矩**：假游标/假连接的行形状**必须从 SELECT 列清单派生**，不许手写。
「测试绿、真库错」的假绿，比没有测试更危险 —— 它把 bug 变成了「已验证」。
