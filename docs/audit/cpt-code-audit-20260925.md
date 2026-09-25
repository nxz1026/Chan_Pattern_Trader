# CPT 代码审核报告 — 安全性 / 模块化 / 精简度 / 重复造轮子 / 死代码

> 审核日期：2026-09-25
> 审核范围：`cpt/` 包、`dashboard/` 前端、`scripts/`、`tests/`
> 审核方式：AST 静态分析（复杂度 / 嵌套深度 / 参数数 / 重复函数体 / 死导入 / 未接线模块）+ 全文 grep（安全模式）+ 人工通读核心模块
> 明确**不做**：功能性审核（按需求排除）
> 依据工具：`ast` 自研分析器（输出 `audit.json`）、圈复杂度（**自研口径 = mccabe 基数 + 每个 `and`/`or` 计 1，非 radon 口径**）、`grep` 导入图双重交叉验证

---

## 0. 量化概览

| 目录 | 代码行数 | 说明 |
|---|---:|---|
| `cpt/adapters` | 3,981 | 交易所 / 数据源 / 本地 DB 适配器 |
| `cpt/application` | 2,885 | 快照构建、回放、canvas、dashboard_* 服务 |
| `cpt/domain` | 2,302 | 缠论纯函数领域（bi/zhongshu/trend_type/contain/recursion…） |
| `cpt/engine` | 816 | realtime + rebuild（**生产未接线**） |
| `cpt/storage` | 636 | repository（**生产未接线**） |
| `cpt/web` | 1,653 | HTTP 入口与路由 |
| **`cpt/` 小计** | **12,274** | |
| `dashboard/` | 4,120 | 前端 JS（`dashboard.js` 单文件 3,186） |
| `scripts/` | 380 | factor_backfill 等 |
| `tests/` | 7,802 | 约为生产代码的 63% |

**核心结论（TL;DR）**

- 工程质量整体**中上**：文档/注释密度高、`.importlinter` 分层契约清晰、安全基线良好（无硬编码密钥、无 `verify=False`、无 `shell=True`、SQL 全参数化、前端约定 `textContent` 不拼 HTML）。
- 两类系统性问题最突出：
  1. **死模块 / 孤儿代码层**：`engine/`、`storage/`、`domain/signal.py`、`domain/a_share_rules.py` 及 **14 个 `dashboard_*` 服务模块**在生产代码里**无任何导入方**，仅测试可达（**全口径 ≈ 2,353 行**「测试在测一段不会运行的代码」）。
  2. **跨文件重复造轮子**：27 组同名函数，集中在 `dbconfig`、`_as_float`、`_resolve_level`、`_bar_to_dict`、`_infer_interval_ms`、`CanonicalBar` 构造块，且部分已发生**漂移**（同名不同行为）。
- **代码屎山热点**：`web/app.py:make_handler`（圈复杂度 **94**〔自研口径〕、嵌套 **12 层**、332 行）；`dashboard/dashboard.js` 单文件 3,186 行 / 147 函数。

---

## 1. 安全性审核

### 1.1 已通过项（良好）

| 检查项 | 结果 |
|---|---|
| 硬编码密钥 | ✅ 无（`api_key/secret/password/token = '...'` 全文 grep 零命中） |
| TLS 校验绕过 | ✅ 无 `verify=False` |
| 命令注入 | ✅ 无 `shell=True`；生产唯一 `subprocess` 在 `wind_source.py:125`，使用 **list argv**（非字符串拼接），无注入面 |
| SQL 注入 | ✅ 3 处 f-string 拼接均为**模块级常量**表名/列名（`a_share_local._SECURITY_MASTER/_DATE_COL/_OHLC_COLS`、`a_share_rules._DERIVED_FIELDS` 由常量 `", ".join()`），**值一律走 `%s` 参数化** → 无注入风险 |
| 前端 XSS | ✅ 无 `eval` / `new Function`；`dashboard.js:21` 明确约定「所有 DOM 文本走 `textContent`，禁止把 snapshot 拼进 HTML」；测试里 `subprocess` 均为 headless-chromium / 起服务的测试脚手架 |

### 1.2 需关注项（低-中风险，均非功能缺陷）

