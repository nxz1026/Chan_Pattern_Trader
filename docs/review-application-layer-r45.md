# 复盘：`cpt/application/` 层（R45）

> 32 文件 / 5,170 行（`a_share_snapshot.py` 一个就 1,135 行）。
> 这是**漂移最大**的一层（R24 以来 +1,307 行），也是 R25~R45 反复被动过的
> 一层 —— 从没按层复盘过。R45 全审完。
>
> 结论：**3 个真 bug，全部已修并验证**。

---

## 1. 三个真 bug

### ①② DB 故障被报成「缺因子」（同一个 bug 的两处）

**① `_try_on_demand_factors`**（约 1046 行）

    except Exception:  # 注释：「DB 类问题不该在这里吞掉，交给外层」
        return None          # ← return 就是吞掉

而**同一文件**别处白纸黑字写着：

> 必须与「没数据」和「DB 挂了」分开报 —— 报成 db_error 会把排查方向带偏（实测踩过）

这两句直接冲突。外层 `except Exception` 做的**正是**对的事（记 warning +
报 `db_error:<类型>`），但内层把异常吞了，外层**根本收不到**。

**② `build_ashare_snapshot` 补因子后重读失败**（约 205 行）—— 同样报
`no_factor`、同样不 rollback。走到这里的前提是「因子已**成功**落库」，
所以重读失败**几乎必然是 DB 问题**。

两条修法：
- 外层 `except Exception` 补 `_rollback_quietly`（**仅共享连接时** ——
  自有连接的 `finally` 会 close，不受影响）；
- 两处都改成「记日志 + rollback + 如实报 `db_error:<类型>`」。

### ③ `canvas_wbt` 的 CDN 护栏自己有个后门，且零测试

那道外链检查的注释写着「防御性：模板升级引入外链时响亮失败」——
**它的存在意义**就是拦模板升级引入的外链。但：

- 原正则要求**显式协议**（`https?://`），**协议相对 URL**
  （`src="//cdn.example.com/x.js"`）直接漏过去 —— 同样是外链，断网同样拉不到；
- 而画布 D 恰恰是「**断网可用**」（R16-5 验收项）的验收对象，
  它用 `srcdoc` 渲染 wbt 报告；
- `grep cdn_reference_leaked tests/` **无命中** —— 一道自称防御性的护栏
  从没被测过。

修法：`(?:https?:)?//`。**刻意不误伤** `src="/vendor/x.js"` ——
那正是页面要注入的本地资源，误伤会把画布 D 整个打掉。

---

## 2. 我在这层错的两次（比任何 bug 都值得记）

### 2.1 `except` 块内部的异常，**不被兄弟 handler 接住**

第一版修法：把 ① 的吞异常删掉，「让它冒到下面的 `except Exception` 统一处理」。

**错。** Python 里 `except` 块**内部**抛出的异常不会被同一 `try` 的**兄弟**
`except` 子句捕获（那些只匹配 `try` 主体抛出的异常）。结果异常直接逃出
`build_ashare_snapshot` —— 原来至少返回一个 degraded 快照，改完变 500。**更糟。**

正确修法：在 `except` 块**内部**自己 try/except、就地处理。

### 2.2 scp 静默失败 ⇒ 有三轮「改完测试不对」

真因不是代码，是**远端跑的还是旧文件**。现在每次传完都用
`grep -c <目标标识符>` 校验真的更新了。

这两条写进 `known-traps.md` #24。

---

## 3. 确认干净、无需改动

| 位置 | 结论 |
|---|---|
| `_ensure_factors_and_persist` | 自建 client + `finally` close ⇒ 失败连接是一次性的，不需要 rollback。**设计得好** |
| `MetricRecorder` 处的 except | 已被 R45 那轮修的 `run_metric.MetricRecorder.record`（写失败时 rollback）覆盖 |
| `canvas_wbt` 的 HTML 注入 | 进 HTML 的值全是数值/枚举（`_num`/`_stamp`/"向上"），symbol/kind 过了 `html.escape(quote=True)` ⇒ **注入面处理正确** |
| `dashboard_runs` 的 200MB 预算 | 注释里明说是「显式预算」；实测进程 RSS 仅 **75MB**（ring 未满），机器尚有 12GB 可用 ⇒ 现状无压力 |
| 20~40 行的 `dashboard_*` 小文件 | 只读投影薄层，标注「已接线」，无可修逻辑 |
| `build_ashare_snapshot` 其余 except | `:201`/`:223` 是**业务异常**（缺因子/无行情），`:247` 是纯计算校验 ⇒ 均不需要 rollback |

**两轮模式化扫描零命中**（27 个剩余文件）：
- 「except 返回 falsy / 静默 pass 且无日志无回滚」：0
- 「注释声称『交给外层』但代码在吞」：0

## 4. 测试

新增/改写 4 个测试文件、约 37 个用例，**全部离线**：

| 文件 | 用例 | 盯什么 |
|---|---|---|
| `test_ashare_db_error_not_masked.py` | 6 | DB 故障必须报 `db_error` 而非 `no_factor`；共享连接必须 rollback |
| `test_canvas_wbt_cdn_guard.py` | 20 | 6 条必须抓到的外链 + 5 条必须放过的本地引用 |
| （沿用 `test_storage_failure_semantics.py` / `test_llm_*`） | | |

每个修复都验证了**回退后测试变红**。

**全量测试**：仅剩 2 个既存基线失败（`test_dashboard_runs_index` /
`test_dashboard_wiring_d`），stash 掉全部改动后同样失败 ⇒ 与本轮无关。
