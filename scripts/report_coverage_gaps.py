#!/usr/bin/env python3
"""从 coverage 数据里挑出**真正值得补测试**的目标（R45 P0-1）。

## 为什么不直接看总覆盖率

总覆盖率是个**平均值**，会把两类东西抹平：

- 大量**只有定义、从未被调用**的路径（``except`` 分支、降级兜底、错误处理）；
- 与生产无关的边角。

真正贵的是**「生产上会跑、但从没跑过」**的代码 ——
今天已经吃过一次：``run_metric_store`` 长期零覆盖，
而它的 ``prune`` **在生产 cron 里每天 04:10 删行**。

## 三档输出

  P0  被生产入口引用 **且** 覆盖率为 0   ← 真风险，先补这些
  P1  覆盖率 < 30% 且有分支没走到        ← 次之
  P2  仅统计                            ← 只报数，不建议现在动

「生产入口」按 :data:`PROD_ROOTS` 判定 —— 看板 / CLI / cron 真正会走的那几行。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 生产入口清单：这些文件/目录里引用到的函数，零覆盖就是真风险。
PROD_ROOTS = (
    "cpt/web",
    "cpt/application",
    "cpt/storage",
    "cpt/adapters",
    "scripts",
)


def main(argv: list[str] | None = None) -> int:
    cov = json.loads(Path("/tmp/coverage.json").read_text(encoding="utf-8")) \
        if Path("/tmp/coverage.json").exists() else None
    if cov is None:
        # 没有 JSON 就直接问 coverage
        out = subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", "/tmp/coverage.json"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if out.returncode != 0:
            print(out.stderr[-800:] or "coverage json 失败")
            return 1
        cov = json.loads(Path("/tmp/coverage.json").read_text(encoding="utf-8"))

    files = cov["files"]
    summary = cov["totals"]
    print("=" * 78)
    print(f"总行覆盖 {summary['covered_lines']}/{summary['num_statements']} "
          f"= {summary['percent_covered']:.1f}%   "
          f"分支覆盖 {summary.get('percent_covered_display', '')}")
    print("=" * 78)

    # 生产代码引用了哪些函数名
    used: set[str] = set()
    for pat in PROD_ROOTS:
        for p in ROOT.glob(f"{pat}/**/*.py"):
            used |= set(re.findall(r"\b([a-z_][a-z0-9_]{3,})\s*\(", p.read_text(encoding="utf-8")))

    p0: list[tuple[float, str, str, int, int]] = []
    p1: list[tuple[float, str, str, int, int]] = []
    for path, info in sorted(files.items()):
        rel = path if path.startswith("cpt/") else path
        if not rel.startswith("cpt/"):
            continue
        pct = info["summary"]["percent_covered"]
        missing = [ln for ln in info["missing_lines"]]
        if not missing:
            continue
        # 逐个函数看：它有没有被生产代码引用
        text = Path(path).read_text(encoding="utf-8") if Path(path).exists() else ""
        for fn in re.findall(r"^\s*def ([a-z_][a-z0-9_]{3,})\s*\(", text, re.M):
            if fn.startswith("__"):
                continue
            body_lines = _body_lines(text, fn)
            if not body_lines:
                continue
            miss = [ln for ln in body_lines if ln in missing]
            if not miss:
                continue
            ratio = 1 - len(miss) / len(body_lines)
            rec = (ratio, rel, fn, len(miss), len(body_lines))
            if fn in used:
                p0.append(rec)
            elif ratio < 0.7:
                p1.append(rec)

    p0.sort()
    print(f"\n🔴 P0 —— **生产会调用**但覆盖不足（{len(p0)} 个函数）")
    for ratio, rel, fn, m, t in p0[:22]:
        print(f"  {ratio*100:5.1f}%  {rel:42s} {fn}  ({m}/{t} 行未覆盖)")

    p1.sort()
    print(f"\n🟡 P1 —— 覆盖 <70% 且非生产直接引用（{len(p1)} 个函数）")
    for ratio, rel, fn, m, t in p1[:12]:
        print(f"  {ratio*100:5.1f}%  {rel:42s} {fn}  ({m}/{t} 行未覆盖)")

    print("\n" + "─" * 78)
    print("P0 是先补的：那些代码**线上真的会跑**，只是从没被测过。")
    print("总覆盖率不是目标 —— 目标是「生产路径上有没有没跑过的分支」。")
    return 0


def _body_lines(text: str, fn: str) -> list[int]:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines, 1):
        if re.match(rf"^\s*def {re.escape(fn)}\s*\(", line):
            start = i
            break
    if start is None:
        return []
    indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
    out = [start]
    for i in range(start + 1, len(lines) + 1):
        line = lines[i - 1]
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        out.append(i)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