- **S1（中）`dashboard/canvas_d.js:152` `doc.body.innerHTML = payload.body_html`**：内容是同源服务端 `wbt HtmlReportBuilder` 生成的报告，注入到 `<iframe sandbox="allow-same-origin allow-scripts">`。
  - 风险点：`allow-same-origin` + `allow-scripts` **同时**授予时，iframe 内脚本可移除自身 `sandbox` 属性（MDN 明示），隔离形同虚设；且 `runScripts()`（:89-97）会**重建并执行**片段里的 `<script>`。
  - 当前可控是因为「内容来自同源服务端、非用户输入」。建议：① 给 iframe 文档加 `Content-Security-Policy`；② 或对 `body_html` 做白名单 sanitize；③ 至少在代码注释里写明信任边界（「body_html 必须同源服务端可信」），防未来接入第三方数据源时埋雷。
- **S2（低）`wind_source.py:125` 调外部 Wind CLI**：list argv 无注入、已设 `timeout`（好）。建议补 stderr 长度上限与二进制绝对路径白名单，防 PATH 劫持。
- **S3（低）`dashboard.js:1085/1091` `localStorage` 存便签**：key 由 `noteKey(selection)` 拼接，未做命名空间/转义；selection 含特殊字符时可能串 key。低风险，建议 key 加固定前缀 + `encodeURIComponent`。
- **S4（提示）`a_share_snapshot.py:315/323` 模块级可变全局 `_FETCHER` + `global`**：非安全问题，属状态管理坏味道，见 §4.5。

> **安全总评**：无高危/中危可利用漏洞。重点是 **S1** 的 iframe sandbox 信任边界需要固化，其余为加固建议。

---

## 2. 模块化审核

### 2.1 优点

- `.importlinter` 显式声明了 `domain → engine → application → adapters → storage → web` 的分层契约，方向正确。
- `domain/` 纯函数化程度高（`contain.py`、`bi.py`、`zhongshu.py` 无 I/O、无系统时钟），可单测、可幂等重放——这是全仓最健康的层。
- `canvas_wbt.py` 等模块 docstring 写明「为什么这么做」（含实测依据），可维护性好。

### 2.2 问题：架构分层「名存实亡」——两层整层未接线

`.importlinter` 声明的层，与**真实生产调用图**不符。经 AST 导入图 + `grep` 双重验证，以下模块在生产代码里**没有任何导入方**（仅测试 import）：

| 模块 | 行数级 | 生产导入方 | 测试引用 |
|---|---:|---|---|
| `cpt/engine/realtime.py` | — | **0** | 1 |
| `cpt/engine/rebuild.py` | — | **0** | 1 |
| `cpt/storage/repository.py` | — | **0** | 4 |
| `cpt/domain/signal.py` | — | **0** | 1 |
| `cpt/domain/a_share_rules.py` | — | **0** | 1 |
| 16 个 `cpt/application/dashboard_*.py` | — | **0** | 各 1-2 |

→ 详见 §3「死模块」。

### 2.3 问题：Web 层巨型分发函数（屎山热点 #1）

`cpt/web/app.py:make_handler` —— **圈复杂度 94（闭包汇总，自研口径；radon 报 1、mccabe 报 84）、嵌套 12 层、332 行**，是全仓最危险函数；`do_GET` 圈复杂度 72（自研口径；radon 62 / mccabe 56）。单个内嵌 handler 承担了：路由分发 + query 解析 + 业务调用 + 快照序列化 + 异常到 HTTP 状态码映射，全部揉在一个闭包里。详见 §4.1 重构指导。

### 2.4 问题：前端单文件巨石（屎山热点 #2）

`dashboard/dashboard.js` —— **3,186 行、147 个函数**，是全仓**最大单文件**（超过任何 Python 文件）。状态管理、SVG 渲染（A/B/C 画布）、事件绑定、demo fixture 全塞在一个 IIFE 里。最大函数：`renderChrome` 169 行、`boot` 84 行、`drawChartA` 75 行、`setRangeState` 74 行、`renderResearchDetails` 89 行。详见 §4.2 拆分指导。

---

