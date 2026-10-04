# 复盘：`cpt/web/` 层（R45）

> 5 文件 / 2,829 行（`app.py` 1,239 + `__main__.py` 1,055 占八成）。
> 这层 **R29 复盘过**（JSON 错误响应统一 + 运行时门禁），此后仍有漂移。
>
> 结论：**未发现需要修改的问题**。这是 R45 复盘四层以来第一个「干净」的结果，
> 而且**不是因为没认真看** —— 见 §2 的四处实证。

---

## 1. R29 留下的护栏仍然成立

`tests/test_web_error_contract.py`（4 个用例）保证「JSON API 的错误响应必须是 JSON」。
真机抽查 8 个畸形输入，**全部如实**：

| 输入 | 状态码 | reason |
|---|---:|---|
| `a-share/snapshot?code=ZZZZZZ` | 400 | `invalid_code` |
| `a-share/snapshot?code=` | 400 | `code_required` |
| `canvas/wbt?start_ms=abc` | 400 | `window_not_int` |
| `multi-run?run_ids=` | 400 | `invalid_run_ids` |
| `compare?left=&right=` | 400 | `missing_run_id` |

## 2. 四处需要判断的地方，逐个查过 —— 都不是 bug

### ① `_read_json_body` 读失败返回 `None`（`app.py:1169`）

正确。它让调用方回**自己的 400**，而不是在 handler 里冒未捕获异常变 500 ——
注释把这个理由写清楚了，而且有 `_LOG.warning`。

### ② `_cached_snapshot` 缓存未命中返回 `None`（`__main__.py:608`）

正确。这是「**没命中**」信号而非「失败」。且 `move_to_end(key)` 之前先判了
`entry is None`，不会 KeyError。`ValueError`（周期标签不可解析）那条也**显式
说明了**是刻意不抛、交给 `_poll_once` 的既有降级路径。

### ③ `main()` 的 `KeyboardInterrupt → return 0`（`__main__.py:1047`）

正常。我的模式扫描把它当成了「返回 falsy」的假阳性。

### ④ `limit` 畸形 → HTTP 200 且无 reason（3 例）

实测确认**都是静默回落默认值**、返回的是有效数据：

    /api/dashboard/llm/calls?limit=abc        → 200, 6 条（abc→默认 20）
    /api/dashboard/runs?limit=-1              → 200, 50 条（clamp 到 ring 上限）
    /api/dashboard/runs?limit=99999999        → 200, 50 条（同上）

「200 + 合法默认值」是合理降级，不是撒谎。**我的探针把它标成 ⚠️ 是假阳性。**

## 3. 扫过的模式

- 「except 返回 falsy / 静默 pass 且无日志无回滚」：4 处候选，**逐个查过全是假阳性**
- 「注释声称『交给外层』但代码在吞」（R45 在 application/ 抓到的那个模式）：**零命中**
- 真机抽查 6 个正常端点：`degraded` 字段**全部为空**（无虚假降级标记）

## 4. 为什么这层干净是有原因的

R29 那次复盘**建立了运行时契约**（不是源码 grep），把「错误响应必须是 JSON」
变成了 CI 会红的断言。R45 在 application/ 找到的三个 bug，形态都是
**没有契约覆盖**的地方（DB 故障的 reason 语义、CDN 后门、跨层异常边界）——
这层因为有契约，恰好躲过了同一类问题。

## 5. 一个观察（不修，仅记录）

`__main__.py` 的 `main()` 与 `app.py` 合计 2,294 行，承担了
CLI 解析、provider 选择、HTTP handler 装配、降级策略四件事。
**拆分的信号**已经很明显（`serve_snapshot` / `make_handler` / provider 工厂
各自独立成块），但拆分会动到 HTTP 装配路径、风险大于收益 ——
**不在 R45 做，记在此处**。

## 6. 测试

R29 的 4 个契约用例 + HTTP/路由相关共 **42 个测试全绿**。
