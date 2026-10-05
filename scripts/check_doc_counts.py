#!/usr/bin/env python3
"""文档里的**计数断言**逐条对账（R45 第五轮）。

## 为什么单独一个脚本

P/E/T/K/L 是「有没有」，计数是「**多少**」——
后者是最容易悄悄腐坏的一类，而且**完全不会让任何测试变红**。

R45 一天里栽过三次：
- architecture §2.1 的行数偏低（6 层全偏）
- known-traps 说「16 个模块」（R22 后只剩 1 个）
- handoff 说「5 份里有 4 份没状态标记」

共同点：**数字看起来都很合理**，没有一处会让人怀疑。

## 怎么判

只认**可机械复算**的计数，判据分两档：

  强  「N 个文件」「M 行」「K 只票」紧跟在某个**可枚举的目录/集合**后面
  弱  「共 N 条」「累计 M 次」—— 需要历史状态，**不判**（会大量假阳性）

强判据会**真的去数**，数不上就报。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 「N 个文件」/「M 行」
N_FILES = re.compile(r"`?(\d+)`?\s*个(?:代码)?文件")
N_LINES = re.compile(r"`?(\d+)`?\s*行")

#: 文档里显式写出的「目录 → 计数」对应表（人工核过、值得钉住的那种）
CHECKS: list[tuple[str, str | None, str]] = [
    # (文档片段, 实数, 描述)
    ("cpt/domain", None, "domain 层 .py 文件数"),
    ("cpt/adapters", None, "adapters 层 .py 文件数"),
    ("cpt/application", None, "application 层 .py 文件数"),
    ("cpt/storage", None, "storage 层 .py 文件数"),
    ("cpt/llm", None, "llm 层 .py 文件数"),
    ("cpt/web", None, "web 层 .py 文件数"),
]


def count_files(rel: str) -> int:
    base = ROOT / rel
    return len([p for p in base.rglob("*.py") if p.is_file()]) if base.is_dir() else 0


def count_lines(rel: str) -> int:
    base = ROOT / rel
    return sum(
        len(p.read_text(encoding="utf-8").splitlines()) for p in base.rglob("*.py") if p.is_file()
    )


def main() -> int:
    print("═══ 实际计数（用作对照）═══")
    truth: dict[str, tuple[int, int]] = {}
    for rel, _, desc in CHECKS:
        n, lines = count_files(rel), count_lines(rel)
        truth[rel] = (n, lines)
        print(f"  {rel:18s} {n:3d} 文件 / {lines:6d} 行   ({desc})")

    print("\n═══ architecture.md §2.1 表里写的 ═══")
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    bad = 0
    for m in re.finditer(r"\| `(\w+)/` \| (\d+) \| ([\d,]+) \|", arch):
        layer, n_doc, l_doc = m.group(1), int(m.group(2)), int(m.group(3).replace(",", ""))
        key = f"cpt/{layer}"
        if key not in truth:
            continue
        n_real, l_real = truth[key]
        ok = n_doc == n_real and l_doc == l_real
        if not ok:
            bad += 1
        print(
            f"  {'✅' if ok else '❌'} {layer:12s} 文档 {n_doc:3d}/{l_doc:6d}  "
            f"实际 {n_real:3d}/{l_real:6d}"
        )

    print("\n═══ 全仓散落的「N 个文件 / M 行」断言 ═══")
    hits = 0
    for d in sorted(ROOT.rglob("*.md")):
        if any(x in d.parts for x in (".git", "references", "node_modules", "archive")):
            continue
        for i, line in enumerate(d.read_text(encoding="utf-8").splitlines(), 1):
            for m in N_FILES.finditer(line):
                hits += 1
                print(f"  · {d.relative_to(ROOT)}:{i}  「{m.group(0)}」")
    print(f"  共 {hits} 条（需人工核对——脚本不自动判，只负责**不漏看**）")

    print("\n" + "=" * 80)
    print(f"{'✅ §2.1 与实际一致' if not bad else f'❌ §2.1 有 {bad} 处对不上'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