## 3. 死模块 / 死代码（重点）

> 判定口径：**生产代码（非 tests/）零导入方 + 仅测试可达**。与「该功能是否工作」无关（功能性不在审核范围）。

### 3.1 孤儿代码层：`engine/`（816 行）+ `storage/`（636 行）

`engine/realtime.py`、`engine/rebuild.py`、`storage/repository.py` 在 `cpt/` 生产代码里**无任何 import**（AST 导入图 + `grep "from cpt.engine|from cpt.storage"` 均只命中测试文件）。`.importlinter` 把它们声明为架构层，但生产快照管线并未消费它们。

**危害**：测试在覆盖一段**不会被任何入口执行的代码**，制造「已测试 = 可靠」的假安全感；同时约 1,450 行死重增加阅读与重构成本。

### 3.2 14 个 `dashboard_*` 服务模块

`dashboard_compare / dashboard_event_audit / dashboard_export / dashboard_levels / dashboard_market_fetch / dashboard_multi_run / dashboard_parity / dashboard_quality / dashboard_realtime / dashboard_runs / dashboard_signal_history / dashboard_stats / dashboard_watch / dashboard_watchlist / …`（共 14 个）——抽样核实（如 `dashboard_compare.py`，26 行）确认是结构完整的只读 helper，但**生产路由（`web/app.py` / `web/__main__.py`）并不 import 它们**，仅各自 1-2 个测试引用。

这与上一份 M7 审计的 **B2 结论一致，至今仍未处理**。

### 3.3 `domain/signal.py` + `domain/a_share_rules.py`

同样生产零导入。其中 `a_share_rules.fetch_daily_tags`（:65）、`t_plus_one_purchase_allowed`（:137）为 **test-only**（生产代码零调用）。

### 3.4 处置建议（三选一，给决策标准）

| 情形 | 处置 |
|---|---|
| 确为「规划中、即将接线」 | 在模块 docstring 顶部标注 `Status: pending-wiring, owner, deadline`，建 issue 跟踪，**设期限**；到期未接线则删 |
| 已被 `dashboard.py` / `dashboard_snapshot_v2.py` 等新实现取代 | **直接删除**（git 历史可回溯），并同步删除对应测试与 `.importlinter` 声明 |
| 仍想保留作为「备用算法」 | 移出 `cpt/` 包到 `experiments/` 或 `scripts/`，从生产包与分层契约里摘除 |

**关键原则**：让 `.importlinter` 的「声明架构」与「真实调用图」重新对齐——要么接线，要么删层，不要保留「纸面分层」。

### 3.5 其它死代码 / 澄清

- **误报修正**：`domain/recursion.py:138 _conforms_barlike` 位于 `if TYPE_CHECKING:` 块内，是 **mypy 编译期结构断言**（验证 `StructureElement` 满足 `BarLike` Protocol），**非死代码**，不应删除。静态扫描的「零引用」需结合 `TYPE_CHECKING` 语境判读。
- 各层 `__init__.py` 空壳（`cpt/adapters/application/domain/engine/storage/web`）：若不作门面聚合导出，建议保留 namespace 但加一行注释说明，避免读者误以为有公共 API。
- 测试内重复 `date()` helper（`test_a_share_local.py:73` ↔ `test_a_share_rules.py:20`）：提取到 `tests/conftest.py`。

---

## 4. 代码屎山（重点，含修复指导）

### 4.1 重构 `web/app.py:make_handler`（CC 94〔自研口径〕→ 目标 <10；验收改用 mccabe）

**症状**：单闭包 = 路由表 + 参数解析 + 业务 + 序列化 + 错误映射，332 行、嵌套 12 层。任何一处改动都要在巨型 if/elif 里翻找，回归风险高。

**重构路线（按收益排序）**

1. **路由表化**：把 `(method, path) → handler` 抽成模块级 `dict`，`make_handler` 只做匹配与分发。
2. **每路由一个纯函数**：`_handle_snapshot(provider, query) -> tuple[int, dict]`，彼此独立、可单测。
3. **横切关注点下沉**：query 解析抽 `_parse_window(query)` / `_parse_code(query)`；异常映射抽 `_to_http_error(exc) -> tuple[int, dict]`。
4. **分发器极简化**。

