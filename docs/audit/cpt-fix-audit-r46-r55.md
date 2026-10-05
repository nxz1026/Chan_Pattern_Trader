# Chan_Pattern_Trader (CPT) 审计报告 · 修复补记（R46）

- **审计对象**：`ubuntu@140.83.62.161:/home/ubuntu/DSH/Chan_Pattern_Trader`
- **修复分支**：`fix/p0-gates`（基线 `68c7557a6`），本地提交 `efbb45eff`，**未 push**
- **日期**：2026-10-05
- **本轮范围**：仅修复上轮审计报告标出的三处 **P0 门禁违规**。未触碰 P1（mypy/ruff 宽面）与凭据副本，留待另行确认。
- **一句话结论**：三处 P0 全部修复，两道此前为红的门禁（分层契约、SQL 越层）**实测转绿**，文档计数同步，无新增回归；全量测试仅剩基线上那 2 个 chromium 环境失败。

> 风险声明：本项目及其缠论信号（含一买）仅供技术研究，不构成任何投资建议。金融市场存在风险，历史表现不代表未来收益。本报告为代码仓库技术审计，非投资分析。

---

## 1. 修复清单

| # | 缺陷 | 改法 | 落点 |
|---|---|---|---|
| P0-1 | 分层契约 BROKEN：`cpt/adapters/reference_backend.py` 反向 import `cpt.application` | 新建 `cpt/adapters/reference_pipeline.py`，把 `compute_domain_structures` / `tencent_structures` / `default_backend` / `to_ref_config` / `normalize_structures` 从 application **下沉到 adapters**；`replay.py` 保留 re-export 兼容既有 import 点 | 新增 1 文件；改 4 文件 |
| P0-2 | SQL 越层：`cpt/web/a_share_routes.py:330` 直接写 SQL | 新增 `cpt/adapters/a_share_local.fetch_latest_raw_close()`，web 层 `_raw_close` 改为委派，不再出现 SQL | 改 2 文件 |
| P0-3 | 运行时 parity 恒崩溃：`'Fractal' object has no attribute 'get'` | `reference_backend` 侧返回的**领域对象**在进 `build_parity_snapshot`（只吃 dict）前统一过 `_normalize`，与 `cpt_side` 同口径 | 改 1 文件 |
| 附带 | 文档计数漂移（adapters 20→21 文件等） | 同步 `docs/architecture.md §2.1` 六层「文件/行数」 | 改 1 文件 |

## 2. diff 概览（`git diff --stat 68c7557a6 HEAD`）

```
 cpt/adapters/a_share_local.py       |  27 +++++
 cpt/adapters/reference_backend.py   |  14 +--
 cpt/adapters/reference_pipeline.py  | 204 ++++++++++++++++++++++++++++++++++++
 cpt/application/parity_reference.py |  75 ++-----------
 cpt/application/replay.py           |  85 ++-------------
 cpt/web/a_share_routes.py           |  75 ++++++++-----
 docs/architecture.md                |   8 +-
 7 files changed, 304 insertions(+), 184 deletions(-)
```

迁移保真：新模块函数体由 **AST 从原文件逐字抽取**，非手抄；`cpt.application.replay` 仍 re-export `compute_domain_structures`，`a_share_snapshot` / `web.__main__` 等既有 import 点零改动。

## 3. 门禁前后对照（均以仓库自带 `.venv` 实测）

| # | 门禁 | 修复前 | 修复后 |
|---|---|---|---|
| 1 | `pytest tests` | 2 失败（chromium 环境） | 2 失败（**同基线**，chromium SIGTRAP 环境问题） |
| 2 | `ruff check cpt tests scripts` | 66 errors | 64 errors（**↓2**，格式化顺带修掉原有 E501/I001；无新增） |
| 3 | `ruff format --check` | 55 待格式化 | 53 待格式化（改动文件已格式化；无新增） |
| 4 | `mypy cpt scripts` | 26 errors | 26 errors（**无新增**；re-export 相关报错已消） |
| **5** | **`lint-imports`** | **BROKEN（5 kept, 1 broken）** | **✅ `Layered architecture KEPT`（6 kept, 0 broken）** |
| **6** | **`check_sql_layering`** | **FAIL（web 越层）** | **✅ 通过（扫描 62 文件）** |
| 7 | `vulture --min-confidence 60` | 3 findings | 3 findings（无变化） |
| 8 | `check_doc_drift` | ✅ | ✅（计数同步后） |

**专项测试**：`-k "replay or parity or reference or backend or a_share"` → 全通过（其中 `test_replay_integration` 31 项）。

## 4. 迁移过程中的自纠（透明记录）

首次改动引入了 3 处**我自己造成**的副作用，已在同一轮内修复，未带入提交：

1. 装配脚本沿用了原私有名（`_default_backend` / `_to_ref_config` / `_tencent_structures`），导致 `reference_backend` 调不到 → 改名为公开名。
2. 删函数后 `run_replay` 内部仍调用 `_default_backend`/`_to_ref_config` → 在 `replay.py` 补别名导入。
3. 从「定义」变「re-export」后 mypy 报「未显式导出」→ 把 `compute_domain_structures` 加入 `replay.__all__`。
4. 新文件未格式化 + 文档计数 → 格式化改动文件、重算并回填 §2.1。

> 这几处若不做，就会把原本的绿灯（ruff/mypy/doc_drift）搞红——等于用一个修复换三个新问题。

## 5. 遗留（未处理，需你确认）

| 项 | 说明 | 建议 |
|---|---|---|
| mypy 26 / ruff 64 / format 53 | 均为**改动前既有**，非本次引入 | 单独开一轮统一治理，勿与本修复混提 |
| vulture 3 findings | `_from_normalized` / `_czcs_structures` / `Action` 未使用 | 确认是死代码还是 whitelist 漏项 |
| 4 份明文凭据副本 | `deploy/env/*.env.bak.*`（未入库） | 收敛为 1 份、清理 bak |
| `运行索引行缺 run_id` WARNING | 线上日志 | 排查 `dashboard_run_store` 是否静默丢行 |
| chromium 测试失败 | 环境 SIGTRAP | 装图形/沙箱依赖或标记 xfail |

## 6. 复现命令

```bash
cd /home/ubuntu/DSH/Chan_Pattern_Trader
git checkout fix/p0-gates && git log --oneline -1   # efbb45eff
.venv/bin/lint-imports                                # 6 kept, 0 broken
.venv/bin/python scripts/check_sql_layering.py        # ✓
.venv/bin/python scripts/check_doc_drift.py           # ✅ 未发现漂移
.venv/bin/python -m pytest tests -q                   # 仅 2 chromium 环境失败
```

**边界**：本修复**未动**运行时服务（`cpt-dashboard.service` 仍在跑旧代码），也**未 push**。上线需你确认后 `git push` + 重启服务。

---

# P1 补记（R47）：质量门禁全绿

- **提交**：`fe9bd32a6 fix(R47): P1 质量门禁全绿 + 文档锚点同步`（分支 `fix/p0-gates`，**未 push**）
- **日期**：2026-10-05
- **范围**：修复上轮遗留的 P1 —— ruff check / ruff format / mypy / vulture 四道**静态质量门禁**，并同步受影响的文档锚点。

## 1. 修复清单

| 门禁 | 修复前 | 改法 | 落点 |
|---|---|---|---|
| `ruff check cpt tests scripts` | 6 errors（E741×2 + E501×4） | 变量名 `l`→`lines`/`lo`；两处超长行拆行 | `check_doc_counts.py`、`compare_chanlun_backends.py`；`selftest_gates.py`、`verify_public_contracts.py` 的 `E501` 折行 |
| `ruff format --check` | 53 文件待格式化 | 先 `ruff format` 批量规范化（本已在工作区），再把手写豁免行改回 ruff 折叠形状 | 全仓 + `factor_recompute.py` |
| `mypy --strict cpt scripts` | 26 errors（10 文件） | 补 `Any`/`Callable`/`ModuleType` 注解；`None` 收窄；`cast(ReferenceChanlunBackend, ...)`；三元组解包改 `Sequence[Any]` 返回 | `check_all_claims.py`、`check_doc_counts.py`、`selftest_gates.py`、`compare_chanlun_backends.py`、`check_storage_failure_semantics.py`、`factor_epoch_store.py`、`factor_recompute.py`、`reference_backend.py`、`parity_reference.py`、`a_share_routes.py` |
| `vulture --min-confidence 60` | 3 findings | 删 3 个**真死符号**（`_from_normalized`、`_czsc_structures`、`Action` 别名） | `reference_backend.py`、`parity_reference.py`、`recommendation.py` |

> vulture 白名单文件 `whitelist.py` 自带注释明确「**真死代码不进本文件**」——故此处按项目策略**删除**而非登记白名单。

## 2. 连带修复（同一轮内，避免「修一个红三个」）

