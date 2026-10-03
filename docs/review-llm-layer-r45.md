# 复盘：`cpt/llm/` 层（R45，2026-10-03）

> 起因：`architecture.md` §2.1 给这层打过 ✅（依据是 R25「建层」+ R28「验证修 3 个问题」）。
> R45 核实后认为那**不是复盘**（复盘 = 以层为单位的勘察 + 重做），于是整层重做。
>
> 结论：**8 个文件 / 1,240 行全部审完，3 个真 bug，全部已修并验证。**

---

## 0. 为什么这层值得重做

它是**唯一与外部 LLM 服务对话的层**，而外部契约漂移**不会让任何测试变红** ——
这与 R44 那个 §4 bug（东财用 `success:false` 表达「没分红」，代码当成失败，
掐停整轮 3036 只）是同一类风险。

实测外部触点：**1 / 8 文件**（`providers/openai_compatible.py` 发 urllib）。
比 `adapters/`（6/19，31.6%）低，但同样属于「出去问别人要数据」。

---

## 1. 三个真 bug

### ① `config.py::load_config` 会清空整个进程环境

**为什么严重**：这是**整个 LLM 层唯一读密钥的地方**（`CPT_LLM_API_KEY`）。

原实现为了「传 dict 就只读这份 dict」：

```python
previous = dict(os.environ)
os.environ.clear()            # ← 清空整个进程环境
os.environ.update(environ)
```

源码注释写的是「局部覆盖…**不碰进程全局**」，**而它字面上就在改进程全局**。

CPT 是多线程的（`ThreadingHTTPServer` + LLM worker）。任何线程在
`clear()` 与 `update()` 之间读环境变量，拿到的都是**残缺甚至空**的环境 ——
包括 `RDSHOST` / `DB_PW` / 飞书 webhook。

**实测**：回退成旧实现后跑新测试，真实线程读到了 **520 次读不到 `DB_PW`**。

**修法**：把 `source` 一路传给 `_env_str/_env_int/_env_float/_env_bool`，
从根上不碰全局。功能不变。

### ② `structured.py::parse_structured` 把合法 JSON 标量归成 `not_json`

模块 docstring 自己写着「**最危险的是 B，不是 D**」—— B 是「合法 JSON、
形状全错」，`json.loads` 成功、字段是错的，一路传下去直到前端空白才被发现。

而**另一族 B** 被归错了：

| 输入 | 修复前 reason | 真相 |
|---|---|---|
| `"just a bare string"` | `not_json` | **解析成功了**，只是类型不对 |
| `123` / `null` / `true` | `not_json` | 同上 |
| `{"code":200,"data":{}}` | `schema_mismatch` ✓ | 用例 B，本来的对 |

`not_json` 的定义是「找不到任何能解析的 JSON 候选」—— **它明明解析成功了**。

**归类撒谎比归类粗糙更有害**：粗糙只是信息少，撒谎会把人引向**错误的假设**
（排查的人会去找"为什么模型不吐 JSON"，而真相是"它吐了，只是形状不对"）。

**根因是个巧合掩盖的 bug**：`schema_failed` 标志初始化在第 3 步之前。
对象形态的用例 B 之所以正确，是因为第 3 步的 `_iter_braces` 会把同一个容器
**再扫一遍**，那次才置上位。标量压根没有 `{`/`[` 可扫 ⇒ 标志永远 False。

### ③ `__init__.py::get_queue` 静默丢弃后传的审计回调

`application/llm_cases.py` 有**两个** `get_queue` 调用点：

```
_bootstrap()  →  get_queue(on_status=on_llm_status)   ← 真正落库的那一方
list_calls()  →  get_queue()                          ← 不传回调
```

而 `list_calls` 是 `GET /api/dashboard/llm/calls` 的处理函数 ——
**看板打开就会轮询它**（R45 真机 CDP 抓包：首屏即调 `/llm/calls?limit=20`）。

原实现见到 `_QUEUE is not None` 就直接 return，把新回调丢掉。于是：