**重构后骨架**

```python
# cpt/web/app.py
RouteFn = Callable[[SnapshotProvider, dict[str, list[str]]], tuple[int, dict]]

_ROUTES: dict[tuple[str, str], RouteFn] = {
    ("GET",  "/api/dashboard/snapshot"): _handle_snapshot,
    ("GET",  "/api/dashboard/health"):   _handle_health,
    ("POST", "/api/dashboard/select"):   _handle_select,
    # ... 每个路由一个独立 _handle_* 函数
}

def make_handler(provider: SnapshotProvider):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  self._dispatch("GET")
        def do_POST(self): self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            route = _ROUTES.get((method, url.path))
            if route is None:
                return self._send_json(404, {"error": "not_found", "path": url.path})
            try:
                status, payload = route(provider, parse_qs(url.query))
            except CptError as exc:                      # 统一异常 → HTTP
                status, payload = _to_http_error(exc)
            self._send_json(status, payload)
    return _Handler
```

**验收**：`make_handler` 本体 < 30 行；每个 `_handle_*` < 15 行；`xenon --max-average B` 通过。

### 4.2 拆分 `dashboard/dashboard.js`（3,186 行 → 多模块）

**症状**：一个 IIFE 包揽状态 / 渲染 / 事件 / demo 数据，147 函数，`renderChrome` 169 行。

**拆分方案**（仓库已有 `canvas_a.js / canvas_b.js / canvas_c.js / canvas_d.js / market_a_share.js` 的按职责分文件先例，顺势扩展为 ES Module）：

| 新模块 | 职责 | 迁入内容 |
|---|---|---|
| `js/state.js` | 单一可变状态 + 订阅 | selection、range、runtime 状态 |
| `js/render/chrome.js` | 顶部/侧栏骨架 | `renderChrome`(169L)、`renderSelection`(65L) |
| `js/render/chart_a.js` | A 画布 K 线 | `drawChartA`(75L)、`drawCandles`(60L) |
| `js/render/details.js` | 研究详情面板 | `renderResearchDetails`(89L) |
| `js/demo_fixture.js` | 离线 demo 快照 | 大段字面量 fixture（:57 起） |
| `js/main.js` | 入口装配 | `boot`(84L) |

**收益**：单文件 < 400 行；demo fixture（纯数据）与渲染逻辑解耦后，前端可被 tree-shake / 按需加载；`boot` 只做依赖装配。

### 4.3 长函数清单（次优先级）

> 阈值：≥60 行 或 圈复杂度 ≥12。`web/app.py` 之外的主要对象：

| 位置 | 行数 | 建议 |
|---|---:|---|
| `dashboard.js renderChrome` | 169 | 见 §4.2 |
| `dashboard.js renderResearchDetails` | 89 | 拆为「取数 + 模板」两段 |
| `dashboard.js boot` | 84 | 只做装配，逻辑下沉 |
| `dashboard.js drawChartA` | 75 | 抽「坐标换算」「画笔/中枢」子函数 |
| `trend_type.py:163 classify_trend`(84) / `signal.py:167 assess_first_buy`(89，死模块) | 84-89 | 优先保证单一职责，必要时拆「校验 / 计算 / 组装」。（注：`contain.py` 最长函数仅 37 行，未入榜） |

（完整清单见 `audit.json` 的 `long_functions` / `complex_functions` 字段。）

### 4.4 `canvas_wbt.py`（448 行）——结构尚可，建议二分

注释质量高、职责相对聚焦（wbt 报告外壳）。建议把「HTML 片段抽取」（`_BODY_RE / _SCRIPT_RE / _CDN_RE` 三个正则 + `extract_body_fragment`）与「payload 构建」（`build_canvas_d_payload` + 内嵌 `_stamp`）拆成两个模块，单模块 < 250 行。

### 4.5 不合理逻辑 / 坏味道

