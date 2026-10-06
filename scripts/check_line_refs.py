#!/usr/bin/env python3
"""门禁⑪：文档里的 `file.py:123` **行号引用必须仍指到那件事**。

## 为什么要这道门（R54 实测踩出来的）

`scripts/check_all_claims.py` 的 L 类也在查行号引用，但它的判据只有一句：

    if ln > n:                      # n = 文件总行数
        bad["L 行号越界"].append(...)

即**只验「行号没有超出文件总行数」**，从不读那一行还是不是文档说的那件事。
于是它一边打印 `✅ 全部断言对得上`，一边有 30+ 处行号正指着完全无关的代码。
该文件自己的 docstring 却写着 L 类要验「那行还在不在」—— **门禁验错了属性**。
这是本仓第四类静默门禁失效（前三类：R45 门禁恒返回 0、R49 断言依赖本机环境、
R52 YAML 块标量吞掉步骤）。

R54 实测的两个样本：

    docs/pending-wiring.md          `cpt/web/app.py:513`  ← 该行实为一句注释
    docs/duplication-triage.md      `a_share_local.py:225` ← 该行实为 T+1 docstring
                                    （真身在 :579，文件自身漂移逾 350 行）

## 判据（保守，宁可漏报不可误报）

对文档行里的每个 `file.py:NNN`：

1. **严格解析目标文件**：先按完整路径；否则按 basename 全仓唯一匹配。
   **同名多份 ⇒ 直接跳过**，绝不猜 —— 猜错会制造假阳性，假阳性会让门禁被无视。
2. 行号越界 ⇒ 判失败。
3. 取**归属本次引用的片段**（上一个引用结束 → 下一个引用开始，见 `_segment`），
   在其中找「确实被该文件 `def`/`class` 定义」的标识符当锚点。
   **片段唯一且恰好命中一个锚点**才继续；0 个（如引用的是调用点、或那段文字里
   没有符号名）或多个（歧义）都跳过。这天然绕开了
   `` `a.py:225` / `b.py:389` `` 这类列表引用 —— 每个文件只会认领自己的符号。
4. 锚点在 `NNN ± 3` 行内出现（**包含调用点**，不要求是定义行）⇒ 通过。

## 历史文档豁免

`docs/duplication-triage.md`、`docs/progress-log.md`、`docs/handoff-*.md`、
`docs/review-*-r45.md` 里的行号是**当时现场的归档证据**，不是导航坐标。
按本仓「保留原文 + 就地标注」的规矩，**改写它们等于伪造当时的记录**，
故整体豁免 —— 但每条豁免都必须在下表写明理由，否则豁免表会变成垃圾桶
（`check_all_claims.py` 的 `_ALLOW` 已有同样的纪律）。

## 用法

    python scripts/check_line_refs.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 锚点允许的偏移（行）。文档惯例是指认定义行或紧邻的调用点。
WINDOW = 3

#: 历史文档 → 豁免理由（**每条必须写理由**，空理由会被下面的自检挡下）
HISTORICAL: dict[str, str] = {
    "docs/duplication-triage.md": "2026-09-25 去重判定归档 —— 行号是当时快照的现场证据",
    "docs/progress-log.md": "逐轮开发日志 —— 记录每轮当时看到的行号",
    "docs/archive/handoffs-r44-r45.md": "5 份交接单合并件 — 行号是当时快照的现场证据",
    "docs/archive/reviews-r45.md": "9 份 R45 分层复盘合并件 — 当时现场",
}

_REF = re.compile(r"([A-Za-z0-9_/.-]+\.py):(\d+)")
_IDENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]{2,})")
_SKIP_DIRS = {".venv", ".git", "node_modules", "__pycache__", "references"}


def _python_index() -> dict[str, list[Path]]:
    """basename → 全部匹配路径（用于唯一匹配，多份则不猜）。"""
    index: dict[str, list[Path]] = {}
    for p in ROOT.rglob("*.py"):
        if _SKIP_DIRS & set(p.parts):
            continue
        index.setdefault(p.name, []).append(p)
    return index


def _resolve(fp: str, index: dict[str, list[Path]]) -> Path | None:
    """严格解析：完整路径优先；否则仅当 basename 全仓唯一时才认。"""
    direct = ROOT / fp
    if direct.exists():
        return direct
    cands = index.get(Path(fp).name, [])
    return cands[0] if len(cands) == 1 else None


def _segment(line: str, hits: list[re.Match[str]], k: int) -> str:
    """返回**归属第 k 个引用**的文字片段（上一个引用结束 → 下一个引用开始）。

    没有这一步，同一行并列两个引用时会互相污染：例如
    ``（`cpt/web/app.py:862`）；序列型值由 `cpt/web/app.py:287 _summarize_diff_value` 降级``
    若拿整行取锚点，`:862` 会被误判成「该处应有 `_summarize_diff_value`」。
    """
    start = hits[k - 1].end() if k else 0
    end = hits[k + 1].start() if k + 1 < len(hits) else len(line)
    return line[start:end]


def _uncovered(doc: Path, index: dict[str, list[Path]]) -> list[str]:
    problems: list[str] = []
    for lineno, line in enumerate(
        doc.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        hits = list(_REF.finditer(line))
        if not hits:
            continue
        for k, m in enumerate(hits):
            fp, n = m.group(1), int(m.group(2))
            target = _resolve(fp, index)
            if target is None:
                continue
            src = target.read_text(encoding="utf-8", errors="replace").splitlines()
            if n > len(src):
                problems.append(
                    f"{doc.relative_to(ROOT)}:{lineno}: `{fp}:{n}` 越界"
                    f"（{target.relative_to(ROOT)} 只有 {len(src)} 行）"
                )
                continue
            seg = _segment(line, hits, k)
            anchors = [
                x
                for x in _IDENT.findall(seg)
                if re.search(rf"\b(def|class)\s+{re.escape(x)}\b", "\n".join(src))
            ]
            if len(anchors) != 1:
                continue  # 无可信锚点（引用调用点 / 无符号名 / 歧义）⇒ 不判
            sym = anchors[0]
            near = "\n".join(src[max(0, n - 1 - WINDOW) : n + WINDOW])
            if re.search(rf"\b{re.escape(sym)}\b", near):
                continue
            problems.append(
                f"{doc.relative_to(ROOT)}:{lineno}: `{fp}:{n}` 处没有 `{sym}`"
                f"（±{WINDOW} 行内未见），该行实为：{src[n - 1].strip()[:60]}"
            )
    return problems


def main() -> int:
    if not all(reason.strip() for reason in HISTORICAL.values()):
        print("❌ 豁免表里有空理由 —— 豁免必须逐条说明原因")
        return 1

    index = _python_index()
    docs = [ROOT / "README.md", ROOT / "deploy" / "README.md"]
    docs += sorted((ROOT / "docs").glob("*.md"))

    problems: list[str] = []
    skipped_hist = 0
    for doc in docs:
        if not doc.exists():
            continue
        rel = doc.relative_to(ROOT).as_posix()  # ← 必须 as_posix()
        if rel in HISTORICAL:
            skipped_hist += 1
            continue
        problems.extend(_uncovered(doc, index))

    if problems:
        print("❌ 文档行号引用已经指错地方：")
        for p in problems:
            print(f"  {p}")
        print(
            "\n  ⇒ 这些行号以前是对的，代码漂移后就没人再核对过。\n"
            "    修法：`grep -n 'def <符号>' <文件>` 找到真身，把行号改对；\n"
            "    若该引用本就是历史现场证据，请把它加进本脚本的 HISTORICAL 并写明理由。"
        )
        return 1

    print(f"  ✅ 行号引用仍指到该指的地方（扫 {len(docs)} 份文档，豁免 {skipped_hist} 份历史归档）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