| 门禁 | 触发原因 | 改法 |
|---|---|---|
| `check_all_claims`（CI） | 删死符号后 `parity_reference.py` 由 172→146 行，`docs/pending-wiring.md` 的 `parity_reference.py:155` 锚点越界 | 该行号更新为 `:61`（惰性 import `build_parity_snapshot` 的真实位置） |
| `check_doc_counts`（CI） | P0 重构改动了各层行数 | `docs/architecture.md §2.1` 刷新 4 层：storage 1,839→1,858、adapters 6,242→6,186、application 5,578→5,553、web 3,139→3,141 |

## 3. 门禁结果（仓库自带 `.venv` 实测）

| 门禁 | 结果 |
|---|---|
| `ruff check cpt tests scripts` | ✅ All checks passed |
| `ruff format --check cpt tests scripts` | ✅ 232 files already formatted |
| `mypy cpt scripts` | ✅ Success: no issues found in 111 source files |
| `vulture --min-confidence 60 cpt whitelist.py` | ✅ exit 0 |
| `lint-imports` | ✅ 6 kept, 0 broken（P0 成果保持） |
| `check_sql_layering.py` | ✅ 扫描 62 文件 |
| `check_doc_counts.py` | ✅ §2.1 与实际一致 |
| `check_all_claims.py` | ✅ 全部断言对得上 |
| `check_doc_drift.py` | ✅ 未发现漂移 |
| `selftest_gates.py` | ✅ 全部门禁都能抓到各自的错例 |
| `check_enqueue_skeleton_unique.py` | ✅ |
| `check_job_poll_unique.py` | ✅（只报告不阻断） |
| `build_dashboard_bundle.py --check` | ✅ bundle 与源文件同步 |
| `pytest tests` | 1099 passed / 2 failed / 8 skipped |

> **2 failed** = `tests/test_dashboard_chromium_interactions.py` 的 chromium headless SIGTRAP：本机 chromium 二进制在该沙箱环境崩溃，属**环境级**，与本次改动无关，与基线一致。

## 4. 迁移过程中的自纠（透明记录）

首次补丁引入了 2 处**我自己造成**的副作用，已在同一轮内修复、未带入提交：
1. `compare_chanlun_backends.py` 把循环变量 `l` 改名为 `lo`，但函数体内 `float(l)` 漏改 → 已统一为 `float(lo)`。
2. `factor_recompute.py` 手写拆行后单行 103 字符触发 `E501` → 改回 ruff 规范折叠形状。

## 5. 复现命令

```bash
cd /home/ubuntu/DSH/Chan_Pattern_Trader
git checkout fix/p0-gates && git log --oneline -2   # fe9bd32a6 (P1) / efbb45eff (P0)
.venv/bin/ruff check cpt tests scripts              # All checks passed
.venv/bin/ruff format --check cpt tests scripts     # 232 already formatted
.venv/bin/mypy cpt scripts                          # no issues in 111 files
.venv/bin/vulture --min-confidence 60 cpt whitelist.py
.venv/bin/lint-imports                              # 6 kept, 0 broken
.venv/bin/python scripts/check_doc_counts.py        # ✅ §2.1 一致
.venv/bin/python scripts/check_all_claims.py        # ✅ 全部断言对得上
.venv/bin/python -m pytest tests -q                 # 仅 2 chromium 环境失败
```

## 6. 更新后的遗留（仍未处理，需你确认）

| 项 | 说明 | 建议 |
|---|---|---|
| 4 份明文凭据副本 | `deploy/env/*.env.bak.*`（未入库） | 收敛为 1 份、清理 bak |
| `运行索引行缺 run_id` WARNING | 线上日志 | 排查 `dashboard_run_store` 是否静默丢行 |
| chromium 测试失败 | 环境 SIGTRAP | 装图形/沙箱依赖或标记 xfail |
| **P0+P1 提交未 push** | 线上仍跑旧代码 | 确认后 `git push` + `systemctl restart cpt-dashboard.service` |

> 说明：上轮列出的「mypy 26 / ruff 64 / format 53 / vulture 3」**本轮已全部清零**；P1 已完成。

---

# P2 补记（R48）：运维卫生 + 测试环境适配（已上线）

- **提交**：`655025f3f fix(R48): P2 运维卫生 + 测试环境适配`（分支 **main**，已 push 到 GitHub，`git ls-remote` 逐字比对一致）
- **日期**：2026-10-05
- **分支管理**：本轮应用户要求完成主干化——main fast-forward 合并 `fix/p0-gates`（`3a9edf263..fe9bd32a6`，含 P0+P1），push 后远端 main 与本地逐字一致；本地分支 `fix/p0-gates` 已删除（已 merged），GitHub 远端无同名分支（未 push 过），无需远删。**当前 main HEAD = `655025f3f`，P0+P1+P2 全部在主干并在线运行。**

## 1. P2 修复清单

| 项 | 审计定级 | 处理 | 结果 |
|---|---|---|---|
| `deploy/env/` 3 个 `.env.bak.*` 明文凭据副本 | P2 安全卫生 | **删除** `bak.1790855459 / .1790913046 / .1790937871`，保留单一 `cpt-dashboard.env` | ✅ systemd `EnvironmentFile` 只引用 `.env`；3 个 bak 键集是其子集，无配置丢失；均未入库（gitignore 覆盖） |
| `.env.example` 模板密钥字段 | （衍生核查） | 逐键核验：`CPT_LLM_API_KEY` 等密钥字段均为 EMPTY，仅端口/主机等非敏感配置有真实值 | ✅ 无入库泄漏面 |
| 「运行索引行缺 run_id」WARNING | P2 运行时 | **分级**：占位快照（`runtime.status="empty"`，DB 真空/缺因子时返回，无 `reproducibility` 可哈希）跳过持久化属**设计路径** → 降级 `debug`；真异常快照缺身份仍 `warning`（附 `status`/`bar_count` 便于诊断）。落点 `cpt/storage/dashboard_run_store.py::upsert_run` | ✅ 由 2 条新回归测试钉死（`tests/test_dashboard_runs_persisted.py`，FakeConn 内存假库直测：占位不产 WARNING / 真异常仍产 WARNING） |
| ruff 版本漂移 0.16.9 vs 锁 0.16.8 | P2 工具链 | 核验 `.venv/bin/ruff --version` = **0.16.8** == `requirements-dev.txt:82` | ✅ 无需动作（此前已对齐） |
| 2 个 chromium 测试 SIGTRAP | P3 环境 | `tests/conftest.py` 新增 `chromium_runnable()` **实跑探针**（加载真实 `dashboard/index.html?mode=watch`，进程级缓存），替代仅验二进制存在的 `chromium_path()` skipif | ✅ 探针判定本机不可运行 → 2 用例干净 skip（带明确原因），不再红 |

**run_id 根因定性**（如实记录）：`build_run_index` 的索引行 `run_id = runtime.run_id or reproducibility.dataset_hash`；真实快照总有 `dataset_hash` 可持久化，只有「无数据可哈希」的占位快照天然无身份。**这不是静默丢真实数据行**，问题只是日志级别把预期路径误报成异常。

**chromium 根因定性**：本机 `chromium` 是指向 Playwright chromium-1243（aarch64）的符号链接；`--dump-dom about:blank` 能跑通（exit 0），但加载真实页面时 V8 **SIGTRAP（exit 133）**，stderr 除 crashpad 噪音外无 FATAL/Check 栈——属环境缺 GUI/沙箱能力，**非 repo 内代码可修**，故用探针适配而非装依赖硬修。若日后装好依赖，探针会自动检测并恢复运行这两个用例。

## 2. 提交时门禁（13 道全绿）

`ruff check` ✅ / `ruff format --check`（232）✅ / `mypy`（111 文件）✅ / `vulture` ✅ / `lint-imports`（6 kept 0 broken）✅ / `check_sql_layering` ✅ / `check_doc_counts` ✅ / `check_all_claims` ✅ / `check_doc_drift` ✅ / `selftest_gates` ✅ / `check_enqueue_skeleton_unique` ✅ / `check_job_poll_unique` ✅ / `build_dashboard_bundle --check` ✅ / **pytest：1101 passed / 0 failed / 10 skipped / 75.7s**（2 个 chromium 按环境 skip）

> ⚠️ **本节原写「14 道全绿」是错的 —— 清单漏了 `check_storage_failure_semantics`**（CI `ci.yml:67` 确有这道）。
> R49 复核实测发现它当时就是**红的**，见文末 R49 补记。

连带文档同步：`docs/architecture.md §2.1` storage 行数 1,858→1,867（跟随 store +9 行），`check_doc_counts`/`check_doc_drift` 复绿。

## 3. 上线验证（重启后实测）

| 检查 | 结果 |
|---|---|
| `systemctl restart cpt-dashboard.service` | ✅ 成功，新 PID 2628998，`active`，监听 `127.0.0.1:8010` |
| `GET /api/dashboard/health` | ✅ HTTP 200，`{ok:true, degraded:false, stale:false, consecutive_failures:0}` |
| `GET /api/dashboard/runs` | ✅ HTTP 200，真实快照均带 run_id/dataset_hash 正常入库（佐证占位跳过是设计路径） |
| `GET /api/dashboard/engine-state` | ✅ 200 |
| 启动日志 | ✅ 无新 WARNING/ERROR；HEAD = `655025f3f` |
| 「缺 run_id」WARNING 线上对照 | 重启前 10 分钟 0 条 / 重启后 90 秒 0 条——占位路径间歇触发，观察窗内未命中；**行为修复以回归测试为准，此对照不作为增量证据** |
| parity 对照失败 | 重启前后观察窗均 0 条（该路径由请求触发，窗口内未命中）；P0-3 修复的线上效果建议后续使用中持续观察 |