| 位置 | 问题 | 修复 |
|---|---|---|
| `a_share_local.py:340` | `except Exception: pass`（吞**宽泛**异常，无任何记录） | 至少 `_LOG.debug(...)`，并收窄为预期异常类型 |
| `a_share_snapshot.py:342` | `except AShareNoFactorError: pass`（吞特定异常但无注释说明为何安全） | 补一行「为何可忽略」注释，或写入 `data_quality` |
| `wind_source.py:346` | `except OSError: pass`（清理路径，可接受） | 加注释说明是 best-effort 清理 |
| `a_share_snapshot.py:315/323` | 模块级可变全局 `_FETCHER` + `global` 语句 | 改为类实例属性，或显式依赖注入传入 fetcher |

---

## 5. 重复造轮子（重点，含修复指导）

> 27 组跨文件同名函数中，**确认重复**的如下。先按「复制性质」分三类，对应不同处置：
> - **A 类：同项目同包内复制** → 必须合并（无任何借口）
> - **B 类：同项目跨包复制** → 提取共享 helper
> - **C 类：跨项目刻意复制**（有文档说明的 `asel` 边界）→ 可保留，但需收敛到唯一权威源 + 注释指回

### 5.1【A 类·最严重】`~/.dbconfig` 解析三胞胎

**证据**：`_read_dbconfig` + `connection_kwargs` 同时存在于——
- `cpt/adapters/a_share_local.py:133 / :152`
- `cpt/adapters/a_share_pool.py:47 / :60` ← **与 a_share_local 同在 `cpt/adapters/` 包内，复制毫无道理**
- `scripts/factor_backfill.py:78 / :90`（第三份）

`a_share_local.py:135` 的注释解释「不 import `asel`，因跨项目」——这是**跨项目**（C 类）的合理理由，但**完全不能解释** `a_share_pool.py` 在**同包内**又抄一份。

**修复（合并为唯一权威源）**

```python
# cpt/adapters/_dbconfig.py   ← 新建唯一实现
from __future__ import annotations
import pathlib
from typing import Any

DB_CONFIG_FILE = pathlib.Path.home() / ".dbconfig"

def read_dbconfig() -> dict[str, str]:
    if not DB_CONFIG_FILE.exists():
        return {}
    out: dict[str, str] = {}
    for line in DB_CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("$") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out

def connection_kwargs(cfg: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = read_dbconfig() if cfg is None else cfg
    if not cfg.get("$RDSHOST") or not cfg.get("$DB_PW"):
        raise RuntimeError(f"~/.dbconfig 缺失 $RDSHOST 或 $DB_PW。位置: {DB_CONFIG_FILE}")
    return {
        "host": cfg["$RDSHOST"], "port": int(cfg.get("$DBPORT", "5432")),
        "dbname": cfg.get("$DBNAME", "longkonglong"), "user": cfg.get("$USER", "postgres"),
        "password": cfg["$DB_PW"], "connect_timeout": 15,
    }
```

- `a_share_local.py` / `a_share_pool.py` / `factor_backfill.py` 全部改为 `from cpt.adapters._dbconfig import read_dbconfig, connection_kwargs`。
- **注意差异**：`a_share_local.py:156` 额外拼了 `ssl_cert = ~/global-bundle.pem`。共享版返回后由 `a_share_local` 自行 `kw["sslmode"] = ...; kw["sslrootcert"] = str(ssl_cert)` 扩展，不要把 ssl 塞进共享版（pool 不需要）。

### 5.2【A/B 类·已漂移】`_as_float` 三胞胎

**证据**：同一意图「把外部字段解析成有限 float，失败抛领域异常」，三种实现已漂移——

| 位置 | 严格度 | 异常类型 |
|---|---|---|
| `adapters/binance_futures.py:244` | **严格**：拒 `bool`、查 `math.isfinite` | `BinanceDataError` |
| `adapters/ccxt_source.py:81` | 宽松：仅 `float(str(v))` | `CcxtSourceError` |
| `adapters/a_share_public.py:134` | 宽松 + 带 `row` 上下文 | `ASharePublicError` |

**修复（提取共享解析器 + 各适配器薄包装）**

