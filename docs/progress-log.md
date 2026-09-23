# CPT 进度日志

维护人：队长（DSH 会话）
开始：2026-09-23 凌晨
依据：`docs/implementation-plan.md` v0.1（2026-09-22）
commit 规范：每个里程碑验收通过后一次 commit；阶段内允许 working commit（不阻塞）

## 0. 开局盘点

- 仓库：`/home/ubuntu/work/Chan_Pattern_Trader/`（原 `cryptocurrency-trading/`，本次重命名）
- remote：`https://github.com/nxz1026/Chan_Pattern_Trader.git`
- 分支：`main`，已与远端同步（HEAD `f417635`）
- references/ 现状（本地已落盘，HEAD 与计划 §3 一致）：
  - `references/chanlun-pro/` — `78ffa470` ✓
  - `references/chanlun.py/` — `2e4fa135` ✓
  - `references/chanlun_pine/` — `0c028ef` ✓
- 缺失交付物：`scripts/fetch_references.sh`、`docs/reference-audit.md`、工具链配置、venv

## 1. 决策日志

### 2026-09-23 开局决策
- **D1**：M0 派工用 omp-coder skill，四阶段（IMPLEMENT/VERIFY/REAL/REPORT）拆分，每个工单一哨兵。
- **D2**：本次同步只动 `M0`（基线）+ `M1`（单级别全链路）；M2–M6 后续 goal 推进。
- **D3**：进度记录写本文件，不混入代码 commit message。
- **D4**：PyPI MIT 版 chanlun 包验证如果结论为"不可用"，**不阻塞** M0；如实写入 `docs/reference-audit.md`，M2 oracle 切换策略留 TODO。
- **D5**：fetch_references.sh 用完整 40 位 SHA 作为 commit 期望值，避免 `--short` 截断造成校验假阳性（已确认 OMP 选用了完整 SHA）。

### 2026-09-23 round 1 — M0-01 IMPLEMENT 验收
- **派工**：M0-01-fetch-references，写 `scripts/fetch_references.sh`，超时 1800s，重试 2。
- **尝试 1（15:52:07 → 15:54:01，113s）**：OMP 输出 sentinel 字符串到 stdout 但 touch 了空 `.done` 文件 → dispatcher `sentinel_ok()` 失败。
- **尝试 2（15:54:07 → 15:54:41，34s）**：OMP 在第二条消息里复述"哨兵已落"但同样只 touch 空文件 → dispatcher `done-file-absent` 误判 → `FAILED attempts=2`。
- **队长独立验收**：
  - `bash -n scripts/fetch_references.sh` 通过 ✅
  - `bash scripts/fetch_references.sh` 实跑：3/3 仓库 HEAD 与期望完整 SHA 匹配 ✅
  - 覆盖检查：完整 SHA + set -euo pipefail + chmod + LICENSE 路径打印 + 失败非零退出码 + bash 4+ 校验 ✅
- **结论**：实质 IMPLEMENT 通过。`FAILED attempts=2` 是 dispatcher ↔ OMP 哨兵契约错位造成的伪阴性。
- **D6（哨兵契约补丁）**：OMP prompt 必须明确写 `echo "GATEKEEPER_ACCEPTED <tag>-<phase>" >> .omp-logs/<tag>-<phase>.done`（追加到文件，而非只 touch 空文件）。下次派工起，所有 prompt 用此模板。
- **补救**：队长手动 `echo "GATEKEEPER_ACCEPTED ..." > .omp-logs/M0-01-fetch-references-IMPLEMENT.done`，记录到日志（本条），不重跑。

### 2026-09-23 round 1 — M0-02 IMPLEMENT 验收
- **派工**：M0-02-reference-audit，写 `docs/reference-audit.md`，超时 1800s，重试 2。prompt 用 D6 模板明确 `echo "..." >> .done`。
- **尝试 1（15:56:55 → 15:57:46，51s）**：OMP 完成并把 sentinel 追加到 `.done` 文件，但 dispatcher `sentinel_ok()` 时机过早（OMP 完成 stdout 输出后到真正 `echo >> .done` 落盘之间有 ~1s 间隙），报 `done-file-absent`。
- **尝试 2（15:57:55 → 15:58:37，42s）**：OMP 复检并再写一次 sentinel，但同 race condition → `FAILED attempts=2`。
- **哨兵文件最终内容**：两行相同 `GATEKEEPER_ACCEPTED M0-02-reference-audit-IMPLEMENT`（无害冗余）。
- **队长独立验收**：
  - 文档存在，3514 bytes，46 行 ✅
  - 覆盖检查：3 仓库元数据表、复用边界表、许可证合规要点（Apache/MIT/GPL-3.0）、cl_interface 加密核心（query_macd_ld/compare_ld_beichi/user_custom_mmd）、PyPI 旧包 TBD 占位、fetch_references.sh 引用 ✅
  - URL/commit 取自 `scripts/fetch_references.sh` REPOS 定义 ✅
  - 实际验证 chanlun.py 存在 NOTICE 文件 ✅
- **结论**：实质 IMPLEMENT 通过。
- **D7（dispatcher race condition）**：omp-resilient4.sh 在 `timeout` 返回后立即检查 `.done` 文件，但 OMP 完成 stdout 流到执行 `echo >>` 命令落盘之间有 ~1s 窗口。建议补丁（不在本次范围内）：在 `if [[ -f "$done_file" ]]; then` 前加 `sleep 1` 或等文件 mtime 稳定。当前缓解：队长手动验哨兵文件内容，若含 sentinel 字符串即视为通过，不重跑。

### dispatcher race condition 现象汇总
- 工单 | 表现 | 哨兵最终状态
- --- | --- | ---
- M0-01 | try1+try2 报 done-file-absent | 队长手动 echo > 写入
- M0-02 | try1+try2 报 done-file-absent | OMP 用 `>>` 追加了 2 次，队长校验通过
- 影响：dispatcher FAILED 但实际产物有效；下一步应在所有 prompt 中显式 `>>` 而不是 `>`，并由队长独立 grep 哨兵确认。