## 4. 迁移自纠记录（透明）

P2 过程中两次自引入问题均在提交前拦下修复，未带入提交：
1. `conftest.py` 行切片误切出 `chromium_runnable` 双份定义 + `import subprocess` 漏写（F811/F821×4）→ 整段去重重写，MD5 校验回传。
2. 初版探针用 `about:blank` 会误报「可运行」（本机 about:blank 能跑、真实页面才崩）→ 探针改为加载真实 `index.html`。

## 5. 三轮修复总结（P0+P1+P2 全部上线）

| 轮次 | 提交 | 内容 | 状态 |
|---|---|---|---|
| P0 | `efbb45eff` | 分层契约 / SQL 越层 / parity 崩溃 | ✅ 主干 + 在线 |
| P1 | `fe9bd32a6` | ruff/format/mypy/vulture 四道静态门禁清零 + 文档锚点 | ✅ 主干 + 在线 |
| P2 | `655025f3f` | 凭据副本清理 / run_id WARNING 分级 / chromium 探针适配 | ✅ 主干 + 在线 |

**最终状态**：main 与 GitHub 远端逐字一致（`655025f3f`），工作区干净，14 道门禁全绿，pytest 0 失败，线上服务健康（health 200 / 无降级 / 无新告警）。

## 6. 遗留与安全提醒

- **SSH 私钥轮换（强烈建议）**：该私钥已在本会话中明文出现两次，无论是否泄露都应视为已暴露，请尽快在 Oracle 主机上轮换 `authorized_keys`。
- chromium 测试在本环境 skip；装好 GUI/沙箱依赖后探针自动恢复。
- parity 修复的线上效果（A股个股页触发）建议日常使用中留意 `journalctl -u cpt-dashboard.service | grep parity`。

---

# R49 补记：门禁清单缺项 + 2 个基线失败

- **日期**：2026-10-05 · **范围**：复核 R46~R48 三轮的门禁声明，**未改业务逻辑**
- **基线**：`655025f3f`（main，与 GitHub 远端逐字一致）· **本地改动 2 文件，未 commit**

## 1. 复核结论

R46~R48 报告的绝大多数门禁声明**属实**（实跑复验），但有一处实质性缺口：

| 项 | 报告声明 | 实测 | 判定 |
|---|---|---|---|
| `check_storage_failure_semantics` | **未列入** 14 道清单 | exit=1（红） | **报告漏项 + 门禁当时就是红的** |
| 其余 12 道静态门禁 | 全绿 | 全绿 | 属实 |
| pytest | 1101 passed / **0 failed** | 2 failed / 10 skipped | 与报告时点不同（见第3节） |

## 2. 根因与修法（R46 引入）

`cpt/adapters/a_share_local.py::fetch_latest_raw_close` —— R46（P0-2）把 SQL 从 web 层下沉时带进来的：

- 判据命中：函数执行了 `cur.execute(...)`，`except` 分支 `return None` 不 raise，判为「吞掉 DB 异常」。
- 门禁担心的真实风险：PG 里「事务中一条语句失败，同连接后续全部 `current transaction is aborted`」，吞掉不是降级而是放大。
- **修法**：按 `cpt/storage/` 三处现有惯例加 `# gate: allow-silent:` 豁免（`structure_event_store.py:79` / `llm_call_store.py:269` / `run_metric_store.py:172`），并在 docstring 里写清调用方如何知情：
  - 调用点 `cpt/web/a_share_routes.py:154` 的消费方显式处理 `None`（`:338` `rec.get("raw_close") if ... is not None else rec.get("price")`），`None` 是**被处理的正常分支**；
  - 本函数 `with psycopg.connect(...)` **自开连接、用完即弃**，不复用调用方连接，吞掉不会污染他人事务。
- 连带：`docs/architecture.md` §2.1 adapters 行数 6,186 → **6,194**（+8 行豁免注释），`check_doc_counts` / `check_doc_drift` 复绿。

## 3. 2 个 pytest 失败：初判「基线问题」—— ❌ 已于 R50 推翻