```python
# cpt/adapters/_numparse.py
import math
def to_finite_float(value, *, field: str, exc_type, ctx: str = "") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise exc_type(f"字段 {field} 期望数值{ctx}, 实测 {value!r}")
    try:
        result = float(str(value))
    except (TypeError, ValueError) as e:
        raise exc_type(f"字段 {field} 无法解析为数值{ctx}: {value!r}") from e
    if not math.isfinite(result):
        raise exc_type(f"字段 {field} 非有限数值{ctx}: {value!r}")
    return result
```

各适配器保留一行薄包装传入自己的异常类型与上下文，例如：

```python
# binance_futures.py
def _as_float(v, *, field: str, index: int) -> float:
    return to_finite_float(v, field=field, exc_type=BinanceDataError, ctx=f"[{index}]")
```

> ⚠️ **行为变更提示**：统一后 `ccxt`/`a_share` 会变得「更严格」（拒绝 bool、拒绝 NaN/Inf）。这是**有意的健壮性提升**，但需在 PR 说明中标注，并跑全量测试确认无依赖宽松行为的用例。

### 5.3【B 类】`_resolve_level` 三胞胎

**证据**：`domain/bi.py:67`、`domain/zhongshu.py:91`、`domain/trend_type.py:67`，同一逻辑「显式 `level` 覆盖；否则要求所有输入同级，否则 `ValueError`」，仅入参形状不同（2 个分型 / N 笔 / 笔+中枢）。

**修复**

```python
# cpt/domain/_leveling.py
def require_uniform_level(levels, explicit, *, what: str) -> int:
    if explicit is not None:
        return explicit
    uniq = sorted(set(levels))
    if len(uniq) > 1:
        raise ValueError(f"未显式指定 level 时全部{what}必须同级别, 实测 {uniq}")
    return uniq[0]
```

三处分别薄封装：`_resolve_level = require_uniform_level((f.level for f in fr), level, what="分型端点")` 等。

### 5.4【B 类】`_bar_to_dict` ×3 / `CanonicalBar` 构造块 ×2 / `_infer_interval_ms` ×2 / import 块 ×2

- **`_bar_to_dict`**（`application/dashboard.py:69`、`application/export.py:51`、`engine/realtime.py:104`）：三份 CanonicalBar→dict。`engine/realtime` 是死代码（§3.1）**先删**，剩 dashboard/export 两份 → 收敛为 `domain/models.py` 的 `CanonicalBar.to_dict()` 方法（或 `application/_serialize.py`）。
- **`CanonicalBar` 15 行构造块**（`a_share_public.py:249-264` ↔ `wind_source.py:534-549`）：逐字节同款（`quote_volume=0.0, trade_count=0, taker_buy_*=0.0, is_closed=True, close_time=open_ms+DAILY_INTERVAL_MS-1`）。
  - 修复：在适配器共享 helper（如 `_canonical.py`）提供 `daily_canonical_bar(open_ms, o, h, l, c, v) -> CanonicalBar`，两处调用。`DAILY_INTERVAL_MS` 常量在两个适配器各定义一次，也应一并上移。
- **`_infer_interval_ms`**（`application/dashboard.py:76` ↔ `application/replay.py:327`）：近乎相同（取相邻 open_time 最小正间隔，回落 `config.levels[0]*60000`）。这正是 M7 审计的 **A3，至今未修**。→ 提取到 `application/_interval.py`，两处 import。
- **import 块**（`application/export.py` ↔ `application/replay.py`）：重复的公共 import 段 → 收敛到共享模块统一导出。

### 5.5 同名但**非**重复（澄清，避免误伤）

- **`_stamp`**（`canvas_wbt.py:374` 毫秒→字符串 vs `domain/signal.py:140` int 时间戳处理）：语义不同，不算重复；且 `signal.py` 是死代码。
- **`fetch_daily_bars`**（`wind_source.py:351` 方法 vs `a_share_public.py:176` 方法）：不同数据源的同名接口方法，属正常。建议提取 `MarketDataSource` **Protocol** 统一契约（`fetch_daily_bars(code) -> tuple[CanonicalBar, ...]`），而非删除。
- **`upsert_factor_rows`**（`scripts/factor_backfill.py:235` vs `adapters/a_share_factor.py:240`）：script 版是 adapter 版的复制，→ `factor_backfill.py` 应 `from cpt.adapters.a_share_factor import upsert_factor_rows`，删掉本地副本。