### 2026-09-23 round 1 — M0-03 VERIFY（fetch_references.sh 可复现性）
- **队长独立验收**（在 /tmp/cpt-verify-test 干净目录）：
  - cp 脚本到临时位置、空 references/ → 执行 → 三个仓库全部 clone + checkout 到固定 commit，HEAD 与期望完整 SHA 匹配 ✅
  - exit code 0 ✅
- **结论**：fetch_references.sh 在干净环境可一键复现 references/ 到固定 commit ✅
- **M0-03 哨兵**：手动写入 `.omp-logs/M0-03-fetch-references-VERIFY.done`。

### 2026-09-23 round 2 — M0-04 IMPLEMENT 验收
- **try 1**（16:01:18 → 16:05:11，3:53）：OMP 完成评估，写入 §5 替换占位 TBD。
- **关键结论**：
  - PyPI 上 `chanlun` 包名 2026-05-26 起被 Rust 重写版 `chanlun.rs`（PyO3）接管；旧 Python `chanlun.py` 从未以 pip 包发布。
  - 旧 Python 包名 `chanlun-py`/`chanlunpy`/`chanlun_py` 均 404。
  - 同作者近亲：Rust 版 LICENSE 干净 MIT，无 czsc Apache 传染，无加密核心。
  - 许可证元数据：classifier 标 MIT，但 `license`/`license_expression` 字段为 null（仅靠 LICENSE 文件声明，卫生欠佳）。
  - 算法覆盖 6/7：分型/笔/中枢/线段/背驰/买卖点全有，**走势类型 trend_type 缺**（旧 Python `chan.py` `走势.分析` 等本就是 `pass` 桩）。
- **结论**：不可用作"旧版 Python MIT oracle"（前提不成立）。Rust 版作为附带候选留 M2 决策。
- **独立验证**：队长 curl PyPI `/pypi/chanlun/json`，确认 OMP 报告的 10 个版本号、`license=null`、classifier MIT、author=YuYuKunKun、summary=Rust 重写、project_urls 指向 `chanlun.rs` 全部对得上 ✅
- **try 2**（16:05:18 → 16:09:07）：dispatcher 同 race 触发二次重试，OMP 重复检视；为避免空跑浪费 token，队长 16:09:07 按 SKILL §4 "立即打断"模式 SIGTERM→KILL。attempts 末尾记录 `KILLED_BY_CAPTAIN`。
- **M0-04 实质通过**：§5 占位已替换，事实链清晰，队长独立验证对账。

## 6. Round 2 收尾

| 指标 | 值 |
|---|---|
| 工单完成数 | 4 / 5 (M0-01, M0-02, M0-03, M0-04) |
| 工单待派 | M0-05 工具链（ruff/mypy/pytest/pre-commit/import-linter）|
| 工单待跑 | M0-06 工具链对空骨架生效（队长验）|
| 工单待跑 | M0-07 全量验收 + commit/push |
| PyPI 关键事实 | chanlun 包 = Rust 重写版（非 Python 旧版），无趋势类型，license 元数据 null |
| KILLED_BY_CAPTAIN | M0-04 try 2 已收尾标记 |

## 2. M0 任务分解

| 工单 | 阶段 | 内容 | 哨兵 |
|---|---|---|---|
| M0-01 | IMPLEMENT | scripts/fetch_references.sh | `<tag>-IMPLEMENT.done` |
| M0-02 | IMPLEMENT | docs/reference-audit.md | 同上（合并工单） |
| M0-03 | VERIFY | 干净环境一键复现 | `<tag>-VERIFY.done` |
| M0-04 | IMPLEMENT | PyPI MIT chanlun 评估 | 单列 |
| M0-05 | IMPLEMENT | import-linter + ruff + mypy + pytest + pre-commit | 单列 |
| M0-06 | VERIFY | 工具链对空骨架生效 | `<tag>-VERIFY.done` |
| M0-07 | 队长验收 | 全量独立跑 + commit | — |

## 3. M1 任务分解

（待 M0-07 验收后展开）

## 4. 大分歧 / 等用户拍板

（暂无）

## 5. 已知限制

- 队长跑工单期间，`cryptocurrency-trading` 旧会话缓存路径不影响本仓库，仅历史 cwd 记录。

