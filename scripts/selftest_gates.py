#!/usr/bin/env python3
"""门禁自检套件（R45 P1-2）：**造已知错例，验每个门禁抓不抓得到**。

## 为什么要这个

R45 一天里，我自制的检查器/门禁出了 **15+ 次**错，而且**几乎每一次都是
「报了假阳性」或「漏报」之后才发现**：

  - 路径存在性拿去和「代码文件集合」比 ⇒ 165 条假阳性
  - 把「文档在声明某物不存在」当成漂移
  - 恒 ``return 0`` 的门禁 ⇒ CI 上永远绿，等于没有门禁
  - ``__main__.py`` 同名文件取错 ⇒ 18 条假阳性
  - 正则大小写敏感 ⇒ ``startPolling`` **漏报成「无重复」**
  - 朴素字符串计数判「有没有第二份实现」⇒ 数出 5（把 import 和注释也算进去）
  - 只排除 ``#`` 行 ⇒ docstring 里的提及不算注释，照样数进去

**这些错，没有任何一条是「跑起来红了」暴露的** —— 全是「结果不对劲，
我多看了一眼」才发现。

⇒ 门禁自己也要被验。每个门禁配一个**已知错例**，
跑之前先确认「它真能抓到」—— 否则「全绿」只是**它什么都没查**。

## 怎么做到不维护两份逻辑

所有门禁都用 ``ROOT = Path(__file__).resolve().parents[1]``。
所以本套件把**真脚本原样复制**进一个临时仓库，再往那个临时仓库里
注入已知违规 —— 脚本的 ``ROOT`` 自动跟着变，**跑的仍是同一份代码**。

## 用法

    python scripts/selftest_gates.py            # 全部
    python scripts/selftest_gates.py -v         # 打印注入内容
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


# ── 造临时仓库的构件 ──────────────────────────────────────────
def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _pkg(root: Path, rel: str, body: str) -> None:
    _write(root / "cpt" / rel, body)


# ── 各门禁的「已知错例」 ──────────────────────────────────────
# 每个 fixture 返回 (门禁脚本名, 往临时仓库里写什么, 期望在输出里出现的片段)
def fx_doc_drift(root: Path) -> tuple[str, str]:
    """③ 文档漂移：architecture 里的「✅ 已删除」声明与实际矛盾。"""
    # ⚠️ 夹具必须备齐**其它类要 import 的模块**，否则门禁会先崩在别处，
    # 轮不到 E 类 —— 第一版就因此「漏报」，看起来像门禁坏了，其实夹具不全。
    _pkg(root, "domain/__init__.py", "")
    _pkg(
        root,
        "domain/config.py",
        "from dataclasses import dataclass\n"
        "@dataclass(frozen=True)\n"
        "class RulesConfig:\n    min_bi_len: int = 4\n",
    )
    _write(root / "docs/architecture.md", "### 2.1\n`cpt/domain/x.py` **已整层删除**（真的删了）\n")
    _pkg(root, "domain/x.py", "# 还在\n")
    return "check_doc_drift.py", "x.py"


def fx_all_claims(root: Path) -> tuple[str, str]:
    """④ 全量断言：文档引用一个不存在的文件。"""
    _write(root / "README.md", "见 `cpt/domain/does_not_exist.py`。\n")
    return "check_all_claims.py", "does_not_exist.py"


def fx_doc_counts(root: Path) -> tuple[str, str]:
    """⑤ 计数：architecture §2.1 的行数与实际不符。"""
    _pkg(root, "domain/a.py", "x = 1\ny = 2\n")
    _write(
        root / "docs" / "architecture.md",
        "## 2.1\n| 层 | 文件 | 行数 | 复盘状态 |\n|---|---:|---:|---|\n"
        "| `domain/` | 1 | 999 | x |\n",
    )
    # 期望串取门禁**真实**的失败措辞 —— 第一版写 "domain/"（带斜杠），
    # 而它打印的是 "cpt/domain" / "domain 层" ⇒ 永远匹配不上，看着像漏报。
    return "check_doc_counts.py", "对不上"


def fx_enqueue_unique(root: Path) -> tuple[str, str]:
    """⑥ 入队骨架：故意加**第三份**入队调用点。

    ⚠️ **这个夹具必须跑真门禁**（第一版把 ``check_enqueue_skeleton_unique.py``
    整个覆盖成手写桩，桩只会数「== 2 就退出 0」，于是真门禁**能不能红从未被证明** ——
    而 ``selftest_gates.py`` 报的是「抓到了 ✅」，比不测更坏）。
    ⇒ 现在的做法：**只注入错例**（``llm_cases.py``），一个字节都不改门禁本身。

    注入的 ``llm_cases.py`` 先按真门禁的 5 项判据**摆成全绿**，再加一处
    「第三份入队」—— 这样红的原因只有一个（重复实现），不是夹具压根不合法。
    """
    _pkg(
        root,
        "application/llm_cases.py",
        "def _enqueue_and_submit(conn, row, subject_id=''):\n"
        "    _write(conn, enqueue_call, row)\n"
        "    _existing_call_id(conn, row)\n"
        "    _bootstrap(conn)\n"
        "    submit(row)\n"
        "    return None\n"
        "\n"
        "def _write(conn, fn, row):\n"
        "    return None\n"
        "\n"
        "def explain_structure(conn, row):\n"
        "    _enqueue_and_submit(conn, row)\n"
        "\n"
        "def summarize_recommendation(conn, row):\n"
        "    _enqueue_and_submit(conn, row)\n"
        "\n"
        "def _legacy_explain(conn, row):\n"
        "    # 抄了一份的入队入口 —— 真门禁应当抓到「3 ≠ 2」\n"
        "    _enqueue_and_submit(conn, row)\n",
    )
    return "check_enqueue_skeleton_unique.py", ""


def fx_poll_unique(root: Path) -> tuple[str, str]:
    """⑦ 前端轮询：自己写一个 poll 循环。"""
    _write(
        root / "dashboard/x.js",
        "async function pollThing() {\n"
        "  await fetch('/x');\n"
        "  window.setTimeout(pollThing, 1000);\n"
        "}\n",
    )
    _write(root / "README.md", "x\n")
    return "check_job_poll_unique.py", "pollThing"


# ── 门禁①（SQL 分层）──────────────────────────────────────────
def fx_sql_layering(root: Path) -> tuple[str, str]:
    """在 **禁入层**（``cpt/domain``）里塞一条 ``cur.execute("SELECT ...")``。

    门禁①的判据是「SQL 出现在 domain/application/web/llm 即违规」，
    �� ``import psycopg``（实测：该门禁正是为了抓这种**没有 import 也能写 SQL**
    的越界）。
    """
    _write(
        root / "cpt/domain/bad.py",
        "def fetch(conn):\n"
        "    with conn.cursor() as cur:\n"
        '        cur.execute("SELECT * FROM public.derived_bar")\n'
        "        return cur.fetchall()\n",
    )
    _write(
        root / "cpt/storage/ok.py",
        "def fetch(conn):\n"
        "    with conn.cursor() as cur:\n"
        '        cur.execute("SELECT * FROM public.cpt_run_metric")\n'
        "        return cur.fetchall()\n",
    )
    return "check_sql_layering.py", "cpt/domain"


# ── 门禁②（store 失败语义）────────────────────────────────────
def fx_storage_failure(root: Path) -> tuple[str, str]:
    """在 store 里放一处「catch 住 DB 异常并 ``return None``」。

    这正是 R45 storage 复盘查到的两个真 bug 的形态 ——
    PostgreSQL 里事务中一条语句失败会让**同连接后续全部 aborted**，
    吞掉异常不是降级，是把局部失败放大成整页失败。
    """
    _write(
        root / "cpt/storage/bad_store.py",
        "def load_x(conn):\n"
        "    try:\n"
        "        with conn.cursor() as cur:\n"
        '            cur.execute("SELECT 1")\n'
        "            return cur.fetchone()\n"
        "    except Exception:\n"
        "        return None\n",
    )
    return "check_storage_failure_semantics.py", "bad_store"


# ── 门禁⑧（bundle 同步）───────────────────────────────────────
def fx_bundle_sync(root: Path) -> tuple[str, str]:
    """造一个**已生成**的 bundle，然后改源文件 ⇒ 必须报「不同步」。

    ⚠️ 这个夹具要**先生成**再改，否则 ``--check`` 找不到 bundle 会
    因「文件不存在」而报错 —— 那是另一条路径，测不到「改了没重建」这个真问题。

    ⚠️⚠️ **第一版把真门禁整个覆盖成了手写桩**（自己实现 ORDER、HEAD/FUNCS/TAIL
    解析、比对），于是「真 builder 能不能报 STALE」**从未被证明**，而自检却报
    「抓到了 ✅」。桩测的是桩，**比不测更坏**（它给出虚假的安心感）。
    ⇒ 现在的做法：只造源文件，**跑真 builder**（``run_one`` 已把真脚本复制进来了），
    一个字节都不改门禁本身。

    真 builder 的前提（照抄它的契约，不简化）：7 个 ``ORDER`` 文件**全部**要有
    ``// <<<HEAD`` / ``// <<<FUNCS`` 哨兵，且**至少一个**要有 ``// <<<TAIL``
    （缺了它直接 ``SystemExit('没有模块带 <<<TAIL')``）。
    """
    import re as _re

    ords = _re.findall(
        r'^[A-Z_]+ = \(\n((?:    "[^"]+",\n)+)\)',
        (root / "scripts/build_dashboard_bundle.py").read_text(encoding="utf-8"),
        _re.M,
    )
    names = _re.findall(r'"([^"]+)"', ords[0]) if ords else []
    assert names, "读不到 builder 的 ORDER —— 夹具不能瞎猜文件清单"
    for i, n in enumerate(names):
        _write(
            root / "dashboard" / n,
            f"// <<<HEAD\nconst A = 1;\n// >>>HEAD\n"
            f"// <<<FUNCS\nfunction fn_{i}() {{ return A; }}\n// >>>FUNCS\n"
            # TAIL 放最后一个：builder 取「最后一个带 TAIL 的文件」。
            + ("// <<<TAIL\nwindow.__boot = true;\n// >>>TAIL\n" if i == len(names) - 1 else ""),
        )
    import subprocess as _sp

    # 先用**真** builder 生成一份，再改源文件
    _sp.run(
        [sys.executable, str(root / "scripts/build_dashboard_bundle.py")],
        capture_output=True,
        text=True,
    )
    # 现在**改源文件**而不重建 ⇒ --check 必须报不同步。
    # ⚠️ 改的必须是 **FUNCS**，不能改 HEAD ——
    # builder 的 head 只取**第一个**文件（``if i == 0: h = mh.group(1)``），
    # 改第二个文件的 HEAD **不进输出** ⇒ bundle 仍然同步 ⇒ 报 OK。
    # 第一版就这么写错了，夹具自己是个假阴性。
    _write(
        root / "dashboard" / names[0],
        "// <<<HEAD\nconst A = 1;\n// >>>HEAD\n"
        f"// <<<FUNCS\nfunction fn_0() {{ return A + 1; }}\n// >>>FUNCS\n"
        + ("// <<<TAIL\nwindow.__boot = true;\n// >>>TAIL\n" if len(names) == 1 else ""),
    )
    return "build_dashboard_bundle.py", "不同步"


def fx_script_tags(root: Path) -> tuple[str, str]:
    """造一个**未闭合**的 ``<script>`` —— R45 真的踩过。

    后果特别隐蔽：浏览器把后续标签当脚本文本吞掉 ⇒ 那个文件
    **从未被请求**、对应全局恒 undefined ⇒ 功能静默消失，
    而页面正常、console 零报错。
    """
    _write(
        root / "dashboard/index.html",
        "<html><body>\n"
        '<script src="./a.js" defer>\n'
        '<script src="./cpt_job.js"></script></script>\n'
        "</body></html>\n",
    )
    return "check_all_claims.py", "script"


def fx_ci_workflow(root: Path) -> tuple[str, str]:
    """⑩ workflow 缩进：造 5 个被 ``run: |`` 吞掉的 step —— R51 真的踩过。

    这个错最阴的地方在于它**不产生测试失败**：门禁脚本本身全对、全绿，
    但它们根本没在 CI 上执行过；CI 恒红却指向一条看不懂的
    ``-: command not found``。R51 之前被 pytest 红灯挡在前面，从未暴露。
    """
    _write(
        root / ".github/workflows/ci.yml",
        "name: CI\non: [push]\njobs:\n  unit:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Static quality gates\n"
        "        run: |\n"
        "          python scripts/check_doc_drift.py\n"
        "          - name: Dashboard bundle in sync\n"
        "            run: python scripts/build_dashboard_bundle.py --check\n"
        "          - name: Doc counts\n"
        "            run: python scripts/check_doc_counts.py\n"
        "      - name: Dead-code audit (vulture)\n"
        "        run: |\n"
        "          vulture --min-confidence 60 cpt\n",
    )
    return "check_ci_workflow.py", "command not found"


def fx_line_refs(root: Path) -> tuple[str, str]:
    """⑪ 行号引用：造一处**行号对不上符号**的引用 —— R54 清出来的。

    这类错长得最像「没事」：文件在、行号也没越界、门禁打印 ✅，
    只是那一行早已换成别的东西。R54 实测现行文档里有 30+ 处这样的坐标，
    而 ``check_all_claims.py`` 的 L 类只验「行号没超出文件总行数」，一路放行。
    """
    _write(
        root / "cpt/domain/models.py",
        "\n".join(f"# 填充 {i}" for i in range(1, 30)) + "\n\nclass TrendType:\n    pass\n",
    )
    _write(
        root / "docs/pending-wiring.md",
        "# 待接线\n\n`cpt/domain/models.py:3` 里的 `TrendType` 还没接上。\n",
    )
    return "check_line_refs.py", "处没有 `TrendType`"


def fx_gate_coverage(root: Path) -> tuple[str, str]:
    """⑫ 门禁体检：造一道**在 ci.yml 里跑、却没有自检夹具**的门禁。

    这正是 R45「门禁恒返回 0」换了个入口复发的样子：新门禁接进了 CI，
    却忘了在 FIXTURES 里配一个「它必须抓到的错例」——
    于是「它能不能红」从未被证明，而 CI 依然全绿（因为它没红）。
    """
    _write(
        root / ".github/workflows/ci.yml",
        "name: CI\non: [push]\n"
        "jobs:\n  unit:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: Gates\n        run: |\n"
        "          python scripts/check_alpha.py\n"
        "          python scripts/check_doc_drift.py\n"
        "          python scripts/check_gate_coverage.py\n",
    )
    for name in ("check_alpha.py", "check_doc_drift.py"):
        _write(root / "scripts" / name, "raise SystemExit(0)\n")
    # 夹具里**故意**漏掉 check_doc_drift.py
    _write(
        root / "scripts/selftest_gates.py",
        "FIXTURES = {\n"
        '    "check_alpha.py": lambda root: ("check_alpha.py", "x"),\n'
        '    "check_gate_coverage.py": lambda root: ("check_gate_coverage.py", "x"),\n'
        "}\n",
    )
    return "check_gate_coverage.py", "逃过体检：`check_doc_drift.py`"


def fx_cold_environment(root: Path) -> tuple[str, str]:
    """⑬ 冷环境：造一个**胜负取决于本机环境**的用例。

    这就是 R49 那两个失败的样子（「本机恰好没有某物」被当成前提）：
    本机跑它是绿的，环境一抽掉它就是红的 —— 而它红的时候，
    看的人第一反应总是「环境问题，与本轮无关」。
    """
    _write(
        root / "tests/test_ambient_probe.py",
        "import os\n\n\n"
        "def test_path_is_untouched():\n"
        '    assert os.environ.get("PATH") != "/nonexistent"\n',
    )
    return "check_cold_environment.py", "依赖本机环境"


def fx_test_mutation(root: Path) -> tuple[str, str]:
    """⑭ 变异抽查：造一个**只会无脑报「抓到」**的机制。

    这条夹具必须**在真实仓库里跑**（见 :data:`RUN_IN_REPO`）—— 因为
    ``check_test_mutation.py`` 的正控制要真跑真测试、负控制要真的「无关改动
    不被抓红」。临时目录里既没有源码也没有测试，在那儿跑它会因为「文件缺失」
    而变红 —— 那是**假阳性**：它红的原因与「机制能不能分辨抓到/存活」无关。

    注入的错是**逻辑取反**：把「负控制必须存活」改成「负控制必须被抓到」。
    于是 --selftest 的两个控制必然有一个不满足 ⇒ 退出码非 0 ⇒ 夹具判定
    「抓到了」。这证明的是：**机制坏了会报红，而不是照样放行**。

    ⚠️ 这正是它自己存在的意义 —— 一个「总是说抓到」的变异检查，
    和一条恒真的断言一样没价值。
    """
    script = (SCRIPTS / "check_test_mutation.py").read_text(encoding="utf-8")
    anchor = "        good = pos and not neg"
    assert script.count(anchor) == 1, "锚点变了，请更新这个夹具"
    broken = script.replace(anchor, "        good = pos and neg")
    # 写到临时目录，但 :func:`run_one` 会让它以真实仓库为 CWD 执行
    (root / "scripts" / "check_test_mutation.py").write_text(broken, encoding="utf-8")
    return "check_test_mutation.py", "自检失败"


FIXTURES = {
    "check_doc_drift.py": fx_doc_drift,
    "check_all_claims.py": fx_all_claims,
    "check_doc_counts.py": fx_doc_counts,
    "check_enqueue_skeleton_unique.py": fx_enqueue_unique,
    "check_job_poll_unique.py": fx_poll_unique,
    "check_sql_layering.py": fx_sql_layering,
    "check_storage_failure_semantics.py": fx_storage_failure,
    "build_dashboard_bundle.py": fx_bundle_sync,
    "check_all_claims.py#H": fx_script_tags,
    "check_ci_workflow.py": fx_ci_workflow,
    "check_line_refs.py": fx_line_refs,
    "check_gate_coverage.py": fx_gate_coverage,
    "check_cold_environment.py": fx_cold_environment,
    "check_test_mutation.py": fx_test_mutation,
}

#: 这些门禁的夹具必须在**真实仓库**里跑，而不是临时目录。
#:
#: 默认协议是「复制脚本 + 造几个假文件到临时仓库」—— 对查唯一性/行数的门禁够用，
#: 因为它们只读那几���假文件。但 ``check_test_mutation.py`` 要**真跑 pytest**
#: （正控制）并确认**无关改动不被抓红**（负控制）；临时目录里既没源码也没测试，
#: 在那儿跑只会因为「文件缺失」而变红 —— 那是与被验性质无关的假阳性。
RUN_IN_REPO: set[str] = {"check_test_mutation.py"}


# ── 跑 ────────────────────────────────────────────────────────
def run_one(
    script: str, builder: Callable[[Path], tuple[str, str]], verbose: bool
) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        (root / "scripts").mkdir(parents=True)
        (root / "docs").mkdir(parents=True)
        # ⚠️ 形如 ``check_all_claims.py#H`` 要**先**拆名再复制 ——
        # 否则 shutil 会去找一个叫 ``…py#H`` 的文件（第一版就栽在这，报
        # 「自检本身崩了」而不是「夹具不对」）。
        cat = None
        if "#" in script:
            script, cat = script.split("#", 1)
        # 复制**真脚本**，ROOT 随之指向临时仓库
        shutil.copy(SCRIPTS / script, root / "scripts" / script)
        script, expect = builder(root)
        if verbose:
            print(f"    注入后 {script} 在 {root} 的内容：")
            for p in sorted(root.rglob("*")):
                if p.is_file() and p.name != script:
                    print(f"      {p.relative_to(root)}")
        argv = [sys.executable, str(root / "scripts" / script)]
        if script == "build_dashboard_bundle.py":
            argv.append("--check")
        if cat:
            argv.extend(["--cat", cat])
        # RUN_IN_REPO 的夹具：跑的是**临时目录里那份被改坏的副本**，
        # 但 CWD 与 CPT_REPO 必须指向**真实仓库** —— 它要读真源码、真跑 pytest。
        # ⚠️ 改的是临时目录的副本，真实仓库一个字节都不动。
        run_env = None
        cwd = str(root)
        if script in RUN_IN_REPO:
            cwd = str(ROOT)
            argv.append("--selftest")
            run_env = {**os.environ, "CPT_REPO": str(ROOT)}
        out = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=run_env,
            timeout=600,
        )
        blob = out.stdout + out.stderr
        if not expect:
            # 靠退出码判
            return out.returncode != 0, blob
        caught = expect in blob
        return caught, blob


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    print("═" * 72)
    print("门禁自检：造已知错例，验每个门禁**抓不抓得到**")
    print("═" * 72)
    ok = True
    for script, builder in FIXTURES.items():
        try:
            good, blob = run_one(script, builder, a.verbose)
        except Exception as exc:  # noqa: BLE001
            print(f"  💥 {script:34s} 自检本身崩了: {type(exc).__name__}: {exc}")
            ok = False
            continue
        mark = "✅ 抓到了" if good else "❌ **漏报了**"
        print(f"  {mark:16s} {script}")
        if not good:
            ok = False
            print("      ↓ 它对下面的错例没有任何反应 —— 「全绿」只是它什么都没查")
            for line in blob.strip().split("\n")[:8]:
                print(f"      │ {line[:88]}")
    print("─" * 72)
    print("全部门禁都能抓到各自的错例 ✅" if ok else "有门禁**抓不到自己该抓的错例** ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