```
用户先打开过看板 → 队列带着【空回调】建好
再点「解释结构」 → on_status 被丢弃、永远不注册
⇒ LLM 跑完了，cpt_llm_call 里那条永远停在 queued，UI 永远转圈
```

**实测**：复现脚本里审计回调被调用 **0 次**（修复后 2 次）。

**修法**：`LLMQueue.set_on_status()` 补注册（后设覆盖先设）；worker 侧全部改走
`_emit()` 取**快照**再调（持锁调用会卡住补注册，而回调自己要开 DB 连接）；
配 `_callback_lock` 保护（worker 读、HTTP 线程写）。

⚠️ **修法里的顺序陷阱**（已用 `test_existing_queue_survives_config_disabled` 盯住）：
补注册那段**必须在 `load_config()` 之前**。我第一版放到了后面，于是
「环境变量被改成停用」时 `get_queue()` 返回 `None` ——
**把正在正常运行的队列凭空藏起来**（队列还在跑、线程还在打 LLM，
但调用方拿到 None）。

---

## 2. 这层本来就做对的地方（别在重做时改坏）

| 位置 | 做法 |
|---|---|
| `config.py` | `api_key: str = field(default="", repr=False)` —— 密钥不进 dataclass repr |
| `config.py` | `redacted()` 只回 `has_api_key: bool`，不�� key |
| `queue.py` | **`except LLMRateLimited` 排在 `except LLMError` 之前** —— 前者是后者的子类，顺序正确，**躲过了 R40 那个坑** |
| `queue.py` | worker 外层 `except BaseException` —— 注释记了真机「线程一死就永久静默丢任务」 |
| `queue.py` | `submit()` 查 `_worker_alive()` 而不只看 `_stop` —— R28-4 真机丢过调用 |
| `openai_compatible.py` | 错误分类按**真机抓包**定的（429 响应体空、无 `Retry-After`）；结构不认识就显式抛 |
| `structured.py` | `_scan_balanced` 手工处理字符串转义，并写明为什么不能用 `text.find('}')` |
| `levels.level_label` | 未知 level 返回 `未标注级别（level=abc）`，**不编造**；a_share 的 5 → `日线级别`（不是 5 分钟，R28-9 修过） |

---

## 3. 扫过但确认无需改动的

- **`registry.py`**（38 行）—— 末尾那个 `raise` 看着像死代码，其实是
  「加了 provider 忘了加分支」的兜底，**故意留**。
- **`prompts.py`**（113 行）—— fuzz 11 种畸形 `structure`（level 是
  str/None/list/bool/超大数、market 未知、围栏注入）：**零崩溃**，
  `json.dumps(default=str)` 兜住。
- **`base.py`**（114 行）—— 只有 Protocol 与数据类，干净。

---

## 4. 遗留（低危，记此备查）

1. **围栏可被撑破**：若 `structure` 的字符串值里含**奇数**个 ``` 围栏，
   user 消息里的 json 围栏会提前闭合。实测危害有限 —— 聊天模型仍读得到 JSON，
   且 `structured.py` 本就防御式解析（`parse_structured` 的第 2 步就是解围栏）。
2. **`structure` 未校验**：`submit_llm_explain(code, structure)` 的 structure
   **直接来自 HTTP 请求体**，既不回查 DB 也不校验形状。这是「用户注入自己的
   LLM 调用」，无权限边界跨越；但若将来 LLM 结果要展示给**其他**用户，
   这里需要重新评估。

---

## 5. 复核命令

```bash
# 新增的三组回归（全部离线）
python -m pytest tests/test_llm_config_env_isolation.py -v
python -m pytest tests/test_llm_structured_reasons.py -v
python -m pytest tests/test_llm_queue_callback.py -v

# 畸形输入 fuzz（应 0 处抛异常）
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0,'.')
from cpt.llm.prompts import render_structure_payload
for lv in ["abc", None, [1,2], True, 10**18]:
    render_structure_payload(code="600519", name="x", market="a_share",
                             structure={"id":"x","level":lv})
print("fuzz ok")
PY
```
