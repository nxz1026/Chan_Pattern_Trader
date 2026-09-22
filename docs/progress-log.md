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
