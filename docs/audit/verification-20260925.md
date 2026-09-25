# CPT 审核报告核实结果 · v2（原始 audit.json 已补交）

> 本文件**取代 v1**（`CPT-审核报告核实-20260925.md`）。v1 中「audit.json 不含静态分析字段、CC 类数字无原始产物可复算」一句**已失效并撤回** —— 审核方于 2026-09-25 02:17 补交了真正的 `audit.json`（501,250 bytes，sha256 `f14a6cb9`），字段齐全。
> 被核报告：`cpt-code-audit-20260925.md`（sha256 `ee515c99`，391 行）
> 基线：`Chan_Pattern_Trader` HEAD `79170b6`，工作区干净
> 核实工具：`grimp 3.17`、`vulture 2.14`（CI 同款）、`radon 6.0.1`、`mccabe`（flake8 内核）、生产入口实测 `import cpt.web.__main__` + `sys.modules`

---

## 0. 补交件带来的结论变化

补交的 `audit.json` **是真货**，顶层 22 个字段全部有值、`errors: []`：

```
functions 1040 | long_functions 24 | complex_functions 24 | deep_functions 12
many_args 11 | mutable_defaults 0 | dup_names 27 | dup_bodies 1 | dup_blocks 21
dead_imports 136 | unwired_modules 28 | unused_funcs 17 | security 14 | smells 5
layer_loc 10 | file_stats 149 | grep_hits 5 | js_hits 4 | js_functions 5
js_dup_names 2 | main_guard_files 4
```

**核心变化：报告正文的三处争议数字，全部与它自己的原始数据矛盾。** 也就是说：

- **不是工具算错了，是报告作者抄错了。**
- `audit.json` 的结论与我的独立复核**完全一致**（14 个死模块、448 行、27 组同名函数）。
- 报告正文的「16」「683」「radon 口径 94」是**转录/标注错误**，需要改正文，不需要改数据。

| 争议点 | 报告正文 | audit.json 原始值 | 我的独立复核 | 判定 |
|---|---|---|---|---|
| 死 `dashboard_*` 模块数 | 16 | **14**（`unwired_modules` 与 `unused_funcs` 双处都是 14） | **14**（grimp + vulture 60% + 实测 import） | 正文错，数据对 |
| `canvas_wbt.py` 行数 | 683 | **448**（`file_stats`），全文 `683` 出现 **0 次** | **448**（`wc -l`，且 git 五版本全是 448） | 正文错，数据对 |
| `make_handler` / `do_GET` 复杂度 | 94 / 72「radon 口径」 | **94 / 72**（确有此值） | radon **1 / 62**、mccabe **84 / 56** | 数字对，**口径标签错** |
| 同名函数组数 | 27 | **27**（`dup_names`） | **27**（独立 AST 统计 29 减去 2 个 dunder） | 三方一致 ✅ |
| `dashboard.js` 函数数 | 147 | **147**（`js_functions`） | **147**（101 命名声明 + 46 命名箭头） | 三方一致 ✅ |

---

## 1. 原始数据核实：audit.json 与报告正文的一致性审计

### 1.1 ✅ 报告忠实转录了原始数据的地方

