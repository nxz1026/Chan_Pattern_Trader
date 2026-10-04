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
    _pkg(root, "domain/config.py",
         "from dataclasses import dataclass\n"
         "@dataclass(frozen=True)\n"
         "class RulesConfig:\n    min_bi_len: int = 4\n")
    _write(root / "docs/architecture.md",
           "### 2.1\n`cpt/domain/x.py` **已整层删除**（真的删了）\n")
    _pkg(root, "domain/x.py", "# 还在\n")
    return "check_doc_drift.py", "x.py"


def fx_all_claims(root: Path) -> tuple[str, str]:
    """④ 全量断言：文档引用一个不存在的文件。"""
    _write(root / "README.md", "见 `cpt/domain/does_not_exist.py`。\n")
    return "check_all_claims.py", "does_not_exist.py"


def fx_doc_counts(root: Path) -> tuple[str, str]:
    """⑤ 计数：architecture §2.1 的行数与实际不符。"""
    _pkg(root, "domain/a.py", "x = 1\ny = 2\n")
    _write(root / "docs" / "architecture.md",
           "## 2.1\n| 层 | 文件 | 行数 | 复盘状态 |\n|---|---:|---:|---|\n"
           "| `domain/` | 1 | 999 | x |\n")
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
    _pkg(root, "application/llm_cases.py",
         "def _enqueue_and_submit(c, r, subject_id=''):\n    pass\n"
         "def a(c, r):\n    _enqueue_and_submit(c, r); _enqueue_and_submit(c, r)\n"
         "def b(c, r):\n    enqueue_call; _bootstrap(); _enqueue_and_submit(c, r)\n")
    return "check_enqueue_skeleton_unique.py", ""


def fx_poll_unique(root: Path) -> tuple[str, str]:
    """⑦ 前端轮询：自己写一个 poll 循环。"""
    _write(root / "dashboard/x.js",
           "async function pollThing() {\n"
           "  await fetch('/x');\n"
           "  window.setTimeout(pollThing, 1000);\n"
           "}\n")
    _write(root / "README.md", "x\n")
    return "check_job_poll_unique.py", "pollThing"


FIXTURES = {
    "check_doc_drift.py": fx_doc_drift,
    "check_all_claims.py": fx_all_claims,
    "check_doc_counts.py": fx_doc_counts,
    "check_enqueue_skeleton_unique.py": fx_enqueue_unique,
    "check_job_poll_unique.py": fx_poll_unique,
}


# ── 跑 ────────────────────────────────────────────────────────
def run_one(script: str, builder, verbose: bool) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        (root / "scripts").mkdir(parents=True)
        (root / "docs").mkdir(parents=True)
        # 复制**真脚本**，ROOT 随之指向临时仓库
        shutil.copy(SCRIPTS / script, root / "scripts" / script)
        script, expect = builder(root)
        if verbose:
            print(f"    注入后 {script} 在 {root} 的内容：")
            for p in sorted(root.rglob("*")):
                if p.is_file() and p.name != script:
                    print(f"      {p.relative_to(root)}")
        out = subprocess.run(
            [sys.executable, str(root / "scripts" / script)],
            capture_output=True, text=True, cwd=str(root), timeout=120,
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
    print("全部门禁都能抓到各自的错例 ✅" if ok
          else "有门禁**抓不到自己该抓的错例** ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