### 5.6 防复发机制（CI 护栏）

- 新建共享 util 层：`cpt/adapters/_common`（dbconfig/numparse/canonical）、`cpt/domain/_leveling`，并在各模块 docstring 顶部约定「解析字段→float / 构造 CanonicalBar / 解析 level 一律先查共享 helper」。
- CI 引入重复检测：`jscpd`（dup % 阈值，如 <3%）+ `radon cc` / `xenon`（圈复杂度阈值，如 average ≤ B、单函数 ≤ D 即失败）。
- 已有 `.importlinter`，补一条「禁止 `cpt.adapters.*` 内部跨模块复制 dbconfig/numparse」的代码评审 checklist。

---

## 6. 与上一份（M7）审计的对账

| 旧编号 | 问题 | 现状 |
|---|---|---|
| A1 | `cpt/llm` 冗余目录 | ✅ **已修复**（目录已删除） |
| A2 | `dashboard.js` 残留 `/api/dashboard/parity` fetch | ✅ **已修复**（grep 无命中） |
| A3 | `_infer_interval_ms` 双份 | ❌ **仍存在**（dashboard.py:76 ↔ replay.py:327） |
| A4 | `dashboard.js` 残留 "replaced" 字面量 | ✅ **已修复** |
| B1 | `engine/`、`storage/` 孤儿层 | ❌ **仍存在**（§3.1） |
| B2 | 14 个 `dashboard_*` 模块无生产导入方 | ❌ **仍存在**（§3.2） |

> 说明：旧审计修复了「表层冗余文件/字面量」（A1/A2/A4），但**结构性问题**（A3 重复、B1/B2 死模块）全部遗留至今。

---

## 7. 优先级行动清单

### P0（本周必做，高收益低风险）
1. **合并 `~/.dbconfig` 三胞胎** → 单一 `cpt/adapters/_dbconfig.py`（§5.1）。
2. **处置孤儿层与 14 个 `dashboard_*` 模块**：要么接线、要么删除、要么移出生产包（§3.4）。先删最无争议的 `engine/realtime.py`（其中 `_bar_to_dict` 还是被复制的源头之一）。
3. **重构 `web/app.py:make_handler`**：路由表化，CC 94 → <10（§4.1）。

### P1（两周内）
4. 收敛 `_as_float` / `_resolve_level` / `CanonicalBar` 工厂 / `_infer_interval_ms` / `_bar_to_dict`（§5.2–5.4）。
5. 拆分 `dashboard.js` 为 ES Module（§4.2）。
6. 修复 3 处 `except-pass`、消除 `_FETCHER` 全局状态（§4.5）。
7. `factor_backfill.py` 删除 `upsert_factor_rows` 本地副本，复用 adapter（§5.5）。

### P2（一个月内）
8. `canvas_wbt.py` 二分（§4.4）。
9. 提取 `MarketDataSource` Protocol 统一数据源接口（§5.5）。
10. CI 接入 `jscpd` + `xenon` 护栏（§5.6）。

---

## 附：审核产物

- 静态分析原始数据：`audit.json`（含全部函数复杂度 / 重复体 / 死导入 / 未接线模块明细）。
- 本报告：`cpt-code-audit-20260925.md`。

---

## 附录 B：核实修正记录（2026-09-25，密米尔）

> 本报告经独立复核，正文已就地修正 **3 处数字**；下列 **B.1 之外**的条目**未改动正文**，但落地前必须核验。
> 核实方法与完整证据见同目录 `verification-20260925.md`；原始静态分析数据见 `audit-20260925.json`（sha256 `f14a6cb9…`，501,250 bytes）。

### B.1 已就地修正（3 处，均为正文与自身 `audit.json` 矛盾的转录错误）