| 报告条目 | audit.json 对应字段 | 核对 |
|---|---|---|
| §0 量化概览表（9 行） | `layer_loc` | **9/9 精确**（`cpt/__init__.py: 1` 也在，报告未列） ✅ |
| §2.3 `make_handler` 332 行 / 嵌套 12 层 | `complex_functions[0]`：`lines 332, depth 12` | 精确 ✅ |
| §2.4 `dashboard.js` 3,186 行 | `file_stats`：`lines 3186` | 精确 ✅ |
| §2.4 147 个函数 | `js_functions['dashboard/dashboard.js']` = 147 条 | 精确 ✅ |
| §1.1 SQL f-string「3 处」 | `security` 中 3 条 `SQL f-string`（a_share_local:113/:267、a_share_rules:77） | 精确，且正确剔除了 10 条 tests-only subprocess ✅ |
| §1.1 subprocess 唯一在 `wind_source.py:125` | `security` 中该条源码 `subprocess.run(list(argv)...)` | 精确 ✅ |
| §1.2 S1 `canvas_d.js:152` | `js_hits.js_innerHTML` = `[["dashboard/canvas_d.js", 152, "doc.body.innerHTML = payload.body_html || \"\";"]]` | **逐字精确** ✅ |
| §1.2 S3 `dashboard.js:1085` | `js_hits.js_localStorage` 命中 :1085 | 精确 ✅ |
| §4.5 五行坏味道 | `smells` 5 条：a_share_local:340、wind_source:346、a_share_snapshot:315/323/342 | **一一对应，无多无少** ✅ |
| §3.5 `recursion.py:138` 是 TYPE_CHECKING 误报 | `unused_funcs` 标它为 `"no-refs-anywhere"` | **报告正确推翻了工具的标注**，加分 ✅ |
| §3.5 测试内 `date()` helper 重复 | `dup_bodies` 唯一 1 条 = `test_a_share_local.py:73` ↔ `test_a_share_rules.py:73/20` | 精确 ✅ |
| §5.4 `CanonicalBar` 同款块 | `dup_blocks`：`a_share_public.py:249` ↔ `wind_source.py:534` | **行号逐字精确** ✅ |
| §5.4 import 块重复 | `dup_blocks`：`export.py:17` ↔ `replay.py:60` | 原始数据确实命中 ✅（但见 §3.3，建议本身是伪问题） |
| §5.1/5.2/5.3/5.4 全部行号 | `dup_names` 27 组键名 | 27 组键与我独立统计**完全一致** ✅ |

### 1.2 ❌ 报告正文与原始数据矛盾（3 处，必须改正文）

#### (1)「16 个 `dashboard_*`」→ 原始数据就是 **14**

```python
>>> [m['module'] for m in audit.json['unwired_modules'] if 'dashboard_' in m['module']]
14 条
>>> len([x for x in audit.json['unused_funcs'] if 'dashboard_' in x[0]])
14
```

`audit.json` 两个独立字段都是 **14**，报告正文写「共 16 个」，而报告自己只列了 14 个名字 + 省略号。**纯计数错误。**

三方独立确认 14：`audit.json` / `grimp` 可达性 / `vulture --min-confidence 60`。活的 8 个是 `alerts`、`config_compare`、`indicators`、`inspector`、`market`、`reproducibility`、`runtime`、`snapshot_v2`。

#### (2)「`canvas_wbt.py`（683 行）」→ 原始数据就是 **448**

```
$ grep -o "683" audit.json | wc -l
0
$ python3 -c "…file_stats…"
{"file": "cpt/application/canvas_wbt.py", "lines": 448, "n_functions": 10, "max_fn_lines": 171, "max_complexity": 25, "max_depth": 2}
```

`audit.json` 的 `file_stats` 明写 **448**，且全文**从未出现 683**。报告的 683 是凭空多出来的，其自己的数据不支持它。

#### (3)「CC 94 / 72，radon 口径」→ 数字对，**口径标签错**

`audit.json` 确有此值：

```json
{"file":"cpt/web/app.py","name":"make_handler","lines":332,"complexity":94,"depth":12}
{"file":"cpt/web/app.py","name":"do_GET","qual":"make_handler<locals>.DashboardHandler.do_GET","lines":207,"complexity":72,"depth":12}
```

但 `radon 6.0.1` 实测**不是这个口径**：

| 度量 | 报告/audit.json | radon 6.0.1 | mccabe |
|---|---:|---:|---:|
| `make_handler`（闭包整体） | 94 | **1**（grade A，radon 不下钻函数内嵌套类，`do_GET` 根本不出现） | **84** |
| `do_GET` | 72 | **62**（grade F） | **56** |

**我找到了差值的确切来源**：把类体抽出后，`do_GET` 区间（74–280 行）内 `and`/`or` 关键字共 **16 个**，而 `mccabe(56) + 16 = 72` **精确吻合**。

→ 该自研分析器的口径 = **mccabe 基数 + 每个 `and`/`or` 记 1**；radon 按 **BoolOp 节点**计数（`a and b and c` 只算 1 个节点），在 `do_GET` 里只加 6 → 62。`make_handler` 的 94 则是把嵌套方法折进父函数后的汇总值（radon 不做这个汇总）。

**结论**：94/72 是「自研分析器（mccabe 基数 + 逐 boolop 计分）」的输出，**不是 radon 口径**。报告把标签写成「radon 口径」是错的。写进工单验收标准时**必须换算**：`mccabe --min 10` 下 `make_handler` ≤ 10，或 `radon cc -n C` 零命中。

---

## 2. ✅ 仍完全成立的核心结论

