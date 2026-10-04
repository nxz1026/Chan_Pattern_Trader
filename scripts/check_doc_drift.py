#!/usr/bin/env python3
"""文档 ↔ 代码 漂移扫描（R45 第二轮复盘）。

## 为什么要有这个

R45 第一轮复盘（找 bug）里反复撞见**同一类**问题，且都不是代码 bug：

  1. ``architecture.md`` §2.1 给 4 层打了 ✅，其中 2 层**从未复盘**过
  2. ``dashboard-plan.md`` 写了 3 个**从未实现**的接口
  3. ``final-acceptance.md`` 说「7 组路由」，实际 25 个
  4. ``min_bi_len`` 的 docstring 写得像全局口径，实际只对 czsc 生效
  5. ``rules.md`` §9 漏记了 7 个自称「冻结」的参数
  6. 「没做过」「少做了」全被一个 ✅ 盖住

⇒ 这些**测试抓不到、代码审不出来**，只能靠「拿文档里的断言去对代码」。

## 扫四类漂移

  A. 计数漂移：文档里的「N 个文件 / M 行」 vs 实际
  B. 状态漂移：文档里的 ✅/⚠️ 复盘状态 vs 实际有没有复盘记录
  C. 参数漂移：文档里声明的默认值/取值 vs 代码里的
  D. 接口漂移：文档里的 HTTP 路径 vs 代码里真实注册的

用法::

    python scripts/check_doc_drift.py            # 全扫
    python scripts/check_doc_drift.py --layer domain
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYERS = ["domain", "adapters", "application", "storage", "llm", "web"]


def files_and_lines(layer: str) -> tuple[int, int]:
    base = ROOT / "cpt" / layer
    fs = sorted(base.rglob("*.py")) if base.is_dir() else []
    lines = sum(len(f.read_text(encoding="utf-8").splitlines()) for f in fs)
    return len(fs), lines


def section(title: str) -> None:
    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


# ---------------------------------------------------------------- A 计数漂移
def check_counts() -> list[str]:
    section("A. 计数漂移：architecture.md §2.1 的「文件 / 行数」")
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(r"\| `(\w+)/` \| (\d+) \| ([\d,]+) \|", arch):
        layer, n_doc, l_doc = m.group(1), int(m.group(2)), int(m.group(3).replace(",", ""))
        if layer not in LAYERS:
            continue
        n_real, l_real = files_and_lines(layer)
        flag = "✅" if (n_doc == n_real and l_doc == l_real) else "❌"
        if flag == "❌":
            bad.append(f"  {flag} {layer:12s} 文档 {n_doc:3d} 文件/{l_doc:5d} 行"
                       f"  vs 实际 {n_real:3d}/{l_real:5d}"
                       f"  (差 {n_real-n_doc:+d} 文件 / {l_real-l_doc:+d} 行)")
        else:
            print(f"  {flag} {layer:12s} {n_doc:3d} 文件/{l_doc:5d} 行")
    return bad


# ---------------------------------------------------------------- B 状态漂移
def check_status() -> list[str]:
    section("B. 状态漂移：✅ 标记 vs 有没有复盘记录")
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    # ⚠️ 复盘记录可能落在 progress-log，也可能落在 docs/review-<层>-*.md。
    # 只认 progress-log 会把「写了独立复盘文档的层」误判成没复盘（踩过：llm）。
    corpus = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((ROOT / "docs").glob("*.md"))
    )
    bad = []
    for m in re.finditer(r"\| `(\w+)/` \| \d+ \| [\d,]+ \| (.+?) \|", arch):
        layer, status = m.group(1), m.group(2)
        if layer not in LAYERS:
            continue
        has_review = bool(
            re.search(rf"{layer}/?\s*层[^\n]{{0,30}}复盘", corpus)
            or re.search(rf"复盘[^\n]{{0,20}}{layer}/", corpus)
        )
        claimed_done = "✅" in status
        if claimed_done and not has_review:
            bad.append(f"  ❌ {layer:12s} 标 ✅ 但全仓 docs 里找不到复盘记录")
        elif has_review and "⚠️" in status:
            bad.append(f"  ⚠️ {layer:12s} 标 ⚠️ 但 docs 里已有复盘记录")
        else:
            print(f"  ✅ {layer:12s} {'✅' if claimed_done else '⚠️'} 与复盘记录一致")
    return bad


# ---------------------------------------------------------------- C 参数漂移
def check_params() -> list[str]:
    section("C. 参数漂移：rules.md §9 声明的冻结参数 vs RulesConfig 实际")
    import dataclasses
    import sys

    sys.path.insert(0, str(ROOT))
    from cpt.domain.config import RulesConfig

    rules = (ROOT / "docs" / "rules.md").read_text(encoding="utf-8")
    bad = []
    names = [f.name for f in dataclasses.fields(RulesConfig) if f.name != "config_version"]
    for nm in names:
        if nm not in rules:
            bad.append(f"  ❌ {nm:28s} 代码里有、rules.md §9 没记")
    if not bad:
        print(f"  ✅ {len(names)} 个冻结参数全部在 rules.md 有记载")
    return bad


# ---------------------------------------------------------------- D 接口漂移
#: 这些路径在文档里出现是**为了说明它们不存在**（幽灵接口的记录），
#: 不算「文档声称存在」。把它们排除，否则扫描器会指着自己的说明报错。
_GHOST_PATHS = {
    "/api/dashboard/candles",
    "/api/dashboard/events",
    "/api/dashboard/stats",
}


def check_endpoints() -> list[str]:
    section("D. 接口漂移：文档里的 /api 路径 vs 代码里真实注册的")
    code: set[str] = set()
    for p in sorted((ROOT / "cpt" / "web").rglob("*.py")):
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r'["\'](/api/(?:dashboard|canvas)/[a-z0-9/_-]*)["\']', src):
            code.add(m.group(1))
    code.discard("/api/dashboard/a-share/")   # 前缀常量，非独立路由

    docs: dict[str, list[str]] = {}
    for p in sorted((ROOT / "docs").glob("*.md")) + [ROOT / "deploy" / "README.md"]:
        for m in re.finditer(r"(/api/(?:dashboard|canvas)/[a-z0-9/_-]+)", p.read_text(encoding="utf-8")):
            docs.setdefault(m.group(1).rstrip(".,)"), []).append(p.name)

    bad = []
    for path in sorted((set(docs) - code) - _GHOST_PATHS):
        if path.endswith("/"):
            continue
        bad.append(f"  ❌ {path:44s} 文档有、代码无  ({','.join(sorted(set(docs[path])))})")
    for path in sorted(code - set(docs)):
        bad.append(f"  ⚠️  {path:44s} 代码有、文档无")
    if not bad:
        print(f"  ✅ 文档与代码完全对齐（{len(code)} 个路由）"
              f"，{len(_GHOST_PATHS)} 个幽灵接口已豁免")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    print("R45 第二轮：文档 ↔ 代码 漂移扫描")
    allbad = []
    for fn in (check_counts, check_status, check_params, check_endpoints):
        allbad += fn()
    print()
    print("=" * 92)
    if allbad:
        print(f"发现 {len(allbad)} 处漂移：")
        for b in allbad:
            print(b)
        return 1
    print("✅ 未发现漂移")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
