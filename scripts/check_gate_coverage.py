#!/usr/bin/env python3
"""门禁⑫：**不许有门禁逃过自检**。

## 为什么要这道门（R45 第一类静默失效的残量）

R45 的教训是「门禁恒 `return 0` —— CI 上永远绿，等于没有门禁」。
当时的对策是 `scripts/selftest_gates.py`：给每个门禁配一个**已知错例**，
先证明它「能红」，再谈它「是不是绿的」。

但那份自检清单是**手工维护**的，而**没有任何东西**保证它与
`.github/workflows/ci.yml` 保持同步。于是同一个病换个入口复发：

    # 某人给 ci.yml 加了一道新门禁
    - name: 新的检查
      run: python scripts/check_something_new.py

    # 忘了在 selftest_gates.py 的 FIXTURES 里加夹具
    # ⇒ 这道门禁是否真的能红，**从来没有被证明过**
    # ⇒ 而 CI 依然全绿 —— 因为它没红

症状与 R45 一模一样，只是从「门禁自己坏」变成「门禁逃过体检」。

## 判据：三方对齐

    A = ci.yml 里跑的门禁脚本
    B = selftest_gates.py 里 FIXTURES 的键
    C = scripts/ 里实际存在的 check_*.py

  R1  A ⊆ B —— ci.yml 跑的门禁**必须**有自检夹具。
      少了就是「逃过体检」：它能不能红从未被证明。
  R2  B ⊇ 磁盘 —— 每个夹具**必须**指向一个真实存在的脚本。
      死夹具会让自检报「自检本身崩了」，掩盖真正的结论。
  R3  C ⊆ A —— 存在的 `check_*.py` **必须**在 CI 里跑。
      写了却从不执行的门禁，与没写没有区别。

## 豁免纪律

`SELF` 是自检自己（不可能自检自身）；`NOT_A_GATE` 是名字像门禁、
实为历史工具的脚本 —— **每条都必须写明理由**，理由为空直接判失败，
否则豁免表会退化成垃圾桶（`check_all_claims.py` 的 `_ALLOW`、
`check_line_refs.py` 的 `HISTORICAL` 都有同样的纪律）。

## 用法

    python scripts/check_gate_coverage.py
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
WF_DIR = ROOT / ".github" / "workflows"
SELFTEST = SCRIPTS / "selftest_gates.py"

#: 自检脚本自己 —— 它不可能给自己当夹具
SELF = "selftest_gates.py"

#: 名字像门禁、但不该进 CI 的脚本 → 理由（**每条必须写理由**）
NOT_A_GATE: dict[str, str] = {
    "scan_doc_claims.py": "R45 的**一次性**普查工具，产出已并入 check_all_claims.py",
    "verify_public_contracts.py": "人工按需跑的对外契约抽查，依赖真实上游，不进 CI",
    "check_shared_tables.py": (
        "**需要真实数据库**的运行期检查，不是静态门禁：它查 emotion-core 写的 7 张"
        "共享 A 股表在生产上停更了没有（2026-10-07 实测 Oracle 上确实没有任何东西"
        "会写它们，详见 docs/shared-tables-contract.md）。CI 没有库，硬接进去只会"
        "变成永远红或永远绿 —— 后者正是本文件要防的那种失效。它的执行点是"
        " deploy/cron/cron-daily-report.sh（离线 cron，每日 07:00 UTC），"
        "有问题发飞书；静态部分（契约 ⇄ 代码双向对齐）由"
        " tests/test_shared_tables_contract.py 在 CI 里守。"
    ),
}

_INVOKE = re.compile(r"\bscripts/([A-Za-z0-9_]+\.py)\b")
_KEY = re.compile(r'^\s*"(?P<name>[A-Za-z0-9_]+\.py)(?:#[A-Za-z]+)?"\s*:', re.M)


def _ci_gates() -> set[str]:
    """A：`.github/workflows/*.yml` 里被 `python scripts/x.py` 调起的门禁。"""
    found: set[str] = set()
    files = sorted(WF_DIR.glob("*.yml")) + sorted(WF_DIR.glob("*.yaml"))
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            found.update(_INVOKE.findall(line))
    return found - {SELF}


def _fixture_keys() -> set[str]:
    """B：`selftest_gates.py` 里 `FIXTURES` 字典的键（去掉 `#分类` 后缀）。

    用 AST 而不是正则 —— 只有真的把 `FIXTURES` 这个赋值解析出来，
    才能证明这些键**确实是夹具**，而不是别处恰好出现的字符串。
    """
    tree = ast.parse(SELFTEST.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "FIXTURES" not in names:
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        keys: set[str] = set()
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value.split("#", 1)[0])
        return keys
    return set()


def _disk_gates() -> set[str]:
    """C：`scripts/check_*.py` 里实际存在的文件。"""
    return {p.name for p in SCRIPTS.glob("check_*.py")}


def main() -> int:
    if not all(reason.strip() for reason in NOT_A_GATE.values()):
        print("❌ 豁免表里有空理由 —— 豁免必须逐条说明原因")
        return 1

    a, b, c = _ci_gates(), _fixture_keys(), _disk_gates()
    problems: list[str] = []

    for name in sorted(a - b):
        problems.append(
            f"R1 逃过体检：`{name}` 在 ci.yml 里跑，但 selftest_gates.py 的 FIXTURES "
            f"里没有它的夹具 ⇒ 它**能不能红从未被证明**"
        )
    for name in sorted(b):
        if not (SCRIPTS / name).exists():
            problems.append(
                f"R2 死夹具：FIXTURES 里的 `{name}` 在 scripts/ 下不存在 ⇒ "
                f"自检会报「自检本身崩了」，掩盖真正的结论"
            )
    for name in sorted(c - a - set(NOT_A_GATE)):
        problems.append(
            f"R3 闲置门禁：`scripts/{name}` 存在，但没有任何 workflow 跑它 ⇒ "
            f"写了却从不执行，等于没写"
        )

    if problems:
        print("❌ 门禁体检清单与 CI 对不上：")
        for p in problems:
            print(f"  {p}")
        print(
            "\n  ⇒ 这道的病根和 R45 的「门禁恒返回 0」是同一个：**全绿只是它什么都没查**。\n"
            "    修法：\n"
            "      · 新门禁要同时进 ci.yml **和** selftest_gates.py 的 FIXTURES"
            "（造一个它必须抓到的错例）；\n"
            "      · 夹具指向的脚本删了，就把夹具一并删掉；\n"
            "      · 确属一次性工具，把它加进本脚本的 NOT_A_GATE 并**写明理由**。"
        )
        return 1

    print(
        f"  ✅ 没有门禁能逃过体检（ci.yml 跑 {len(a)} 道，夹具 {len(b)} 条，"
        f"磁盘上 check_*.py 共 {len(c)} 个；豁免 {len(NOT_A_GATE)} 个非门禁工具）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