1. **§0 行数表 9/9 精确**，`layer_loc` 与实测逐项相等。
2. **§1 安全五项全过**（无密钥、无 `verify=False`、无 `shell=True`、无 `eval`、3 处 SQL f-string 全是常量拼接）——`security` 字段 14 条已逐条核对，报告正确区分了生产与测试。
3. **§3.1 `engine/`、`storage/` 孤儿层**：`unwired_modules` 确认，实测 `import cpt.web.__main__` 后 `cpt.engine.*`/`cpt.storage.*` 零加载。
4. **§3.3 `signal.py`、`a_share_rules.py` 零生产导入**：确认（`unused_funcs` 标 `test-only`）。
5. **§3.5 `recursion.py:138` 是 TYPE_CHECKING 编译期断言、不应删**：报告**正确推翻了工具**的 `no-refs-anywhere` 标注。
6. **§4.5 五行坏味道行号全对**（`smells` 5 条一一对应）。
7. **§5 全部 27 组同名函数的行号与漂移描述全对**，`_as_float` 的严格/宽松漂移表逐条读码验证通过。
8. **§6 M7 对账全对**：A1（`cpt/llm` 已删）、A2（`dashboard.js` 无 `/api/dashboard/parity` fetch，用词精确——parity 渲染逻辑仍在 :975-1054）、A4（无 `replaced`）已修；A3/B1/B2 遗留。
9. **§2.4 `dashboard.js` 指标全复现**（3,186 行、147 函数、`renderChrome` 168-169 行等，差 1 为计数口径）。

---

## 3. ⚠️ 仍成立的问题（补交件未改变的部分）

### 3.1 §0 死代码总量「≈1,450+ 行」低估 → 全口径 **≈2,353 行**

`audit.json` 未提供死代码行数汇总字段，报告手写。实际：

| 组成 | 行数 |
|---|---:|
| `cpt/engine`（realtime 373 + rebuild 442 + `__init__` 1） | 816 |
| `cpt/storage`（repository 479 + **models 156** + `__init__` 1） | 636 |
| `cpt/domain/signal.py` | 309 |
| `cpt/domain/a_share_rules.py` | 148 |
| 14 个 `dashboard_*` | 444 |
| **合计** | **2,353** |

（§3.1 单独说 engine+storage「约 1,450 行」是**对的**；错的是 §0 把全部死代码也标成 1,450+。）

**报告漏项**：`cpt/storage/models.py`（156 行）的唯一导入方是已死的 `repository.py:21`，也是传递性死代码，§3.1 未点。

### 3.2 §2.1「`.importlinter` 声明 `domain → engine → application → adapters → storage → web` 分层契约」→ **不存在**

`.importlinter` 实际只有 **4 条 `type = forbidden`** 契约，无任何 `type = layers` 契约，也无 `cpt.application` / `cpt.web` 的层约束。§3.1/§3.4 说「`.importlinter` 把它们声明为架构层」也不准 —— engine/storage 是作为 forbidden 的**目标/来源**出现。删这两层时要同步处理契约 3（`storage-isolated-from-application`）、4（`engine-orchestrates-domain-and-storage-only`），否则变成空契约。

### 3.3 §5.4「import 块重复 → 收敛到共享模块统一导出」→ 伪问题，建议删除

`dup_blocks` 确实命中 `export.py:17 ↔ replay.py:60`，但命中内容是：

```python
from cpt.domain.config import RulesConfig
from cpt.domain.models import (Bi, CanonicalBar, Fractal, Signal, StructureEvent, ...)
```

这是**领域类型的多行 import 列表**，被 token 级块哈希检测器命中，不是重复逻辑。为消除 stdlib/领域类型 import 的「重复」而建再导出模块，只会增加耦合与循环导入风险。**该条无收益，建议从报告删除。**

### 3.4 §4.4 `canvas_wbt.py` 拆分目标要按 448 行重算（报告写 683）

拆分方案本身（`_BODY_RE/_SCRIPT_RE/_CDN_RE` + `extract_body_fragment` 一组、`build_canvas_d_payload` + `_stamp` 一组）结构判断正确：`file_stats` 显示 `n_functions 10`、`max_fn_lines 171`（即 `build_canvas_d_payload`）。但「683 → 单模块 < 350」的目标改为「448 → 单模块 < 250」更合适。

### 3.5 §4.3「`contain.py` 等 domain 长函数 60+」→ `contain.py` 最长仅 **37 行**