> **⚠️ 本节定性是错的，保留原文供追溯，正确结论见 [R50 补记](#r50-补记两个失败的真实根因)。**
> 当时用 `git stash` 摘掉改动后重跑仍红，就下了「与本轮无关」+「环境依赖型」的结论。
> 那个实验只能证明**不是本轮引入**，证不出**是环境差异**。两个用例的真实根因都是
> **测试自身的沙箱/判据缺陷**，与本机环境是否装了 Wind、node 版本无关。

| 用例 | 当时的现象 | ~~当时定性~~ |
|---|---|---|
| `test_dashboard_url_credentials.py::test_safe_fetch_url_keeps_endpoint_when_unresolvable` | node `ERR_INVALID_URL` | ~~node v22 URL 解析差异~~ → **沙箱失真**（R45 重构后没注入 `url_safety.js`） |
| `test_factor_backfill_script.py::test_main_wind_fallback_degrades_when_wind_missing` | 拿到 `wind_basis_unknown` 而非 `wind_unavailable` | ~~本机 Wind 可用~~ → **判据依赖环境**（把「本机没装 Wind」当前提） |

## 4. 复验（改后全绿）

`ruff check` / `ruff format --check`（232）/ `mypy`（111 文件）/ `vulture` / `lint-imports`（6 kept 0 broken）/ `check_sql_layering` / **`check_storage_failure_semantics`（本轮由红转绿）** / `check_doc_counts` / `check_doc_drift` / `check_all_claims` / `selftest_gates` / `build_dashboard_bundle --check`

## 5. 复现命令

```bash
cd /home/ubuntu/DSH/Chan_Pattern_Trader
.venv/bin/python scripts/check_storage_failure_semantics.py   # 无吞掉 DB 异常的函数
MYPY_CACHE_DIR=/home/ubuntu/work/tmp-caches/mypy .venv/bin/mypy cpt scripts
.venv/bin/python -m pytest tests -q
```

> 环境坑两条：(1) `/tmp` 挂载点不支持 SQLite WAL，`MYPY_CACHE_DIR` 指向 `/tmp` 会让 mypy INTERNAL ERROR（`sqlite3.OperationalError: disk I/O error`），换工作区目录即好；(2) shell 重定向到 `/tmp/*.log` 会 exit=120 且日志 0 字节，脚本前台跑正常。

## 6. 待奎爷拍板（✅ 已于 R49 拍板并执行）

| 项 | 奎爷裁决（m00302「1+2都要」） | 结果 |
|---|---|---|
| 本轮 2 文件改动未 commit | 提交 + 推送 | ✅ `f7b0c5fe8`，已推 GitHub main |
| 2 个环境型 pytest 失败 | 改判据 | ✅ R50 修完，全量 0 failed |

---

# R50 补记：两个失败的真实根因

- **日期**：2026-10-05 · **范围**：修 R49 第 3 节那两个失败，并给新判据加防退化守卫
- **基线**：`655025f3f` → **提交 `f7b0c5fe8`**，已推远端 main（`ls-remote` 实测一致）

## 1. 根因一：node 用例的沙箱失真（与 node 版本无关）

R45（`d404da875`）把 `try/catch` 搬进 `dashboard/url_safety.js` 的 `urlObject()`，
`dashboard.js` 的 `resolveUrl`（`:3718`）/ `safeFetchUrl`（`:3746`）退化成薄委托：
`window.CPT_URL` 存在就用它，否则兜底跑无 try 的 `new URL(...)`。

测试的 `_helper_source()` 只抠这两个函数体 ⇒ 沙箱里 `window.CPT_URL` **恒为 undefined**
⇒ 走的正是浏览器里再也不会执行的那条兜底 ⇒ `new URL("http://", "https://h/cpt/")` 抛
`TypeError: Invalid URL` / `code: 'ERR_INVALID_URL'`。

- 历史核对：R44（`55f1d102c`）时代 `safeFetchUrl` 确实带 `try { ... } catch { return endpoint; }`，R45 才删。
- 实测 node v22 对 `"http://"`、`"http://["`、`"https://"` 一律 THROW，`"::::"` 反而 OK —— 所以问题不在解析器，在**测错了对象**。
- 修法：新增 `_url_safety_source()` 读真 `dashboard/url_safety.js` 注入沙箱，注入顺序 `window` → `url_safety.js` → `_helper_source()`。手工验证三个场景全对（`http://` 回原值、凭据被剥、`file://` 保持 file 协议）。

## 2. 根因二：Wind 用例把环境当前提

`test_main_wind_fallback_degrades_when_wind_missing` 原先**不注入任何 Wind 替身**，直接吃真实
`WindSourceClient.availability()`。本机三件套齐全（`~/.agents/skills/wind-mcp-skill/scripts/cli.mjs`
在、`~/.wind-aifinmarket/config` 在、node 可解析），`availability()` 返回 `(True, "")`，于是走进
R31 的 `check_wind_basis` 基准闸，产出 `wind_basis_unknown` —— 而用例要的是 `wind_unavailable`。

> 判据依赖环境 ⇒ **CI 换机器结果就变**。这是真缺陷，不是「本机环境不同」可以解释的。

修法（关键是**只钉环境前提，不伪造被测行为**）：

- `_cli_missing_init()` 只把 `WindSourceClient.__init__` 的 `cli_script` 钉到
  `/nonexistent/wind-mcp-skill/scripts/cli.mjs`；`availability()` → `call()` →
  `WindUnavailableError` **全走真实实现**。
- 新增镜像守卫 `test_main_wind_fallback_never_probes_wind_when_local_succeeds` ——
  上面那条把 CLI 指向不存在的文件来制造「不可用」，若哪天兜底判断写反了（无条件探一次
  availability、或本地成功时也去问），**只有这条能发现**。它同时 spy 住 `availability`：
  连探都不许探，因为真发一次 Wind 调用会消耗真实额度。
- 新增 `test_main_wind_fallback_degrades_under_real_availability` —— 不改 availability，
  断言 `rc == 0` 且日志含 `wind_` 前缀，**不绑具体档位**，让两种环境都能过。

## 3. 防退化：两条新判据都做过变异验证

| 变异 | 预期 | 实测 |
|---|---|---|
| 把 `_cli_missing_init(...)` 换成 `(lambda cli: None)(...)` | 变红 | ✅ 红：`Wind[wind_unexpected: TypeError: 'NoneType' object is not callable]` ≠ `wind_unavailable` |
| 让镜像守卫记录一次探测 | 变红 | ✅ 红 at `test_main_wind_fallback_never_probes_wind_when_local_succeeds` |

另给测试 1 加了 2 条结构守卫：`test_sandbox_actually_loads_url_safety`（沙箱必须真加载到
`url_safety.js`，防再静默失真）、`test_dashboard_js_thin_delegate_still_has_fallback`
（dashboard.js 兜底分支不得删 —— 它是 `url_safety.js` 缺席时唯一防线）。

## 4. 复验（全绿）

| 项 | 结果 |
|---|---|
| pytest | **0 failed**，4 skipped（ccxt 未装、chromium aarch64 SIGTRAP ×2、first_buy_bridge 该序列无两个中枢 —— 均非代码缺陷） |
| ruff / ruff format | `All checks passed!` / `235 files already formatted` |
| mypy `cpt scripts` | `Success: no issues found in 111 source files` |
| vulture（CI 原命令 `--min-confidence 60 cpt whitelist.py`） | exit 0 |
| lint-imports | `Contracts: 6 kept, 0 broken` |
| 其余 8 道脚本门禁 | 全绿（含 `check_storage_failure_semantics`） |

## 5. 迁移自纠记录

1. **替身形参遮蔽闭包变量**：`_cli_missing_init` 的内层 kw-only 形参原名 `cli_script`，
   同名遮蔽了闭包里的 `missing_cli`，`None` 分支回落到 `DEFAULT_CLI_SCRIPT`（本机真实存在的
   文件）⇒ availability 照样 True、**替身静默失效**。改名后修复，已在 docstring 记坑。
2. **我自己把测试文件写成 0 字节**：`cp tests/... /tmp/tfbs.bak` 撞 `/tmp` 配额（tmpfs 12G
   已用 9.4G，大户是 5 个 `/tmp/dsh-workspace-changes-*` 共 8.4G），备份 0 字节；随后
   `cp` 回来把 `tests/test_factor_backfill_script.py` 截成空文件。`git checkout --` 恢复后重打
   编辑。**教训：备份放工作区，不放 `/tmp`。**
3. **误判定性**：R49 第 3 节把两个失败说成「环境型、基线问题」。`git stash` 实验只能证明
   「不是本轮引入」，证不出「是环境差异」—— 这是推理跳跃，已在该节就地标注推翻。

## 6. 复现命令

```bash
cd /home/ubuntu/DSH/Chan_Pattern_Trader
.venv/bin/python -m pytest tests/test_dashboard_url_credentials.py tests/test_factor_backfill_script.py -q
.venv/bin/python -m pytest tests -q
MYPY_CACHE_DIR=/home/ubuntu/work/tmp-caches/mypy .venv/bin/mypy cpt scripts
.venv/bin/vulture --min-confidence 60 cpt whitelist.py
```

> 环境坑（沿用 R49 三条）：`/tmp` 不支持 SQLite WAL（`MYPY_CACHE_DIR` 指工作区）；
> shell 重定向到 `/tmp/*.log` 会 exit=120 且日志 0 字节（前台跑正常）；**`/tmp` 配额会被
> dsh 工作区快照占满，备份别放这儿**。

---

# R51：画布 D（wbt 报告视图）下线

> 奎爷裁决（m00653）：「**1.a**」= `/api/canvas/wbt` 端点**直接除名**（外部调用拿到
> 404 `not_found`，不做 410 墓碑）；「**2.删**」= `dashboard/dashboard.js` **一并删除**。
>
> 已提交并推送：**`cf8b2216d`**（远端 main 已核对逐字一致）。

## 1. 结论

画布 D 与 `/api/canvas/wbt` 已完全下线。**顺带根除了 CI 五连红**（见 §2），
并清掉一个自 R45 起就存在、本轮才被发现的部署 bug（见 §4）。

| 类别 | 文件 | 变化 |
|---|---|---|
| 服务端 | `cpt/application/canvas_wbt.py` | 删除 469 行 |
| 端点 | `cpt/web/app.py` | 删 `/api/canvas/wbt` 整个 elif 分支 → 落入 404 |
| 前端 | `dashboard/canvas_d.js` | 删除 436 行 |
| 前端 | `dashboard/dashboard.js` | 删除 4925 行（**死代码**，见 §4） |
| 前端 | `index.html` / `dashboard.css` / `canvas_registry.js` / `dash-chart.js` / `dash-core.js` / `url_safety.js` / `cpt_job.js` / `market_a_share.js` | 逐处去 D + R51 留档 |
| 依赖 | `pyproject.toml` | 删 `report` extra（`wbt==0.9.1`） |
| 测试 | `test_canvas_wbt.py`(215) / `test_canvas_d_trust_boundary.py`(249) / `test_web_canvas_wbt.py`(188) / `test_canvas_wbt_cdn_guard.py`(84) | 删除 |
| 门禁 | `check_doc_drift.py` / `check_all_claims.py` | 各自加一处显式豁免，理由入注释 |

合计 **32 文件、+172 / −6729**。

## 2. 顺带根除 CI 五连红

`canvas_wbt.py` 是全仓**唯一** import pandas/plotly 的地方（`:355 import pandas as pd`、
`:168 import plotly.graph_objects as go`），而两行都**在 `_import_wbt()` 那个 try 之外** ——
CI 不装 pandas（`pyproject.toml` 从未声明，只有 `report` extra 钉了 `wbt==0.9.1`），
于是 `ModuleNotFoundError: No module named 'pandas'` ⇒ `test_canvas_d_trust_boundary.py`
红 ⇒ 最近 5 次 push 全红。

删掉这个文件即根除，**不必**改判据、**不必**给 CI 加 pandas/plotly。
此前讨论过的 B 方案（两个 import 都裹进 `WbtUnavailableError` + 声明依赖）被
「删掉」这一刀取代，且更干净。

## 3. 被作废的契约换成了一条更狠的守卫

`test_dashboard_ashare_contract.py::test_canvas_d_only_forwards_market_context`
守的是「D 是服务端取数的，只能透传 `code`、不得按市场分支渲染」。画布 D 下线后
它没有承载对象了 —— 但它记的**根因**（跨市场错配：A/B/C 画 123 根 A 股 K 线、D 画
579 根 BTCUSDT K 线同屏，根因是**画布各自取数**）必须留下。

改写为 `test_canvas_modules_never_fetch_server_side()`：遍历所有 `canvas_*.js`，
断言源码里**不出现** `fetch(` / `XMLHttpRequest` / `/api/`。

> 这条比原断言强：原断言只管 D 怎么透传；新断言管**所有画布永远不许自己取数** ——
> 跨市场错配的门从此关死。

**变异测试验证是真判据**：往 `canvas_b.js` 末尾追加 `fetch("/api/dashboard/snapshot");`
⇒ 红（`AssertionError` 指到那一行）；还原 ⇒ 绿，`git diff --stat` 干净。

## 4. 发现并修掉的两个隐藏问题

**(a) `dashboard/dashboard.js` 早就是死代码。** 删它之前实测：
`tests/conftest.py:258-274 DASHBOARD_JS_ORDER` 里**根本没有它** —— 11 个测试文件走
`dashboard_js()` 助手，读的是 R45 拆分后的 `dash-*.js` 等有序拼接；`index.html` 的
10 个 `<script src>` 里**也没有它**。即 4925 行 / 219693 B 无人加载。奎爷拍板删，
是清理而非有损变更。

**(b) `deploy/dashboard-sync.sh` 一直在同步线上不加载的文件。** `:45` 的
`FILES=(... dashboard.js canvas_d.js ...)` 停在 R45 拆分前 —— 同步了没人用的两个文件，
却**漏掉了真正在线的 7 个 `dash-*.js` 和 `dashboard.bundle.js`**。已改为
「真实加载物清单」，`:87` 的 HTTP 校验循环同步改掉；`deploy/README.md` 的 cp 示例
换成新列表，并加一行「改过 dash-*.js 后必须重建 bundle」。

## 5. 复验（全绿）

| 项 | 结果 |
|---|---|
| pytest | **1074 passed / 0 failed / 4 skipped**（ccxt 未装、chromium aarch64 SIGTRAP ×2、first_buy_bridge 该序列无两个中枢 —— 均非代码缺陷） |
| ruff / ruff format | `All checks passed!` / `227 files already formatted` |
| mypy | `Success: no issues found in 91 source files`（R49 是 111 —— 源文件数随 `canvas_wbt.py` 删除而变） |
| vulture / lint-imports | exit 0 / exit 0 |
| `check_sql_layering` / `check_storage_failure_semantics` / `check_doc_counts` / `check_doc_drift` / `check_all_claims` / `check_enqueue_skeleton_unique` / `check_job_poll_unique` / `selftest_gates` / `build_dashboard_bundle --check` | 全绿 |

`check_all_claims` 本轮转红于「文档引用了已删路径」12 处（roadmap / duplication-triage /
pending-wiring / progress-log / review-dashboard-r45），处理方式是**加显式豁免**而非改文档 ——
那些提及正是**下线记录本身**，改掉它们等于抹掉历史。在 `_ALLOW` 里逐条写明理由。

---

# R52：CI 恒红的真因 —— `ci.yml` 自己的 YAML 缩进

> 这才是「CI 五连红」的**第二根因**，且比第一根（pandas）严重得多。
> R51 删掉画布 D 让 pytest 转绿之后，它才第一次浮出水面。
>
> 已提交并推送：**`7e7a525a0`** —— **CI 首次全绿**（run `37304899073`，`completed success`）。

## 1. 现象

R51 推送后 CI **仍然红**。但这次的日志形状完全不同：

- pytest **绿**：`1051 passed, 27 skipped in 85.12s`（本机 1074/4）
- 所有门禁的输出都打了 ✅，末行是 `check_doc_drift` 的 `✅ 未发现漂移`
- 然后才是真正的死因：

```
line 33: -: command not found
##[error]Process completed with exit code 127.
```

## 2. 根因

不在任何脚本里，在 `.github/workflows/ci.yml` 本身。

```yaml
      - name: Static quality gates
        run: |
          ...
          python scripts/check_doc_drift.py
          - name: Dashboard bundle in sync          # ← 缩进 10 空格
            run: python scripts/build_dashboard_bundle.py --check
          - name: Doc claims (full sweep)           # ← 同上
          ...
```

YAML 的 `run: |` 是**块标量**：其后所有缩进比 `run:` 键**更深**的行都是字符串内容。
这 5 个 `- name:` 缩进 10 空格 > `run:` 键的 8 空格 ⇒ 它们**不是 step，是 shell 脚本的行**。
bash 执行到 `- name: Dashboard bundle in sync` → `-: command not found` → exit 127。

**后果不是"红"，是"没跑"**：这 5 个门禁

| 被吞掉的 step | 门禁 |
|---|---|
| Dashboard bundle in sync | `build_dashboard_bundle.py --check` |
| Doc claims (full sweep) | `check_all_claims.py` |
| Doc counts | `check_doc_counts.py` |
| Enqueue skeleton is unique | `check_enqueue_skeleton_unique.py` |
| Frontend job polling is unique | `check_job_poll_unique.py` |

在 CI 上**从来没有真正执行过一次**。而整条 `Static quality gates` 步骤恒定 exit 127，
所以没人看见。

## 3. 引入时间与被掩盖的过程

- 引入于 `1cda27af1`（R45 dashboard 拆分，2026-10-05 00:39）—— 与 pandas 红灯**同一天**。
- 此后一直被 pytest 的红灯**挡在前面**：pytest 先失败，`Static quality gates` 步骤
  根本轮不到执行，缩进错误零暴露。
- R51 删掉画布 D ⇒ pytest 转绿 ⇒ 步骤终于跑到第 33 行 ⇒ 缩进错误暴露。

> 这是本仓吃过的**第三类**环境/结构型问题（前两类：R45 的「恒返回 0 的门禁」、
> R49 的「判据依赖本机环境」）。共同特征：**它不产生测试失败，只产生"看不见"**。

## 4. 新增门禁⑨：防这类错静默复发

`scripts/check_ci_workflow.py` —— 扫 `.github/workflows/*.yml`，把每个 `run: |` / `run: >`
块标量的**实际内容**抠出来，若内容里出现 `- name:` / `- uses:` / `- run:` / `run:` 开头的行，
判为「step 被吞」，退出码 1。

- **不用 PyYAML**：CI 装了 pyyaml，但本仓 `.venv` 没有；门禁不该依赖装不装得上的包。
  块标量的缩进规则在 YAML 规范里就一句话，手写足够。
- **已验证能抓到**：拿 `1cda27af1` 的真实坏文件跑，rc=1 且指到 `.github/workflows/ci.yml:11`
  （与 R51 前真实的 `:81` 同构）。
- **接入 selftest_gates**：新增 `fx_ci_workflow` 夹具，自检从 9/9 变 **10/10 全抓到**。
- **CI 里排在所有 step 之后**（自己没被吞，才敢查别人）。

## 5. 复验

| 项 | 结果 |
|---|---|
| pytest | `1074 passed / 0 failed / 4 skipped` |
| ruff / ruff format | `All checks passed!` / `228 files already formatted` |
| mypy `cpt scripts` | `Success: no issues found in 111 source files` |
| 9 道脚本门禁 + `selftest_gates` + `build_dashboard_bundle --check` + `lint-imports` | 全绿 |
| `check_ci_workflow` | rc=0 |
| selftest | **10/10 抓到了各自的错例** |

**CI 实跑证据**（run `37304899073`，`completed success`）—— 被吞的那 5 个门禁这次
**确实执行了**，输出各就各位，且 `command not found` / `exit code 127` 计数为 0：

```
1051 passed, 27 skipped in 87.02s
全部门禁都能抓到各自的错例 ✅
✅ 未发现漂移
  ✅ bundle 与源文件同步
✅ 全部断言对得上
  ✅ workflow 缩进正常（1 个文件，step 未被吞进 run 块标量）
```

## 6. 复现命令

```bash
cd /home/ubuntu/DSH/Chan_Pattern_Trader
.venv/bin/python scripts/check_ci_workflow.py
.venv/bin/python scripts/selftest_gates.py
MYPY_CACHE_DIR=/home/ubuntu/work/tmp-caches/mypy .venv/bin/mypy cpt scripts
```

---

# R53：占位行守卫 —— 零价 K 线不再静默进结构计算

> 奎爷 m01237 裁决：「先更新文档+commit+push，然后**已知的问题该修就得修**。」
> 挂在报告里三轮的 P2，本轮修掉。**已提交并推送：`8596e7e6c`。**
>
> 查清后发现**比 P2 严重**：不是一只票降级，是两档危害、其中一档从无任何门禁发现。

## 1. 查库实证

`public.daily_bar` 中 `open=high=low=0` 共 **35 行 / 18 只票**，全部集中在
**2026-09-28~09-30 三天**，全部 `vol=amt=0`；10-01 之后干净
⇒ 上游那三天批量坏了、之后修好了。**不是停牌**（18 只票不会同时停牌）。

- `asel.security_master` 池子 **5221** 只，18 只坏票**全在池内**。
- `public.daily_bar` 全仓**无任何 INSERT** ⇒ CPT 只读，改不了源头
  ⇒ 只能在**读取侧**拦。

| 形态 | 行数 | 原行为 | 后果 |
|---|---|---|---|
| `close≠0`（填的是前收盘价） | 17 | `validate_ashare_bars` 抛 `DataValidationError` | **整只票降级**，657 根里 1 根坏就全废 |
| `close=0`（完全空行） | 18 | `low<=close<=high` 判 `0<=0<=0` **成立** ⇒ **放行** | 零价 K 线进结构计算 ⇒ 假分型/假笔/假中枢，**全程零报错** |

同票多天的 `close` 数值完全相同（601059 三天都 15.560、688496 三天都 0.580）
⇒ 确认填的是**前收盘价**。

**第二档是本轮真正要杀的目标**：它不产生任何失败信号 —— 没有异常、没有 WARNING、
门禁全绿、快照 `data_quality.severity` 仍是 `ok`。画布上只是悄悄多一根 0 价针。

## 2. 改了什么

**adapters（`cpt/adapters/a_share_local.py`）**

- 新增 `ASharePlaceholderRowsError` —— 与 `AShareNoDataError` /
  `AShareNoFactorError` 分开成类，因为三者**排查方向完全不同**：
  查采集 vs 查因子覆盖率 vs 查 DB 连通性。合并会把排查指到错误的地方
  （本仓已有前科：`test_ashare_db_error_not_masked.py` 记着「DB 故障被报成缺因子」）。
- `AShareFetchResult` 加 `skipped_placeholder` 字段，默认 `()`
  ⇒ **不破坏**测试里只给两个字段的 duck-type 假对象。
- 循环里丢弃 `O/H/L` 同时为 0 的行。**判据不看 close** —— 正是为了让
  「校验器会放行」的那一档也被拦下。用原始值判（复权因子再正常，`0 * factor` 仍是 0）。
- 全部为占位行 ⇒ 抛专属错误，不混进 `no_factor`。

**application（`cpt/application/a_share_snapshot.py`）**

- 部分丢弃 ⇒ 打 **WARNING**，带丢弃行数与样例日期。
- 全部丢弃 ⇒ reason `placeholder_rows`，不混进 `no_factor`/`no_data`/`db_error`。
- `_skipped_placeholder` 辅助函数，照抄 `_skipped_no_factor` 的 duck-type-safe 写法。

## 3. 两个被否掉的修法

| 方案 | 否掉的理由 |
|---|---|
| 修校验器去「容忍」0 价 | 0 价 K 线本就不合法，放它进去违反 `docs/rules.md` §5.3「非交易日不出图、不用 0 填充」 |
| SQL `WHERE` 静默过滤 | 违反本仓「缺失要**响亮**失败」的纪律；且分不出两档 |

**丢弃才是对的** —— A 股本来就有合法的日历/停牌缺口，缺一天不影响结构计算。

## 4. 变异测试（三条，各自有测试变红）

| 变异 | 结果 |
|---|---|
| ① 关掉适配器守卫 | **4 红** |
| ② 去掉 WARNING | `test_partial_placeholder_rows_are_announced_loudly` **红** |
| ③ 把判据改回看 `close` | **4 红** |

②证明「响亮」这条纪律是**被守着的**、不是摆设；③证明**静默那一档**被钉住了。
还原后 `1090 passed / 0 failed / 4 skipped`。

另有一条 `test_zero_price_bar_would_have_passed_the_validator` 断言的是
**校验器的真实行为**（0 价 bar 能过校验），不是我们的实现 ——
哪天有人放宽了 `validate_ashare_bars`，它会先红，提醒当初为什么加这道守卫。

## 5. 真机复验（真实 DB，不是 mock）

```
601238  库里 297 行，其中 O/H/L 全 0 的 1 行
  【旧】✗ DataValidationError: bars[294] open_time=1790553600000 OHLC 非法:
        要求 low<=open,close<=high, 实测 low=0.0 open=0.0 high=0.0
        close=10.288075359199999
  【新】✓ 296 根（丢弃 1 行）

000016  【第二档】旧：286 根含 3 根 0 价线，校验器**放行**
                 （open_time=1790553600000 O=H=L=C=0 ← 画布上是一根 0 价针）
        新：283 根，零价行 0 根

10 只相关票全部校验通过；600519 丢弃 0 行（无误伤）
```

`close=10.288075359199999` 与原报告里记的 `close=10.288` 是同一行 —— 原始故障精确复现。

## 6. 复验

| 项 | 结果 |
|---|---|
| pytest | **1090 passed / 0 failed / 4 skipped**（1074 + 16 条新测试） |
| ruff / ruff format | `All checks passed!` / `229 files already formatted` |
| mypy `cpt scripts` | `Success: no issues found in 111 source files` |
| vulture（CI 口径 `cpt whitelist.py`）/ lint-imports | rc=0 / rc=0 |
| 9 道脚本门禁 + `build_dashboard_bundle --check` | 全绿 |

> 踩坑记录：vulture 我一开始跑成 `vulture --min-confidence 60 cpt`（漏了
> `whitelist.py`），报 rc=3 且列出一堆假的死代码。CI 的实际口径带 whitelist
> —— **跑门禁必须照抄 ci.yml 里的那行，不要凭印象**。

## 7. 结论

P2 数据缺陷已修。四轮（R49~R53）报告里挂的待办清零。
`public.daily_bar` 里的 35 行废行仍在（只读，改不了），
但它们现在**既不会污染画布，也不会静默** —— 每天跑批会打 WARNING 带行数与日期。

---

# R54 —— 文档全量对齐：清出「第四类静默门禁失效」

**commit** `957dbd791`「R54: 文档全量对齐 —— 清出第四类静默门禁失效，新增门禁⑪」
（12 文件 +430/-41）。**任务**：扫 repo 全部文档，对齐文档与实际代码。
**范围**：38 份文档 / 16,023 行，按 A 现行事实 / B 计划验收报告 / C 历史记录三分类。

## 1. 核心问题：门禁**验错了属性**

`scripts/check_all_claims.py` 的 L 类管的就是 `file.py:123` 这类行号引用，
判据只有一句（约 `:423`）：

```python
if ln > n:                  # n = 文件总行数
    bad["L 行号越界"].append(...)
```

**只验「行号没超出文件总行数」，从不读那一行还是不是文档说的那件事。**
它一边打印 `✅ 全部断言对得上`（1428 条断言），一边有 30+ 处坐标指着无关代码。
该文件 docstring `:24` 自述 L 类要验「那行还在不在」—— **声明与实现分家**。

实测样本：

| 文档引用 | 那一行实际是什么 |
|---|---|
| `docs/pending-wiring.md` 的 R22 接线表十处 | **全错**（`cpt/web/app.py` 整体右移约 280 行） |
| `docs/known-traps.md:669` 的 `app.py:871` | 真身已漂到 `app.py:900` |
| `docs/duplication-triage.md` 约 30 处 | `cpt/adapters/a_share_local.py` 自身从 225 漂到 579 |

### 本仓第四类静默门禁失效

| 轮次 | 形态 | 表现 |
|---|---|---|
| R45 | 门禁恒 `return 0` | 跑起来永远绿 |
| R49 | 断言依赖本机环境 | 本机绿、CI 红（或反之） |
| R52 | YAML 块标量吞掉 step | 5 个门禁**一次都没执行过** |
| **R54** | **门禁验错了属性** | 跑了、绿了，查的条件**弱于声明** |

四类共同点：**都不产生测试失败**。⇒「CI 绿」只能证明「CI 跑完了」，
证明不了「CI 查的东西是它声称要查的那件事」。

## 2. 逐份核对：文档纪律本身是真的（七处自查全部属实）

| 文档自述 | 实测 |
|---|---|
| `architecture.md:4` 引用的两份文档 | 该行**自标「已删除」**，正确 |
| `deploy/README.md` 的 run5.sh / collector_linux.sh / scheduler.py | 文档 338/439 行明说**是远端采集包内文件、本仓没有** |
| `web-api-reference.md` 自称「27 个接口」 | grep `cpt/web/app.py` 的 `"/api/..."` 字面量**恰好 27 条** |
| 同上，60-62 行「与计划文档的差异」表 | 预先标出 3 个**从未实现**的接口 —— 预判了 grep 会误报 |
| `/api/canvas/wbt` | 清单 `:16` 已划掉并注「R51 下线，回 404」 |
| `pending-wiring.md:218-219` R45 更正 | 属实（`cpt/storage/dashboard_run_store.py`，12173 B） |
| 「唯一仍在册待接线」`t_plus_one_purchase_allowed` | **确实仍无生产调用方**（仅 docstring / `__all__` / def / 测试 / whitelist） |
| `export-schema-v1.md:245` 提 v2 文档 | 是 §8 变更政策的**条件句**，不是断言它存在 |

行号引用分布：最要紧的四份（`README.md` / `architecture.md` / `rules.md` /
`web-api-reference.md`）**0 处**；风险集中在 `pending-wiring.md`（32 处）与
`duplication-triage.md`（67 处）。

## 3. 改了什么

- **`docs/pending-wiring.md`**：文件头加 R54 复核说明（32 处实测 17 处失效；
  以**符号名/路由路径**为准）；**R22 接线表十处全错行号全部改对**；另 12 处改对
  （含 `dashboard_snapshot_v2.py` 占位表、`cpt/web/__main__.py:790→920`、
  `cpt/domain/models.py:163→179`）。
  **实质更正**：原文并列引用的 `_format_multi_level` **全仓已不存在**，已删。
- **`docs/known-traps.md:669`**：`app.py:871` → `app.py:900`（结论未变）。
- **`docs/duplication-triage.md`**：加 R54 头部注记 —— 行号是 2026-09-25 修复前
  快照的**历史证据**，**有意不逐个改写**（改了等于伪造归档现场），
  要查现状请用符号名 grep。
- **`docs/dashboard-final-acceptance.md` / `docs/dashboard-product-roadmap.md`**：
  `cpt/web/__main__.py:844→920`、`dashboard.py:140→cpt/application/dashboard.py:120`。
- **顺带修掉**：`README.md`「四点容易记错」实为 6 条 → **七点**；
  `scripts/check_ci_workflow.py` 自己编号写错（⑨→⑩，与 `ci.yml`/`architecture.md` 对齐）。

## 4. 新增门禁⑪ `scripts/check_line_refs.py`

判据刻意保守，**宁可漏报也不造假阳性**（假阳性会让门禁被无视）：

1. **严格解析目标文件**：完整路径优先，否则 basename 全仓唯一匹配；
   **同名多份直接跳过，绝不猜**。
2. 行号越界 ⇒ 失败。
3. 取**归属本次引用的片段**（上一个引用结束 → 下一个引用开始），在其中找
   「确实被该文件 `def`/`class` 定义」的标识符当锚点；必须**恰好命中一个**，
   0 个（引用调用点 / 无符号名）或多个（歧义）都跳过。
   这一步绕开了 `` `a.py:225` / `b.py:389` `` 这类**列表式引用**。
4. 锚点在 `NNN ± 3` 行内出现（**含调用点**，不要求是定义行）⇒ 通过。

历史归档 5 份整体豁免，但**每条必须写明理由**（空理由直接 rc=1）。
已登记进 `scripts/selftest_gates.py` 的 `FIXTURES`。

> 走过的弯路：第一版锚点启发式「取行号后紧跟的反引号标识符」在列表式引用上
> 把**第二个文件名**当锚点 ⇒ 大量假阳性；第二版拿**整行**取候选 ⇒
> 同一行并列两个引用时互相污染（`pending-wiring.md:309` 的 `cpt/web/app.py:862`
> 被同行的 `:287 _summarize_diff_value` 污染）。加「归属片段」后消除。

## 5. 复验

| 项 | 结果 |
|---|---|
| 门禁自检 | **11/11** `全部门禁都能抓到各自的错例 ✅`（含新增 `check_line_refs.py`） |
| 真仓变异 | `known-traps.md:669` 的 `app.py:900`→`app.py:800` ⇒ **rc=1**，报 `` 处没有 `_index_row_from_body`（±3 行内未见） `` 并打印该行真实内容；还原 ⇒ rc=0 |
| 全部门禁 | **rc=0**（9 道脚本门禁 + `build_dashboard_bundle --check` + selftest + lint-imports `Contracts: 6 kept, 0 broken` + vulture + ruff check + ruff format + mypy） |
| pytest | **1090 passed / 4 skipped** |
| CI run `37317765902`（`957dbd791`） | **success**；抠 run 级日志核实门禁⑪ **确实执行**：3.12 与 3.14 两版各有 step 定义 + 实跑输出 + 自检抓到，共 3 处；两版 `1067 passed, 27 skipped`；`command not found\|exit code 127` 计数 **0** |
| CI run `37318439972`（`52231648d`，收尾补丁） | **success**；门禁⑪ 双版本各实跑 1 次、自检各抓到 1 次、127 计数 0 |

> 踩坑记录：**变异没生效就等于没验**。第一次 `sed` 用 `cpt/web/app.py:900` 做
> 模式，而文档里写的是裸 `app.py:900` ⇒ **一个字符都没改到，门禁照样绿**。
> 第二次先 `sed -n '669p'` 确认注入生效，才认变异结果。
>
> 另一条：抠 CI 日志时**别硬编码 zip 内的文件名索引** —— 两次提取的 index 0
> 分别是 `0_unit (3.14).txt` 与 `1_unit (3.12).txt`，按 `*.txt` 遍历才稳。

**收尾补丁 `52231648d`**：`check_all_claims.py` docstring 第 24 行原写
「L 行号引用 —— 那行还在不在」（**声明与实现分家**，正是 R54 的病根），
已改成实话 —— 「这一句只验『行号没超出文件总行数』；至于『那行还是不是文档说的
那件事』，由门禁⑪ 负责」。只改注释，不改行为。

## 6. 结论

文档纪律经得起查 —— 七处自查全部属实，三道既有文档门禁全绿。
但 `check_all_claims.py` 的 L 类**验错了属性**，R54 已补上门禁⑪ 盯住
「行号还指不指对那件事」。四类静默失效现已各有一道门禁：
①恒返回0/②环境依赖 → `selftest_gates.py`，③YAML 吞 step → 门禁⑩，
④验错属性 → 门禁⑪。

---

# R55 —— 给 ①② 补门禁：同一类病会换入口复发

> 承 R54 结论：四类静默失效已各有门禁，但 **① R45（门禁恒返回 0）** 与
> **② R49（断言依赖本机环境）** 两类此前只有 `selftest_gates.py` **泛泛兜着**，
> 没有专盯的门禁。本轮把这两类的残量缺口找准、各补一道。
> 动的全是门禁与文档，**没有改任何生产代码**。

## 1. ①的残量缺口：没有东西保证「自检清单」与 CI 同步

`selftest_gates.py` 是 R45 的对策：给每个门禁配一个**已知错例**，先证明它能红。
但它那份 `FIXTURES` 清单是**手工维护**的，而**没有任何东西**保证它与
`.github/workflows/ci.yml` 同步。于是：

> 新门禁接进 CI、却忘了配夹具 ⇒ 它**逃过体检**，而 CI 依然全绿 ——
> 因为**它没红**。

病根与 R45 一模一样（**全绿只是它什么都没查**），只是入口从「门禁自己坏」
换成了「门禁逃过体检」。门禁⑩ `check_ci_workflow.py` 只查 YAML 缩进，不管这个。

R45 的原始记载在 `docs/todo-r45-followups.md:300-347`（「P1-2 门禁自检」），
里面已有「恒 return 0 ⇒ CI 上永远绿，等于没有门禁」的错法表 —— 那次修的是
「门禁自己坏」，没修「门禁逃过体检」。

## 2. ②为什么立不出静态判据（两条路都实测否决）

| 试探 | 实测 | 结论 |
|---|---|---|
| **名字启发式**：用例名含 `missing\|unavailable\|degrade\|absent\|without_\|disabled` ⇒ 必须看得到 patch | 命中 **99 个**，其中 **61 个**看不到 patch。但逐条看，那 61 个的「缺」是**数据缺**（缺一天 / 缺一列 / 缺一个字段，由用例自己构造），与环境无关 | 假阳性高到会让你无视这道门禁 ⇒ **否决** |
| **环境探测启发式**：模块里出现 `shutil.which` / `Path.home` / `os.environ` / `platform.*` / `sys.platform` / `psycopg.connect` / `subprocess.*` ⇒ 必须有 skip 守卫或 patch | 含探测的模块只有 **7 个**，其中 **5 个**已有守卫；余下 2 个是 `subprocess.Popen` 启自家服务（正当）。**今日零违规** | 立起来只是摆设；更要命的是它**抓不到 R49 真身** —— 那例代码里根本没有探测调用，靠的是「本机恰好没装 Wind」这个**沉默事实** ⇒ **否决** |

R49 的真身（据 R49 报告节）：`test_dashboard_url_credentials.py` 沙箱没注入
`url_safety.js`、失败被误读成「node v22 的 URL 解析差异」；
`test_factor_backfill_script.py` 把「本机没装 Wind」当前提，拿到
`wind_basis_unknown` 而非 `wind_unavailable`。R50 推翻了初判：真根因是
**测试自身的判据缺陷**，不是环境问题。

⇒ **②是执行级的，不是文本级的。判据只能是「换个环境再跑一遍」。**

## 3. 新增门禁

### ⑫ `scripts/check_gate_coverage.py`（针对①）

三方对齐：

| 集合 | 来源 |
|---|---|
| `A` | `ci.yml` 里 `python scripts/x.py` 调起的门禁（正则 `_INVOKE`，跳过注释行，排除自检自己） |
| `B` | `selftest_gates.py` 的 `FIXTURES` 键（**用 AST 解析**，非正则 —— 要证明那些键确实是夹具；去掉 `#分类` 后缀） |
| `C` | `scripts/check_*.py` 实际存在的文件 |

三条规则：

- **R1** `A ⊆ B` —— 在 ci.yml 里跑、却没有夹具 ⇒ **逃过体检**（能不能红从未被证明）；
- **R2** 每个夹具指向的脚本必须真实存在 ⇒ 否则是**死夹具**；
- **R3** `C ⊆ A` —— 磁盘上有、CI 里不跑 ⇒ **闲置门禁**（写了却从不执行）。

`NOT_A_GATE` 豁免两条，**每条必须写明理由**（空理由 ⇒ rc=1）：
`scan_doc_claims.py`（R45 的一次性普查工具，产出已并入 `check_all_claims.py`）、
`verify_public_contracts.py`（人工按需跑的对外契约抽查，依赖真实上游）。

> **配置即约束**：这份清单跟着代码一起长。新加门禁必须**同时**改 `ci.yml` 与
> `FIXTURES`，改了半边就会被门禁⑫ 拦住。
>
> 我写错过一次：R2 初版用集合差 `b - c` 比较，把不以 `check_` 开头的
> `build_dashboard_bundle.py` 误报成死夹具；改成逐个 `(SCRIPTS / name).exists()`。

实跑：`✅ 没有门禁能逃过体检（ci.yml 跑 12 道，夹具 12 条，磁盘上 check_*.py
共 11 个；豁免 2 个非门禁工具）`

### ⑬ `scripts/check_cold_environment.py`（针对②）

以 `PATH=/nonexistent`、**空 `HOME`**（其余环境原样保留）重跑整个 `tests/`：

- 有 failed / error ⇒ 该用例的结论是**本机环境的函数** ⇒ **判红**；
- `skip` **不算**失败（「本机没有这个能力」是 skip 的正当用途），但**逐条打出来**
  —— 「绿灯来自没执行」比红更糟。

实现要点：临时 HOME 建在 `ROOT/.pytest_cache` 下，**不放 `/tmp`**
（本仓 `/tmp` 配额紧、且不支持 SQLite WAL，两样都踩过）；
pytest 参数**不再多给 `-q`**（`pyproject` 的 `addopts` 已有 `-q`，加到 `-qq`
会把「N passed」汇总行吃掉，只剩点阵 —— R49 踩过）。

## 4. 复验

| 项 | 结果 |
|---|---|
| 门禁自检 | **13/13** `全部门禁都能抓到各自的错例 ✅`（含新增 ⑫ 与 ⑬） |
| 真仓变异 · ⑫/R1 | 往 `ci.yml` 插 `run: python scripts/check_does_not_exist.py` ⇒ **rc=1** `R1 逃过体检：check_does_not_exist.py`；还原 ⇒ rc=0 |
| 真仓变异 · ⑫/R3 | 把 `check_line_refs.py` 从 `ci.yml` 摘掉（脚本仍在磁盘）⇒ **rc=1** `R3 闲置门禁：scripts/check_line_refs.py`；还原 ⇒ rc=0 |
| 真仓变异 · ⑬ | 落盘 `tests/test_r55_ambient_probe.py`（断言 `PATH != "/nonexistent"`）⇒ **rc=1**「这些用例依赖本机环境」+ 打印该用例的真实失败串；**删掉探针** ⇒ rc=0（探针不入库） |
| 门禁⑬ 实测 | 冷跑 **1083 passed / 11 skipped**，耗时 **72~75 秒**（正常跑 1090/4；差的 7 条正是 `node 不可用，跳过 JS 行为契约`） |
| 全部门禁 | **17 项 rc=0**（10 道脚本门禁 + `build_dashboard_bundle --check` + selftest + lint-imports `Contracts: 6 kept, 0 broken` + vulture + ruff check + ruff format 235 files + mypy 91 source files） |
| pytest | **1090 passed / 4 skipped in 91.86s** |
| 推送 | commit **`220e05d2f`**，8 文件 **+539/-2**；`52231648d..220e05d2f`；`ls-remote` 核对 local==remote==`220e05d2f00004cdc5b8b08b02d0a659bd86622e` |

> 三条变异都先确认**注入真的落到了文件里**（`assert s.count(old) == 1` 或先
> `sed -n` 打印那一行）—— R54 吃过「`sed` 没匹配上、门禁照样绿」的亏：
> **变异没生效就等于没验**。

## 4b. CI 第一次红了 —— 我自己又踩了「没照原句跑」

第一次推送 `220e05d2f` 之后，CI run **`37323724126`** 在**两个 Python 版本上都红**，
红的还是「Static quality gates」：

```
scripts/check_gate_coverage.py:98: error: Unused "type: ignore" comment  [unused-ignore]
scripts/check_gate_coverage.py:98: error: "expr" has no attribute "keys"  [attr-defined]
Found 2 errors in 1 file (checked 114 source files)
```

**根因不是门禁的逻辑，是我本地复验时的命令。** ci.yml:55 写的是

```
mypy cpt scripts
```

而我复验时凭记忆敲成了 **`mypy cpt`** —— 覆盖范围少了 `scripts/`，于是我新写的
门禁自己的类型错误，恰好落在没被检查的那一半里。本地「91 source files Success」，
CI 是 114 files / 2 errors。

这条教训（**「照着 ci.yml 的原句跑」**）在 `docs/known-traps.md` 里早就写着
（R52 vulture 那次就是漏了 `whitelist.py`），**还是又踩一次**。所以本轮不只修
代码，还改了**复验方法**：

> 写了 `/home/ubuntu/work/tmp-caches/run_ci_steps.py` —— 它**从 ci.yml 里读**
> 每个 `- name:` / `run:` 步骤（含块标量），照原句在本地逐步执行。
> 手敲命令这件事本身被取消了。

它自己第一次也写错了：块标量的终止条件算错，把 3 个 `run: |` 步骤**整个漏掉**
（`Run unit and integration tests` / `Static quality gates` / `Dead-code audit`）
—— 正是本仓被块标量吞过 5 个 step 的那个坑。改成拿 `run:` 键自身的缩进去比
终止条件后，解析出 **14 步**（修前只解析出 11 步，且 10 步因 `python` 不在 PATH
上而 rc=127 —— 两个假结果都差点被当成真的）。

**终局**：照 ci.yml 逐步照跑 **14 步全 rc=0**；修掉 `type: ignore`（改成
`isinstance(node.value, ast.Dict)` 收窄，类型本来就该这么写，不需要 ignore）。

| 项 | 结果 |
|---|---|
| CI run `37323724126`（`220e05d2f`） | **failure** —— `mypy cpt scripts` 2 errors（我本地只跑了 `mypy cpt`） |
| 本地复验（修正后） | 照 ci.yml **14 步全 rc=0**；`mypy cpt scripts` → **114 source files Success** |
| 补推送 | commit **`1d3579152`**，`220e05d2f..1d3579152`；`ls-remote` 核对 local==remote==`1d3579152da89726713984185ac07febd5688c3f` |
| **CI run `37325137865`** | **completed success**（3.12 与 3.14 两版皆绿） |
| CI 上门禁⑪ 实跑 | `✅ 行号引用仍指到该指的地方（扫 35 份文档，豁免 5 份历史归档）` ×2 |
| CI 上门禁⑫ 实跑 | `✅ 没有门禁能逃过体检（ci.yml 跑 12 道，夹具 12 条，磁盘上 check_*.py 共 11 个；豁免 2 个非门禁工具）` ×2 |
| CI 上门禁⑬ 实跑 | `✅ 抽掉 PATH/HOME 之后结论不变（PATH=/nonexistent、空 HOME）；1058 passed, 36 skipped`（3.14 用 74.67s / 3.12 用 72.64s） |
| CI 自检 | `全部门禁都能抓到各自的错例 ✅`；`Success: no issues found in 114 source files`；`Contracts: 6 kept, 0 broken.` |
| `command not found` / `exit code 127` | 全日志计数 **0** |
| 文档回填推送 | commit **`c6fe3394a`**「R55 补：progress-log 记下 CI 那次红 —— 提醒失效两次了，得换修法」；`ls-remote` 核对 local==remote==`c6fe3394a89c50fce72d65875c486701af82cb42` |
| **CI run `37325815522`** | **completed success** |

> **CI 冷跑与正常跑的差量已逐条核实**：正常跑 `1067 passed, 27 skipped`，
> 冷跑 `1058 passed, 36 skipped`，差的 **9 条 = 2 条 chromium + 7 条
> `test_dashboard_url_credentials.py`「node 不可用」** —— 与本地口径一致
> （本地是 1090/4 → 1083/11，同样差 9 条）。这 9 条不是「被冷环境悄悄关掉」，
> 它们本来就自报依赖 node / Chromium；门禁⑬ 把每一条 skip 都打出来，
> 就是为了让「绿灯来自没执行」这件事没法藏。

## 5. 成本（需要知情的一条）

门禁⑬ 会让 CI **多跑一整轮 pytest**，实测 **+72~75 秒**（本地 91.86s 的正常
全量 + 冷跑 74.66s）。这是 ② 这一类的判据所决定的 —— 静态判据两条路都实测
否决了，只剩「换个环境再跑一遍」这一条。若嫌贵，可把它降级成
「只在 nightly / 手动触发时跑」，代码不用改、只改 `ci.yml` 里那一行的触发条件。

## 6. 结论

四类静默失效现在**各有专盯的门禁**：

| 类 | 形态 | 门禁 |
|---|---|---|
| ① R45 | 门禁恒 `return 0` | 门禁⑫（**不许逃过体检**）+ `selftest_gates.py` |
| ② R49 | 断言依赖本机环境 | 门禁⑬（**冷环境重跑**） |
| ③ R52 | YAML 块标量吞 step | 门禁⑩ |
| ④ R54 | 门禁验错了属性 | 门禁⑪ |

> **同一类病会换入口复发。** R45 是「门禁自己恒返回 0」，到 R55 变成「门禁逃过
> 体检」—— 形态变了，**病根没变**，都是「全绿」代替了「查过」。补门禁时要顺着
> 病根问一句：**这个病还有别的入口吗？**
>
> **判据立不立得住，要用数据试，不要用直觉定。** ② 的两条静态路都是写之前先在
> 真仓上量过的（99 个命中 / 7 个模块），量完才知道不能立 —— 若凭直觉立了，
> 得到的是一道天天误报、很快就被所有人无视的门禁。
