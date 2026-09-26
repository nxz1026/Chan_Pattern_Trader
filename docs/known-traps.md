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

> **唯一不一致的地方**：`ccxt`/`czsc`/`wbt` 缺失时会抛带安装指引的自定义异常，
> 而 `psycopg`（`a_share_local.py:103,190`）和 `pandas`/`plotly`
> （`canvas_wbt.py:158,345`）是**裸 import**，缺了只报
> `ModuleNotFoundError`。想改进的话给它们也加同样的守卫即可。

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

## 8. `docs/pending-wiring.md` 里 16 个模块"零生产导入"是产品决策

不是死代码，是**尚未接线**。2026-09-25 用导入图可达性独立复核过
（根 = `cpt.web.*` + `scripts/*`）：66 个模块中 46 个可达，20 个不可达 ——
20 个里 16 个在这份清单，另 4 个是 `__init__.py` 包标记，**清单没有漏项**。

风险是产品级的：长期不接线会变成事实死代码。**要删要接，是产品决策，不是清理。**