`audit.json` 的 `long_functions` 里 domain 类只有 `signal.py:167 assess_first_buy`（89 行，死模块）与 `trend_type.py:163 classify_trend`（84 行，活）。`contain.py` 15 个函数最长 37 行，**未进任何长函数榜**。举例错误。

### 3.6 修复建议的三处坑（照做会出事）

| # | 报告建议 | 实测问题 |
|---|---|---|
| a | §5.5 `upsert_factor_rows`「script 版是 adapter 版的复制，直接 import 删本地副本」 | **实为已漂移的两个不同实现**：签名（`dry_run: bool` vs `source_url=None`）、SQL 列集（**9 列**含 `source_ref` vs **8 列**）、`dry_run` 干跑语义**只有 script 版有**。直接删会丢 `dry_run` 并改写入列集。正确做法：先扩 adapter 版支持 `dry_run` + 可选 `source_ref`，再合并。 |
| b | §4.1 `_ROUTES` 路由表化骨架 | **会拍平 A 股路由「先于 provider 分发」的短路**（`cpt/web/app.py:77-82` 有明确注释：否则每次 A 股请求白建一次加密快照，且加密侧不可达时 A 股路由永远走不到）。这是报告里**最危险的一条建议**，属功能性回归。 |
| c | §4.5 `a_share_snapshot.py:342`、`wind_source.py:346`「无注释说明为何安全」 | **两行都已有解释性注释**：`pass  # 正是要补的情形`、`except OSError:  # 记账失败不能影响取数`。只有 `a_share_local.py:340` 那条「无任何记录」属实。 |

### 3.7 报告漏项（4 条）

1. **CI 已有 vulture 死代码门禁，但阈值让它变成摆设** —— `.github/workflows/ci.yml` 末步 `vulture cpt --min-confidence 80 --exclude cpt/web/app.py`。实测 exit 0、零输出；降到 60% 则报 74 条。**vulture 把「未使用函数/类」定为 60% 置信度，`--min-confidence 80` 恰好全部过滤**，只剩未使用 import（90%）。这正是 14 个死模块长期存活的**根因**，报告 §5.6 建议「CI 引入护栏」时**没发现护栏已存在、只是阈值错了**。正确动作是降阈值 + 存量白名单，而不是新引入 `jscpd`/`xenon` 了事。
2. **`cpt/storage/models.py`（156 行）** 同样是传递性死代码。
3. **`DAILY_INTERVAL_MS` 有 3 份**（`a_share_public.py:60`、`wind_source.py:69`、`validators.py:63` 的 `A_SHARE_DAILY_INTERVAL_MS`），§5.4 说「两个适配器各定义一次」漏了第三份。
4. **`dup_blocks` 里报告没提的两处生产重复**：`ReferenceChanlunConfig(...)` 构造块（`multi_level.py:48 ↔ replay.py:180`，两个都活）、dataclass 字段块（`contain.py:105 ↔ models.py:62`）。另 `js_dup_names` 命中 `render`（4 个 canvas 文件）与 `q`（2 个文件），§5 未覆盖 JS 侧重复。

---

## 4. 核实后的行动清单

### P0（证据充分，可直接派工）
1. **合并 `~/.dbconfig` 三胞胎** → `cpt/adapters/_dbconfig.py`。报告方案正确；注意三份**并非逐字节相同**（`a_share_local` 用模块级 `_DB_CONFIG_FILE` + 长 docstring；`a_share_pool` 在函数内 `p = Path.home()/".dbconfig"` 且无 docstring；`factor_backfill` 用 `DB_CONFIG_FILE`），合并后 `a_share_local` 需自行追加 `sslrootcert`（`~/global-bundle.pem`）。
2. **处置孤儿层与死模块**：`engine/`(816) + `storage/`(792，含 models.py) + `signal.py`(309) + `a_share_rules.py`(148) + **14 个**（非 16）`dashboard_*`(444) = **2,353 行**。先删 `engine/realtime.py`。同步处理 `.importlinter` 契约 3/4。
3. **修 CI 死代码门禁阈值**：`--min-confidence 80` → `60` + 存量白名单。**报告没写，但这是防复发的关键。**

