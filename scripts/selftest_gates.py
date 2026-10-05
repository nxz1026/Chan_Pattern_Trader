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
    """⑥ 入队骨架：故意加**第二份**入队逻辑。"""
    body = (
        "import ast, sys\n"
        "from pathlib import Path\n"
        "SRC = Path(__file__).resolve().parents[1] / 'cpt' / 'application' / 'llm_cases.py'\n"
        "tree = ast.parse(SRC.read_text(encoding='utf-8'))\n"
        "def cs(n):\n"
        "    return [x.lineno for x in ast.walk(tree) if isinstance(x, ast.Call)\n"
        "            and isinstance(x.func, ast.Name) and x.func.id == n]\n"
        "print(len(cs('_enqueue_and_submit')), len(cs('enqueue_call')), len(cs('_bootstrap')))\n"
        "sys.exit(0 if len(cs('_enqueue_and_submit')) == 2 else 1)\n"
    )
    _write(root / "scripts/check_enqueue_skeleton_unique.py", body)
    _pkg(
        root,
        "application/llm_cases.py",
        "def _enqueue_and_submit(c, r, subject_id=''):\n    pass\n"
        "def a(c, r):\n    _enqueue_and_submit(c, r); _enqueue_and_submit(c, r)\n"
        "def b(c, r):\n    enqueue_call; _bootstrap(); _enqueue_and_submit(c, r)\n",
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
    因「文件不存在」而报错 —— 那是另一条路径，测不到「改���没重建」这个真问题。
    """
    _write(
        root / "dashboard/dash-core.js",
        "// <<<HEAD\nconst A = 1;\n// >>>HEAD\n"
        "// <<<FUNCS\nfunction f() { return A; }\n// >>>FUNCS\n",
    )
    _write(
        root / "dashboard/dash-ops.js",
        "// <<<HEAD\nconst A = 1;\n// >>>HEAD\n"
        "// <<<FUNCS\nfunction g() { return A; }\n// >>>FUNCS\n})();\n",
    )
    # 先照它自己的 ORDER 生成一份，再改源文件
    import re as _re

    ords = _re.findall(
        r'^[A-Z_]+ = \(\n((?:    "[^"]+",\n)+)\)',
        (root / "scripts/build_dashboard_bundle.py").read_text(encoding="utf-8"),
    )
    names = _re.findall(r'"([^"]+)"', ords[0]) if ords else ["dash-core.js", "dash-ops.js"]
    (root / "scripts/build_dashboard_bundle.py").write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, re\nfrom pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\nDASH = ROOT / 'dashboard'\n"
        f"ORDER = ({', '.join(repr(n) for n in names)},)\n"
        "BANNER = '// generated\\n'\n"
        "def build():\n"
        "    h = f = ''\n    t = ''\n"
        "    for i, n in enumerate(ORDER):\n"
        "        x = (DASH / n).read_text(encoding='utf-8')\n"
        "        mh = re.search(r'// <<<HEAD\\n(.*?)\\n// >>>HEAD\\n', x, re.S)\n"
        "        mf = re.search(r'// <<<FUNCS\\n(.*?)\\n// >>>FUNCS\\n', x, re.S)\n"
        "        mt = re.search(r'// <<<TAIL\\n(.*?)\\n// >>>TAIL', x, re.S)\n"
        "        if not mh or not mf:\n"
        "            raise SystemExit('缺哨兵')\n"
        "        if i == 0:\n            h = mh.group(1)\n"
        "        f += ('\\n' if f else '') + mf.group(1)\n"
        "        if mt:\n            t = mt.group(1)\n"
        "    return BANNER + chr(10) + h + chr(10) + chr(10)"
        " + f + chr(10) + chr(10) + t + chr(10)\n"
        "def main():\n"
        "    ap = argparse.ArgumentParser()\n"
        "    ap.add_argument('--check', action='store_true')\n"
        "    a = ap.parse_args()\n"
        "    out = build()\n"
        "    b = DASH / 'dashboard.bundle.js'\n"
        "    if a.check:\n"
        "        cur = b.read_text(encoding='utf-8') if b.exists() else ''\n"
        "        print('  OK' if cur == out else '  STALE')\n"
        "        return 0 if cur == out else 1\n"
        "    b.write_text(out, encoding='utf-8')\n    print('  BUILT')\n    return 0\n"
        "if __name__ == '__main__':\n    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    import subprocess as _sp

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
        root / "dashboard/dash-ops.js",
        "// <<<HEAD\nconst A = 1;\n// >>>HEAD\n"
        "// <<<FUNCS\nfunction g() { return A + 1; }\n// >>>FUNCS\n})();\n",
    )
    return "build_dashboard_bundle.py", "STALE"


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
}


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
        out = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=120,
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