### 2026-09-23 round 3 — M0-05 工具链（队长手写）
- **派工**：M0-05-toolchain，13 项配置/包骨架文件。
- **try 1**（16:10:30 → 16:15:48，5+ min）：OMP 一直处于 thinking 阶段，0 个 tool_call 发出；watchdog 多次 tick "no tool_call yet"；日志 1468 行全是 agent_thought_chunk；明显卡死。队长按 SKILL §4 立即打断 SIGKILL 调度器 + omp-call，attempts 标记 `KILLED_BY_CAPTAIN (OMP stuck in thinking 5+ min, 0 tool_call)`。
- **D10（OMP 触发器问题）**：13 项配置+13 个文件提示体量过大，OMP 在 prompt 解析后进入"自言自语规划"循环未发出 tool_call。可能与 OMP 模型对长 prompt 的工具决策阈值有关。**缓解策略**：超 5 分钟无 tool_call 即视为触发器故障，队长手写或拆工单。
- **队长手写**（round 3 内完成）：
  - `pyproject.toml`：PEP 621 + ruff/mypy/pytest/coverage + setuptools 打包配置 + `[tool.setuptools.packages.find]` 限定 `cpt*`，避免把 references/ 误打包。
  - `.pre-commit-config.yaml`：ruff-format + ruff(check) + mypy + 本地 import-linter hook。
  - `.importlinter`：5 条契约（application⊥llm / domain 无第三方 / adapters 不入 domain / storage 隔离 application / engine 仅编排 domain+storage）。
  - `.gitignore`：追加 .pytest_cache/.mypy_cache/.ruff_cache/.coverage/build/dist/.omp-logs/*.bak。
  - `cpt/{application,engine,domain,adapters,storage,llm}/__init__.py` + `cpt/__init__.py`：6 个包骨架。
  - `tests/__init__.py` + `tests/test_imports.py`：3 个冒烟测试（顶层 / 子包 / tests 包导入）。
- **验证**：13/13 文件齐全、9/9 .py 语法通过、3/3 import 冒烟通过 ✅
- **M0-05 哨兵**：手动写入 `.omp-logs/M0-05-toolchain-IMPLEMENT.done`。

### 2026-09-23 round 3 — M0-06 VERIFY 工具链对空骨架生效
- **venv 建立**：先 `sudo apt-get install -y python3.14-venv`（系统缺 ensurepip），再 `python3 -m venv .venv`。
- **安装**：`pip install -e ".[dev]"` 拉入 ruff 0.16.8 / mypy 2.3.1 / pytest 9.1.1 / pytest-cov 7.1.0 / pre-commit 4.6.2 / import-linter 2.15 / types-requests。
- **初次问题**：
  - ruff format 把 markdown 里的 python 块当代码格式化 → 加 `extend-exclude = ["docs"]` 和 `src = ["cpt", "tests"]` 修复。
  - import-linter 报 `KeyError: 'name'` → contract 必须含 `name` 字段，补 5 条 contract 名字。
  - import-linter 报 `include_external_packages=True` 必需（domain 契约禁外部依赖 chanlun_pro 等），加全局开关。
  - pre-commit hook 调用应是 `import-linter lint --config .importlinter`（子命令 + config 在 lint 下），不是顶层 `--config`。
- **全量验证**：
  - ruff check: All checks passed ✅
  - ruff format --check: 10 files already formatted ✅
  - mypy: Success, no issues in 9 source files ✅
  - pytest -q: 3 passed ✅
  - import-linter: 5 contracts KEPT, 0 broken ✅
- **M0-06 哨兵**：手动写入 `.omp-logs/M0-06-toolchain-VERIFY.done`。

### 2026-09-23 round 3 — M0-07 全量验收 + commit
- **M0 8 项验收全 PASS**：
  - A1 fetch_references.sh 复现 references → 3/3 OK
  - A2 reference-audit.md 6 节齐全（含 §5 PyPI 评估实结论）
  - A3 rules.md §9 已冻结（9.1-9.7 + §9 标题）
  - A4 ruff check All checks passed
  - A5 ruff format --check 10 files already formatted
  - A6 mypy Success, no issues in 9 source files
  - A7 pytest -q 3 passed
  - A8 import-linter 5 contracts KEPT, 0 broken
- **Commit e3ecbe8**：M0 baseline（15 文件），本地已落。
- **Push 待用户授权**：remote 是 https://github.com/nxz1026/Chan_Pattern_Trader.git，缺凭据（无 askpass/.netrc/credential helper）。SKILL §6 要求"push 用现成 askpass，不现场拼凭据"，故 push 留 TODO 给用户（已加 inbox）。建议：GitHub deploy key 或 `gh auth login` 后 `git push origin main`。

## 7. Round 4 收尾

| 指标 | 值 |
|---|---|
| 工单完成数 | M1-01, M1-02（2/8 M1 工单） |
| 文件新增 | cpt/domain/types.py（51 行）+ cpt/domain/models.py（200 行） |
| 质量门 | ruff/mypy/pytest/import-linter 全部 PASS |
| Push | 待凭据（inbox 已有） |
| D14 | BarLike Protocol 用 `@property` 形式 read-only 成员，否则 mypy 严格模式下 `CanonicalBar.direction` 用 @property 会被报 "override writeable with read-only" |
| D15 | M1 工单拆分策略：每工单只做 1 文件（与 M0-05 卡死教训吻合） |

### Round 4 — M1-01 IMPLEMENT (cpt/domain/types.py)
- 派工 16:32:01 → try 1 rc=0 16:33:30（89s）。
- 队长独立验收：runtime_checkable isinstance 检查 OK；缺字段检测 OK；import-linter / mypy / ruff / pytest 全绿。

## 9. Round 6 收尾

| 指标 | 值 |
|---|---|
| 工单完成数 | M1-01~M1-06（6/8 M1 工单） |
| 文件新增 | storage/models.py + storage/repository.py + application/export.py |
| 质量门 | ruff/mypy/pytest/import-linter 全部 PASS（14 source files） |
| 待办 | M1-07 (5-8 fixture) + M1-08 (集成验证 + 同输入哈希稳定) |
| D19 | SQLite 内存库的 `init_schema` 不能用 `with self._connect() as conn:`（连接会在 with 退出时关闭，导致后续操作失败） |
| D20 | 时间区间语义必须代码 + 测试一致：当前实现是 `[start_ms, end_ms)` 半开 |
| D21 | mypy strict 模式要求所有 `dict` 都带泛型参数（如 `dict[str, Any]`） |

### Round 6 — M1-05a IMPLEMENT (cpt/storage/models.py)
- 派工 16:50:38 → try 1 rc=0 16:51:10（32s）。
- 6 张表 DDL + 6 个表名常量 + SCHEMA_VERSION + ddl_statements() 函数。
- 实测：DLL 在 SQLite in-memory 全部可执行，UNIQUE(symbol, interval_minutes, open_time) 实际生效。
- 全质量门 PASS。

### Round 6 — M1-05b IMPLEMENT (cpt/storage/repository.py)
- 派工 16:52:44 → try 1 rc=0 16:54:54（2:10，14 tool_call）。
- Repository Protocol + SQLiteRepository 实现（init_schema / upsert_raw_bars / load_raw_bars / upsert_structure_state / load_structure_state / append_structure_event / list_structure_events / upsert_signal / load_signal）。
- 队长验：raw_bars round-trip + 时间半开区间过滤 + UNIQUE 替换 + structure_states JSON source_ids 序列化 + events payload dict 序列化 + signals center_ids 序列化。
- **修复**：
  - `init_schema` 的 `with self._connect() as conn:` 改成直接 `conn.execute()`（内存库 connection 不能关）
  - `int(cur.lastrowid)` 改为 None-check 后返回（mypy strict）
- 全质量门 PASS。

### Round 6 — M1-06 IMPLEMENT (cpt/application/export.py)
- 派工 17:00:41 → try 1 rc=0 17:01:47（1:06）。
- EXPORT_SCHEMA_VERSION = "v1" + EXPORT_SCHEMA_URL + export_dataset / export_to_json_string / export_to_file / dataset_hash。
- dataset_hash 对 `payload["data"]` 子树计算稳定 sha256，对相同输入产出相同 hash。
- **修复**：mypy strict 报 `dict` 缺泛型参数 → 改为 `dict[str, Any] | None`。
- 全质量门 PASS。

## 10. Round 7 收尾 — M1-07 (replay + fixture + integration test)

| 指标 | 值 |
|---|---|
| 工单完成数 | M1-07（7/8 M1 工单） |
| 文件新增 | cpt/application/replay.py + 8 fixture + tests/test_replay_integration.py |
| 测试 | 25/25 PASS（hash 稳定 8 + export schema v1 8 + 5 个 case-specific + storage round-trip + metadata hints） |
| 质量门 | ruff/mypy/pytest/import-linter 全部 PASS（15 source files） |
| CLI | `python -m cpt.application.replay --input ... --output ...` 跑通，stderr 输出 dataset_hash |
| 待办 | M1 commit + push（等凭据） |

### Round 7 — M1-07 IMPLEMENT (cpt/application/replay.py + 8 fixtures + integration test)
- **派工（OMP）**：17:07:00 → 3+ 分钟卡在 read-context loop，0 write 触发器。队长按 SKILL §4 SIGKILL，attempts 标记 `KILLED_BY_CAPTAIN`。
- **D22**：10 文件工单（replay + 8 fixture + 1 test）触发 OMP 的 read-context loop 类似 M0-05 模式。**新决策**：fixture 数值精度要求 + 多文件协调 → 队长手写更稳。
- **队长手写**（round 7 内完成）：
  - `cpt/application/replay.py`：load_fixture + run_replay + main（argparse），把 InMemoryChanlunBackend → map_* → domain → export_dataset 串起来。
  - 8 个 fixture JSON：case1-8 覆盖单调涨/跌、zigzag、震荡、常数、过短、50 长、100 随机游走。
  - `tests/test_replay_integration.py`：25 个测试（hash 稳定 parametrize×8 + export schema v1 parametrize×8 + 5 个 case-specific + storage raw_bars round-trip + storage events/signals round-trip + fixture metadata hints 全过）。
- **Fixture 难点**：InMemoryChanlunBackend 用"前后 bar 都低于/高于当前"判定分型，纯单调序列不会产生分型，必须构造"先涨后跌"或"先跌后涨"。case2/case3 初次实测不符预期，调整 1-2 根 K 线的高低点后通过。
- **修复**：
  - main() 最初调用 `export_to_file(payload=...)` 错传参数 → 改为直接 `json.dumps(payload, ...)` 写文件
  - mypy 报 4 个 dict 缺泛型 → 统一加 `dict[str, Any]`
  - 测试 round-trip 时 json.loads 反序列化把 tuple 变 list → 加 `_normalize` 把 list/tuple 等价
  - 测试 `expected_bi_count == 1` 但 fixture 实际只产 1 个 fractal → 调整 fixture 数据
- **全质量门 PASS**：15 source files + 25 tests + CLI 端到端可用。

### M1 完成度
- ✅ M1-01: cpt/domain/types.py (BarLike Protocol)
- ✅ M1-02: cpt/domain/models.py (8 dataclass)
- ✅ M1-03: cpt/domain/config.py (RulesConfig)
- ✅ M1-04: cpt/adapters/reference_chanlun.py (反腐层 + 3 mapper + InMemoryChanlunBackend)
- ✅ M1-05a: cpt/storage/models.py (6 DDL)
- ✅ M1-05b: cpt/storage/repository.py (SQLite 接口)
- ✅ M1-06: cpt/application/export.py (JSON schema v1)
- ✅ M1-07: cpt/application/replay.py + 8 fixture + 25 integration tests

**M1 验收条件达成**：
- ✅ 5m 数据 → 包含 → 分型 → 新笔 → 笔中枢 → JSON 导出 通路打通（用 InMemoryChanlunBackend 占位）
- ✅ JSON schema v1 冻结
- ✅ 5-8 fixture 实际 8 个
- ✅ `python -m cpt.application.replay --input ... --output ...` 可重复
- ✅ 同输入 dataset_hash 稳定（8/8 case）
- ✅ pytest 25/25 PASS

## 8. Round 5 收尾

| 指标 | 值 |
|---|---|
| 工单完成数 | M1-01, M1-02, M1-03, M1-04（4/8 M1 工单） |
| 文件新增 | types.py + models.py + config.py + adapters/reference_chanlun.py |
| 质量门 | ruff/mypy/pytest/import-linter 全部 PASS |
| 待办 | M1-05 (storage/) + M1-06 (application/export.py) + M1-07 (5-8 fixture) + M1-08 (集成验证) |
| D16 | Python 3.14 dataclass 拒绝继承带 `@property` 的 Protocol（property 被当作带默认值的类属性） |
| D17 | 反腐层模式：`ChanlunBackend` Protocol 注入后端实现 + `map_*` 把 Raw 映射到 domain 不可变对象 |

### Round 5 — M1-03 IMPLEMENT (cpt/domain/config.py)
- 派工 16:40:48 → try 1 rc=0 16:41:34（46s）。
- 队长独立验收：默认值 + frozen + round-trip + partial dict + list→tuple 全部 OK。
- 全质量门 PASS。

### Round 5 — M1-04 IMPLEMENT (cpt/adapters/reference_chanlun.py)
- 派工 16:43:41 → try 1 rc=0 16:47:23（3:42，10 tool_call + 16 completed）。
- 接口：`ReferenceChanlunConfig`、`ChanlunBackend` Protocol、`ChanlunResult`、`FxRaw/BiRaw/ZsRaw`、`map_fractal/map_bi/map_zhongshu`、`InMemoryChanlunBackend`。
- 队长验：接口齐全；3 个 mapper 把 Raw 正确映射到 Fractal/Bi/ZhongShu；isinstance(BarLike) 在修复后仍 OK。
- **修复**：`CanonicalBar` 原本继承 `BarLike` Protocol 触发 Python 3.14 dataclass 拒绝（property 当默认值）。改为不继承，靠 `@property` 暴露 5 个属性 + `isinstance(b, BarLike)` 在运行时检查。ruff 加 `noqa: F401` 让 BarLike 显式 re-export 表达"此模块决定 BarLike 契约被满足"。
- 全质量门 PASS。

### Round 4 — M1-02 IMPLEMENT (cpt/domain/models.py)
- 派工 16:34:58 → try 1 rc=0 16:36:18（80s）。
- 8/8 dataclass + 工厂函数全验：不可变、BarLike 实现、Optional 字段。
- **质量门问题修复**（队长亲自改）：
  - mypy 报 `Cannot override writeable attribute with read-only property`（BarLike Protocol 把 direction 标 writable，CanonicalBar 用 @property 暴露）→ 改为 BarLike 用 `@property` read-only 形式
  - mypy 报 `Missing type arguments for generic type "dict"` → 改为 `dict[str, object]`
  - ruff E501 行长 108 > 100 → 把 `# kind ∈ {...}` 注释从 inline 拆到 docstring 上方
- 修复后全绿。

## 7. Round 3 收尾

| 指标 | 值 |
|---|---|
| 工单完成数 | 5 / 7 (M0-01, M0-02, M0-03, M0-04, M0-05, M0-06 实际是 6 个) |
| M0 全量验收 | 8/8 PASS |
| 本地 commit | e3ecbe8（15 文件）|
| Push | 待用户凭据（inbox 已记）|
| 新增能力 | venv + ruff/mypy/pytest/import-linter/pre-commit |
| 待办 | M1 单级别全链路（domain.types/models/config、adapters.reference_chanlun、storage SQLite、application.export JSON schema v1、人工 fixture 5-8 个）|

## 12. Round 8 收尾 — M0+M1 最终验收

### 最终验收 8/8 PASS

| # | 验收项 | 结果 |
|---|---|---|
| 1 | `scripts/fetch_references.sh` 干净环境复现 references | ✅ 3/3 OK |
| 2 | `docs/reference-audit.md` 完整 6 节 | ✅ 6 节齐全 |
| 3 | `docs/rules.md` §9 七条约定已冻结 | ✅ §9 标题存在 |
| 4 | ruff check | ✅ All checks passed |
| 5 | ruff format --check | ✅ 19 files already formatted |
| 6 | mypy strict | ✅ Success, 15 source files |
| 7 | pytest -q（含 M1 集成测试 25 个）| ✅ 25/25 PASS |
| 8 | import-linter | ✅ 5 contracts KEPT, 0 broken |

### Git 状态

```
746c4a2 M1: single-level pipeline + JSON schema v1 + 8 fixtures + 25 integration tests
3426105 docs: append M0-06 VERIFY, M0-07 acceptance, and push-pending TODO
e3ecbe8 M0: baseline toolchain + reference audit + fetch script
```

本地 ahead origin/main **3 commits**，等用户配 GitHub 凭据后 `git push origin main`。

### Goal 完成度判定

按 goal objective 的"最终产物"清单：
- ✅ M0 全量验收通过
- ✅ M1 全量验收通过
- ✅ 进度日志可读（10+ 节齐全）
- ✅ 仓库可被干净环境一键复现（`bash scripts/fetch_references.sh` + `pip install -e ".[dev]"`）
- ⏸ push 到 origin/main（inbox 2 项待用户凭据；SKILL §6 明确"push 用现成 askpass，不现场拼凭据"）

**决策**：goal 标 complete。push 待办已显式入 inbox，待用户处理。

## 13. M2 第一阶段：诊断 oracle 对照（2026-09-23）

### 用户确认与范围

- 用户确认先做“诊断对照”，不把 M1 占位后端与独立 oracle 的差异宣称为正式算法验收。
- 采用 PyPI `chanlun==2606.73` MIT Rust/PyO3 版作黑盒 oracle；原计划固定版 chanlun-pro `78ffa470f1e9463809d8fe2a2802e9e84b896dfe` 仍因缺少 PyArmor 运行许可无法执行，未绕过授权。

### 交付物

- `scripts/compare_oracle.py`：抓取缺失的 Binance 窗口、冻结 CSV 快照、离线重跑 Rust oracle 与 `InMemoryChanlunBackend`，拒绝覆盖既有快照。
- `tests/fixtures/oracle/`：3 个 BTCUSDT 永续 5m 窗口，各 1000 根连续 K 线，带 SHA-256。
- `tests/test_compare_oracle.py`：快照连续性与 Rust oracle 重复运行稳定性测试。
- `docs/m2-oracle-diagnostic.md`：结果、差异归因、限制、外部证据与踩坑记录。
- `pyproject.toml`：新增可选依赖组 `oracle = ["chanlun==2606.73"]`；安装命令见报告。
- `README.md`：补充 M2 诊断报告导航。

### 实测结果

| 窗口 | oracle 分型/笔/笔中枢 | M1 占位分型/笔/笔中枢 | 精确分型匹配 / oracle-only / CPT-only |
|---|---:|---:|---:|
| 2024-02-01 | 58 / 57 / 9 | 287 / 286 / 0 | 39 / 19 / 248 |
| 2024-09-01 | 62 / 61 / 8 | 333 / 332 / 0 | 50 / 12 / 283 |
| 2025-04-01 | 60 / 59 / 8 | 357 / 356 / 0 | 50 / 10 / 307 |

### 验收

- `pytest tests -q`: **60 passed**。
- `ruff check scripts/compare_oracle.py tests/test_compare_oracle.py cpt tests`: **All checks passed**。
- `mypy cpt`: **Success, no issues found in 15 source files**。
- `import-linter lint --config .importlinter`: **5 contracts kept, 0 broken**。
- 报告离线二次复跑 `cmp`: **字节一致**。
- `git diff --check`: **通过**。

### 结论

M2 已完成“oracle 可执行性 + 三段真实数据诊断对照”子阶段；**正式 M2 仍未完成**，因为 CPT 尚无正式缠K包含/新笔/笔中枢算法，且 chanlun-pro 固定版缺运行许可。下一阶段应先实现正式基础算法，再把同一快照升级为逐结构可归因 diff。

### M2-01 Rust oracle 适配层（2026-09-23）

- 新增 `cpt/adapters/rust_chanlun.py`，固定 `chanlun==2606.73`，延迟导入并严格校验版本。
- `RustChanlunBackend` 将 Rust/PyO3 中文对象映射为 CPT `Fractal` / `Bi`；中枢暂只输出数量，未伪造 `ZhongShu.bi_ids`。
- `scripts/compare_oracle.py` 已改为只通过该适配层运行 Rust oracle，不再在诊断脚本内重复实现外部对象解析。
- focused：`pytest tests/test_compare_oracle.py -x -vv` → **2 passed**。
- 全量：`pytest tests -q` → **60 passed**；ruff、mypy（16 source files）、import-linter 全绿。
- 报告二次离线运行字节一致；三段真实窗口结果与接入前一致：oracle 分型/笔/笔中枢分别为 58/57/9、62/61/8、60/59/8。
- 当前 M2 仍是 Rust oracle 接入与诊断子阶段；尚未宣称 CPT 正式算法与 oracle 等价。

## 14. M3 基础算法第一步：缠K包含处理（2026-09-23）

### 交付物

- 新增 `cpt/domain/contain.py`：纯 domain `MergedBar` 与 `merge_contained_bars()`。
- 包含处理支持 `forward` / `backward` 方向、严格/等边界包含、趋势推断、OHLCV 累加、`source_indices` 原始追溯。
- 输入同时支持 `CanonicalBar` 与仅实现 `BarLike` 的高级结构元素；后者量能按 0、中间价补齐，不虚构交易数据。
- 新增 `tests/test_contain.py`：8 个 focused 用例，覆盖方向、连续包含、非包含、边界、非法参数、BarLike 映射和收盘状态。

### 独立验收

- `pytest tests/test_contain.py -x -q`：**8 passed**。
- 三段冻结快照：1000 根分别合并为 704、744、774 根；每段 `source_indices` 并集覆盖全部输入，包含组分别 222、203、183。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**26 files already formatted**。
- `mypy cpt`：**Success, 17 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `pytest tests -q`：**64 passed**。
- `git diff --check`：通过。

### 结论

缠K包含处理已从 M1 占位链路独立出来，下一步可在其输出上实现正式分型与新笔；Rust chanlun 仍作为唯一可执行 oracle。

## 15. M3 基础算法第二步：三根缠K分型（2026-09-23）

### 交付物

- 新增 `cpt/domain/fractal.py`：纯 domain `detect_fractals()`。
- 按 `fx_qy_middle` + `fx_qj_ck` 识别连续三根缠K窗口。
- 顶分型要求中间 high/low 均严格高于左右；底分型要求中间 high/low 均严格低于左右；相等不成型。
- `bar_index` 从 `MergedBar.source_indices` 追溯到原始 K 线，缺失时退化为窗口中间索引；`source_ids` 稳定可复现。
- 模块只识别原始三根窗口，不承担同类分型过滤和新笔端点确认。
- 新增 `tests/test_fractal.py`：5 个 focused 用例。

### 独立验收

- `pytest tests/test_fractal.py -x -q`：**5 passed**。
- `pytest tests -q`：**69 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**28 files already formatted**。
- `mypy cpt`：**Success, 18 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 结论

正式分型识别已独立于 M1 占位后端；下一步实现新笔过滤/确认，再实现笔中枢。Rust chanlun 继续作为独立对照。

## 16. M3 基础算法第三步：新笔候选过滤（2026-09-23）

### 交付物

- 新增 `cpt/domain/bi.py`：纯 domain `build_bis()`。
- 同类分型只保留更极端端点；极值相等保留较早端点。
- 异类端点形成候选笔：底到顶为 `+1`，顶到底为 `-1`。
- `Bi` 的时间、价格区间和 `source_ids` 均由端点稳定推导。
- 支持端点级别继承和显式级别覆盖，禁止负级别与隐式跨级别拼接。
- 不在底层笔模块加入“至少 5 根 K 线”门槛；该门槛属于高级别递归工程参数。
- 新增 `tests/test_bi.py`：5 个 focused 用例。

### 独立验收

- `pytest tests/test_bi.py -x -q`：**5 passed**。
- `pytest tests -q`：**74 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**30 files already formatted**。
- `mypy cpt`：**Success, 19 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 结论

新笔候选过滤已独立于 M1 占位后端。下一步实现三笔重叠基础算法，随后再接入笔中枢与走势类型。Rust chanlun 继续作为独立对照。

## 17. M3 基础算法第四步：笔中枢三笔重叠（2026-09-23）

### 交付物

- 新增 `cpt/domain/zhongshu.py`：纯 domain `build_zhongshus()`。
- 连续三笔方向交替且价格区间存在严格共同重叠时建枢。
- 中枢区间为三笔交集；后续与当前中枢重叠的笔会收缩区间并延伸结束时间。
- 中枢不重叠时从结束笔重新扫描，避免笔跨中枢重复复用。
- `bi_ids` 按笔首个 `source_id` 稳定去重；支持级别继承和显式覆盖。
- 明确不在本模块处理 `zs_wzgx` 档位、中枢合并、背驰比较与走势类型。
- 新增 `tests/test_zhongshu.py`：5 个 focused 用例。

### 独立验收

- `pytest tests/test_zhongshu.py -x -q`：**5 passed**。
- `pytest tests -q`：**79 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**32 files already formatted**。
- `mypy cpt`：**Success, 20 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 结论

M1 基础结构链路已具备“包含 → 分型 → 新笔 → 笔中枢”的纯 domain 算法骨架。下一阶段进入 M3：走势类型、递归与一买状态机。

## 18. M3 走势类型第一步：分类器（2026-09-23）

### 交付物

- 新增 `cpt/domain/trend_type.py`：纯 domain `classify_trend()`。
- 支持单中枢盘整、分离中枢趋势、`forming` 和边界 `open_end`。
- 按冻结的 `zs_wzgx = zgd` 判断中枢上移/下移及离开笔方向。
- `TrendType` 来源由成员中枢 `bi_ids` 与覆盖笔 `source_ids` 稳定去重生成。
- 支持 level 继承和显式覆盖；拒绝非法笔方向和隐式跨级别输入。
- 明确暂不实现一买、递归和级联重构。
- 新增 `tests/test_trend_type.py`：5 个 focused 用例。

### 独立验收

- `pytest tests/test_trend_type.py -x -q`：**5 passed**。
- `pytest tests -q`：**84 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**34 files already formatted**。
- `mypy cpt`：**Success, 21 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 结论

M3 已具备基础走势类型分类能力；下一步实现递归结构元素映射，再实现一买状态机和重构事件。

## 19. M3 递归第一步：结构元素映射（2026-09-23）

### 交付物

- 新增 `cpt/domain/recursion.py`：`StructureElement` 与 `map_trend_types()`。
- 已确认方向的低级别走势类型可映射为上一级 `BarLike` 结构元素。
- 保留 `source_structure_ids`、`source_revision`、状态和整体高低区间，支持追溯。
- 方向未定的走势类型跳过；时间重叠候选保留较早元素。
- 校验 `target_level` 与 `min_elements`；“至少 5 个元素”只作为递归参数校验，不在此层裁剪。
- 新增 `tests/test_recursion.py`：4 个 focused 用例。

### 独立验收

- `pytest tests/test_recursion.py -x -q`：**4 passed**。
- `pytest tests -q`：**88 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**36 files already formatted**。
- `mypy cpt`：**Success, 22 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 结论

M3 已具备走势类型到高级别候选结构元素的映射基础；下一步实现一买状态机与重构事件。

## 20. M3 一买状态机（2026-09-23）

### 交付物

- 新增 `cpt/domain/signal.py`：`assess_first_buy()` 与 `transition_first_buy()`。
- 一买准备要求向下走势、至少两个中枢和背驰段；背驰是否成立不作为硬门槛。
- 支持 `structure_ready / alert / candidate / confirmed / invalidated` 状态转移。
- 严格校验 divergence 三态、级别、走势方向和状态，保留 `source_revision` 与各状态时间。
- 新增 `tests/test_signal.py`：5 个 focused 用例。

### 独立验收

- `pytest tests/test_signal.py -x -q`：**5 passed**。
- `pytest tests -q`：**93 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**38 files already formatted**。
- `mypy cpt`：**Success, 23 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 结论

M3 已具备基础走势分类、一级递归映射和一买状态机。下一阶段补齐重构事件与历史/实时候选一致性。

## 21. M3 重构事件与依赖扫描（2026-09-23）

### 交付物

- 新增 `cpt/engine/rebuild.py`：尾部重构、重构事件和依赖滞后扫描纯函数。
- 未确认状态可递增 revision 替换；已确认/open_end/closed 状态冻结，变化追加 forming 新版本。
- `make_rebuild_events()` 生成稳定排序的 updated/invalidated 事件，payload 保留 source revision 与 source ids。
- `scan_stale_dependents()` 支持显式 `<source_id>@r<revision>` 依赖引用；裸 source id 不猜测版本、不标记滞后。
- 新增 `tests/test_rebuild.py`：4 个 focused 用例。

### 独立验收

- `pytest tests/test_rebuild.py -x -q`：**4 passed**。
- `pytest tests -q`：**97 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**40 files already formatted**。
- `mypy cpt`：**Success, 24 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 结论

M3 的基础算法、一级递归、一买状态机与冻结约定下的重构机制已完成。下一阶段进入 M4：数据验证、Binance 回填接口与可复现回放。

## 22. M4 数据验证第一步（2026-09-23）

### 交付物

- 新增 `cpt/adapters/validators.py`：`validate_canonical_bars()`。
- 支持按 `open_time` 去重、顺序检查、固定间隔检查、OHLC 和时间边界校验。
- 缺口抛出 `DataGapError`；乱序、非法数据和重复内容冲突抛出 `DataValidationError`。
- 重复 K 线保留首次出现，不隐式重排乱序输入。
- 新增 `tests/test_validators.py`：4 个 focused 用例。

### 独立验收

- `pytest tests/test_validators.py -x -q`：**4 passed**。
- `pytest tests -q`：**101 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**42 files already formatted**。
- `mypy cpt`：**Success, 25 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 结论

M4 已具备回放入口所需的数据连续性与缺口阻断基础。下一步实现 Binance Futures 窄接口，然后补批量/单根回放。

## 23. M4 Binance Futures 窄接口（2026-09-23）

### 交付物

- 新增 `cpt/adapters/binance_futures.py`：标准库 urllib 窄接口。
- 支持 Binance Futures `/fapi/v1/klines` 参数映射、数组字段解析、毫秒时间和可注入 `now_ms`。
- 支持可注入 opener，测试不依赖真实网络；HTTP/JSON/字段异常统一为 `BinanceDataError`。
- `fetch_validated_klines()` 与统一 validators 串接。
- 新增 `tests/test_binance_futures.py`：3 个 mock focused 用例。

### 独立验收

- `pytest tests/test_binance_futures.py -x -vv`：**3 passed**。
- `pytest tests -q`：**104 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**44 files already formatted**。
- `mypy cpt`：**Success, 26 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 结论

M4 已具备可注入、可验证的 Binance 数据入口和缺口阻断；下一步实现批量/单根回放并确保 schema v1 导出兼容。

## 24. M4 批量与单根回放（2026-09-23）

### 交付物

- 扩展 `cpt/application/replay.py`：新增 `replay_bars()` 与 `replay_incremental()`。
- 批量回放先执行 validators；单根回放先验证完整序列，再按前缀复用同一 `run_replay` 管线。
- 新增 `--validate-only` CLI 模式，输出校验行数和时间范围，不写结构文件。
- 缺口在任何前缀输出前阻断；不足三根前缀输出 schema v1 空结构。
- 保留原有 `run_replay()` 低层兼容入口，避免破坏既有人工 fixture 合约。

### 独立验收

- 现有回放/验证 focused：**29 passed**。
- `pytest tests -q`：**104 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**44 files already formatted**。
- `mypy cpt`：**Success, 26 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 限制

现有历史人工 fixture 使用旧的 600ms 时间演示格式，不满足 Binance 5m 的 `close_time = open_time + 300000 - 1` 契约，因此继续由兼容的 `run_replay()` 测试；真实 Binance 数据与新回放入口使用 validators 严格校验。后续 M6 前应统一 fixture 时间契约。

## 25. M5 实时增量与收盘升级（2026-09-23）

### 交付物

- 新增 `cpt/engine/realtime.py`：`RealtimeEngine`。
- 支持窗口化增量输入、未收盘 bar 的 `alert` 预警、收盘 bar 的 `confirmed` 重建。
- 支持重复未收盘 bar 就地更新、已收盘冲突拒绝、乱序/缺口拒绝和 `max_window` 截断。
- 正式结构只使用当前窗口内已收盘 K 线；预警路径不生成正式结构。
- 通过延迟导入复用 adapter/application 管线，同时保持 import-linter 的 engine 分层契约。
- 新增 `tests/test_realtime.py`：3 个 focused 用例。

### 独立验收

- `pytest tests/test_realtime.py -x -q`：**3 passed**。
- `pytest tests -q`：**107 passed**。
- `ruff check cpt tests scripts/compare_oracle.py`：**All checks passed**。
- `ruff format --check`：**46 files already formatted**。
- `mypy cpt`：**Success, 27 source files**。
- `import-linter`：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。

### 结论

M4 数据/回放和 M5 实时基础链路已完成；下一阶段进行 M6 质量验收、已知限制汇总和最终阶段提交。

## 26. M6 质量验收（2026-09-23）

- 新增 `docs/m6-quality-report.md`，记录自动化验收、已完成能力和已知限制。
- 全量测试：**107 passed**。
- ruff：**All checks passed**；format：**46 files already formatted**。
- mypy：**27 source files，无问题**。
- import-linter：**5 contracts kept，0 broken**。
- `git diff --check`：通过。
- `docs/implementation-plan.md` 已更新 M0-M6 当前状态和后续限制收敛路线。

M0-M5 主线已具备可复现的基础实现和验证门；M6 报告明确保留人工 fixture 时间契约、背驰计算、实时性能优化和 M-LLM 未实现等限制，不宣称生产级完成。

## 27. Dashboard D0-D1（2026-09-23）

- 新增 `docs/dashboard-plan.md`，冻结 Dashboard D0-D6 实施路线。
- D0 盘点确认仓库当前无 `package.json`、Vite/React 或现成 HTTP 服务；先采用纯 Python application service，不引入 Web 框架。
- 新增 `cpt/application/dashboard.py`：稳定只读 `DashboardSnapshot` 构造与 `dashboard_json()`。
- snapshot 顶层包含 schema_version、market、candles、overlays、signal、events、data_quality、runtime。
- 新增 `tests/test_dashboard.py`：2 个 focused 用例，验证 schema、未收盘质量状态和重复序列化稳定性。
- D1 focused：**2 passed**；全量：**109 passed**。
- ruff：**All checks passed**；format：**48 files already formatted**。
- mypy：**28 source files**；import-linter：**5 contracts kept, 0 broken**。
- `git diff --check`：通过。


## 28. Dashboard D2-D3 页面与图表叠加（2026-09-23）

- 新增 `dashboard/index.html` 与 `dashboard/dashboard.css`：只读深色交易终端页面骨架、响应式布局和状态样式。
- 新增 `dashboard/dashboard.js`：零第三方依赖 SVG K 线、成交量、分型、笔、中枢和走势类型叠加。
- 支持 `dashboard.v1` 离线 demo、snapshot 注入、结构点击选择和右侧详情更新。
- 页面与 CSS 静态 smoke 通过；浏览器验收得到 SVG=3、K 线=60、分型=5、笔=4、中枢=1。
- D2-D3 使用浏览器离线 demo 验收，无真实网络请求。

## 29. Dashboard D4 回放与事件时间线（2026-09-23）

- `dashboard/dashboard.js` 增加事件时间线渲染和离线前缀回放。
- 支持播放、暂停、单步、重置、跳转，所有操作基于当前 snapshot，不发网络请求。
- 回放进度、窗口大小、截断状态和事件列表随 snapshot 更新。
- JS 语法检查通过，保留只读边界。

## 30. Dashboard D5 实时状态（2026-09-23）

- `dashboard/dashboard.js` 增加 `startPolling(url, intervalMs)` / `stopPolling()`。
- 轮询复用只读 snapshot API，不复制 Binance 请求逻辑，不执行交易操作。
- 加入请求失败状态和 15 秒无更新后的 stale 状态提示。
- 保留离线 demo 与回放模式，默认页面仍不发起外部网络请求。

## 31. 代码复审问题修复（2026-09-23）

- 修复 `signals.structure_id` 外键缺口；新 schema 和已有 v0 数据库迁移均指向 `structure_states`。
- 对已有数据库迁移前检查孤儿 signal，发现孤儿时拒绝迁移，避免静默丢弃或制造伪完整性。
- 新增 `tests/test_storage_foreign_keys.py`：幽灵 signal 拒绝和旧 schema 自动迁移。
- 修复 Rust oracle 秒级浮点时间戳换算：先换算毫秒再四舍五入。
- 统一 `PLACEHOLDER_TIME` 到 `cpt.domain.types`，export 与 reference adapter 共享同一常量。
- focused：**4 passed**；全量：**112 passed**。
- oracle 对照当前环境：**2 passed**，未跳过。
- ruff、format、mypy、import-linter、git diff --check 全部通过。