| 位置 | 原文 | 修正为 | 依据 |
|---|---|---|---|
| §0 / §3.2 标题与正文 / §6 B2 / §7 P0-2 | 「16 个 `dashboard_*`」 | **14 个** | `audit.json` 的 `unwired_modules` 与 `unused_funcs` **两处都只有 14 个**；grimp 可达性 + `vulture --min-confidence 60` 三方一致 |
| §4.4 标题 | 「`canvas_wbt.py`（683 行）」 | **448 行** | `audit.json` 的 `file_stats` 明写 `lines: 448`，且全文 `683` 出现 **0 次** |
| §4.1 标题 / §0 / §2.3 / §7 P0-3 | 「CC 94/72，radon 口径」 | **自研口径**（= mccabe 基数 + 每个 `and`/`or` 计 1） | `radon 6.0.1` 实测 `make_handler` **1**（不下钻函数内嵌套类）、`do_GET` **62**；`mccabe` **84 / 56**。`do_GET` 区间 `and`/`or` 共 **16** 个，`mccabe(56)+16=72` 精确吻合 |
| §0 / §4.3 | 「≈1,450+ 行」死代码总量 | **≈2,353 行** | 816+636+309+148+444（§3.1 单说 engine+storage 的 1,450 行是对的） |
| §4.3 | 「`contain.py` 等 domain 长函数 60+」 | 改为 `trend_type.py:classify_trend`(84) / `signal.py:assess_first_buy`(89) | `contain.py` 最长函数仅 **37 行**，未入长函数榜 |

### B.2 未改动正文，但落地前必须核验（5 条）

1. **§2.1「`.importlinter` 声明 `domain → engine → application → adapters → storage → web` 分层契约」不成立** —— 实际只有 4 条 `type = forbidden` 契约，无任何 `type = layers` 契约，也无 `cpt.application`/`cpt.web` 层约束。§3.1/§3.4「声明为架构层」的说法同样不准（engine/storage 是作为 forbidden 的目标/来源出现）。删这两层需同步处理契约 3、4。
2. **§5.4「import 块重复 → 收敛到共享模块统一导出」是伪问题** —— `dup_blocks` 命中的 `export.py:17 ↔ replay.py:60` 是多行**领域类型 import 列表**，token 级块哈希误判，不是重复逻辑。建议删除该条。
3. **§4.5 两条「无注释说明为何安全」与事实不符** —— `a_share_snapshot.py:342` 已有 `pass  # 正是要补的情形`，`wind_source.py:346` 已有 `except OSError:  # 记账失败不能影响取数`。只有 `a_share_local.py:340` 那条「无任何记录」属实。
4. **§5.5 `upsert_factor_rows` 不是「复制」而是已漂移的双实现** —— script 版有 `dry_run`、写 **9 列**（含 `source_ref`）；adapter 版无 `dry_run`、写 **8 列**。**直接删本地副本会丢干跑能力并改写入列集**，须先扩 adapter 版再合并。
5. **§4.1 路由表化骨架会拍平 A 股路由的「先于 provider 分发」短路**（`cpt/web/app.py:77-82` 有明确注释：否则每次 A 股请求白建一次加密快照，且加密侧不可达时 A 股路由永远走不到）。属功能性回归，是本报告风险最高的一条建议。

### B.3 报告漏项（4 条）

1. **CI 已有 vulture 死代码门禁，但阈值让它失效** —— `.github/workflows/ci.yml` 用 `--min-confidence 80`，而 vulture 把「未使用函数/类」定为 **60%**，恰好被全部过滤（实测 exit 0、零输出；降到 60% 则报 74 条）。这正是 14 个死模块长期存活的**根因**；§5.6 建议「引入护栏」时应改为「修正已有护栏阈值」。
2. `cpt/storage/models.py`（156 行）同样零生产导入（唯一导入方是已死的 `repository.py:21`），§3.1 未点。
3. `DAILY_INTERVAL_MS` 实有 **3 份**（`a_share_public.py:60`、`wind_source.py:69`、`validators.py:63` 的 `A_SHARE_DAILY_INTERVAL_MS`），§5.4 说「两个适配器各定义一次」漏了第三份。
4. `dup_blocks` 中报告未提的两处生产重复：`ReferenceChanlunConfig(...)` 构造块（`multi_level.py:48 ↔ replay.py:180`）、dataclass 字段块（`contain.py:105 ↔ models.py:62`）；`js_dup_names` 另有 `render`(4 文件) 与 `q`(2 文件)。
