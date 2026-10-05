#!/usr/bin/env python3
"""抽取文档里的**未兑现承诺**候选（R45 第二轮）。

## 定位

`check_doc_drift.py` 抓的是**机械**漂移（数字、路径、参数名）——
那些能被正则对上。这一个抓的是**语义**漂移：文档说了某件事，
代码里找不到对应物。

R45 第一轮实测到的 6 处漂移里，最难发现的两处属于这类：

  - ``dashboard-plan.md`` 写了 ``/candles`` ``/events`` ``/stats``
    三个接口，读起来像「已规划待接」，**没有一处写着「其实没有」**
  - ``min_bi_len`` 的 docstring 写「底层笔最少跨度」，
    像一条全局口径，实际只是 czsc 的适配参数

⇒ 这类只能先把候选捞出来**逐条人工核对**，没法全自动判对错。
本脚本的职责是「别让人漏看」，不是「替人判断」。

用法::

    python scripts/scan_doc_claims.py            # 全扫
    python scripts/scan_doc_claims.py --grep 计划
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 承诺类措辞。命中不等于漂移 —— 绝大多数是「计划」，需要人工判断。
CLAIM_PATTERNS = [
    r"(?<!不)计划",  # 排除「不是计划」
    r"将要|将会|打算",
    r"待实现|未实现|尚未实现|还没实现",
    r"TODO|FIXME|XXX",
    r"后续(?:会|将|需要?)",
    r"应该(?:会|能|支持|提供)",
    r"预留|占位",
    r"仅(?:支持|覆盖|处理)",
    r"暂未|暂不支持",
]

#: 明显是「已兑现的现状陈述」，跳过以免噪声淹没真信号
SETTLED = re.compile(
    r"已(?:实现|完成|支持|删除|合并|接入|修正|废弃|落地|回填|清理|验证|确认)"
    r"|从(?:未|不)|不再|曾经|历史|已废弃|已归档"
)


def iter_docs() -> list[Path]:
    out = sorted((ROOT / "docs").glob("*.md"))
    extra = [ROOT / "README.md", ROOT / "deploy" / "README.md"]
    return [p for p in out + extra if p.is_file()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--grep", help="只显示含该子串的行")
    ap.add_argument("--doc", help="只扫指定文档（文件名片段）")
    a = ap.parse_args(argv)

    pat = re.compile("|".join(CLAIM_PATTERNS))
    total = 0
    for p in iter_docs():
        if a.doc and a.doc not in p.name:
            continue
        lines = p.read_text(encoding="utf-8").splitlines()
        hits = []
        for i, line in enumerate(lines, 1):
            if not pat.search(line) or SETTLED.search(line):
                continue
            if a.grep and a.grep not in line:
                continue
            # 跳过表格分隔线 / 纯标题
            if set(line.strip()) <= set("-|: "):
                continue
            hits.append((i, line.strip()))
        if not hits:
            continue
        print(f"\n─── {p.relative_to(ROOT)}  ({len(hits)} 条候选)")
        for i, line in hits:
            print(f"  {i:5d}| {line[:150]}")
            total += 1
    print(f"\n{'=' * 80}\n共 {total} 条候选 —— **需要逐条判断**，命中 ≠ 漂移")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