### P1
4. 收敛 `_as_float` / `_resolve_level` / `_infer_interval_ms` / `_bar_to_dict` / `CanonicalBar` 工厂（核实全部成立）。`_as_float` 统一是**有意行为变更**（ccxt/a_share 变严，拒 bool/NaN/Inf），需 PR 标注 + 全量测试。
5. `make_handler` 路由表化：①**必须保留 A 股路由先于 provider 分发的短路**；②验收阈值改用 **mccabe** 口径（`make_handler` ≤ 10、无单函数 > 15），不要沿用 94/72。
6. `upsert_factor_rows`：**先扩 adapter 版支持 `dry_run` + 可选 `source_ref`，再合并**。

### P2
7. `canvas_wbt.py` 二分（按 **448** 行算）。
8. 提取 `MarketDataSource` Protocol。
9. `except-pass` 只修 `a_share_local.py:340`（另两处已有注释）。
10. 修正报告正文三处数字（16→14、683→448、删「radon 口径」标签）后归档，`audit.json` 作为附录产物一并入库。

---

## 5. 踩坑记录

1. **原始数据对、正文错** —— 这次是报告作者**抄错自己的工具输出**：`audit.json` 写 14/448/94，正文写成 16/683/94。教训：**核报告时要先看它的原始产物，再判「工具错」还是「转录错」**；我 v1 因原始产物未交付，只能判「不可复算」，v2 补交后定性更准。
2. **`radon` 不下钻「函数内嵌套类」的方法** —— 对 `make_handler` 直接报 CC **1**，`do_GET` 根本不出现。测这类代码必须用 `mccabe`（会把嵌套折进父函数）或把类体抽出单独跑。
3. **自研 CC 分析器的口径要问清楚** —— 本例 = mccabe 基数 + **逐个 `and`/`or` 关键字**计 1；radon 按 **BoolOp 节点**计。同一个 `do_GET`：自研 72 / radon 62 / mccabe 56。**跨工具比较复杂度前必须对齐口径**，否则验收阈值形同虚设。
4. **`vulture --min-confidence 80` 静默过滤掉所有「未使用函数」(60%)** —— 门禁看着在跑、exit 0，实际对死函数零效力。判断「有没有死代码门禁」不能只看 CI 里有没有 vulture，**必须看阈值**。
5. **`grimp.find_descendants("cpt.web.__main__")` 返回 0**（`__main__` 节点特殊），`find_modules_directly_imported_by` 才准。生产可达性最稳的判据是**实测** `import cpt.web.__main__` 后读 `sys.modules`。
6. **报告「修复建议」的坑比「问题发现」的坑更隐蔽**：`upsert_factor_rows` 直接删副本会丢 `dry_run`；路由表化拍平 A 股前置分发会造成功能性回归。**建议落地前必须读原码验证，不能照抄骨架。**
7. **token 级块哈希检测器会产生 import 列表类伪重复** —— `dup_blocks` 命中的 `export.py:17 ↔ replay.py:60` 是多行领域类型 import，不是逻辑重复。对 `dup_blocks` 的结论要逐条人工判读，不能直接进工单。

---

## 附：可复现命令

```bash
cd /home/ubuntu/work/Chan_Pattern_Trader
F=/home/ubuntu/.dsh/attachments/v1/files/f1/f14a6cb982790649ad2cd83803c04b881a116ed928468ccaa6240d7c4f6f901f/audit.json

# 原始数据自证：14 个死模块、448 行、683 零出现
python3 -c "import json;d=json.load(open('$F'));print(len([m for m in d['unwired_modules'] if 'dashboard_' in m['module']]))"
python3 -c "import json;d=json.load(open('$F'));print([f for f in d['file_stats'] if 'canvas_wbt' in f['file']])"
grep -o "683" $F | wc -l

# 生产可达性（最权威）
.venv/bin/python -c "import sys, cpt.web.__main__; print(sorted(m for m in sys.modules if m.startswith('cpt')))"

# 死模块交叉验证（CI 同款工具，降阈值）
.venv/bin/python -m vulture cpt --min-confidence 60 --exclude 'cpt/web/app.py'

# 圈复杂度三口径对比
python3 -m pip install --target /tmp/radonlib radon
PYTHONPATH=/tmp/radonlib python3 -m radon cc cpt/web/app.py -s
python3 -m pip install --target /tmp/mccabelib mccabe
PYTHONPATH=/tmp/mccabelib python3 -m mccabe --min 10 cpt/web/app.py
awk 'NR>=74 && NR<=280' cpt/web/app.py | grep -oE "\b(and|or)\b" | wc -l   # → 16，即 56+16=72
```
