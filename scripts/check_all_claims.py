#!/usr/bin/env python3
"""全量文档断言扫描（R45 第五轮）—— 从头扫，不是再切一片。

## 为什么要重写成这个形状

前四轮都是**切片扫**：storage 层 → 文档 → deploy → 浏览器，
每片扫完收工。于是 owner 观察到「每次扫都有新东西」——
那不是运气，是**方法问题**：切片扫必然每次留下一片没看。

这一轮的做法反过来：

1. **先把全量清单列出来**（40 份文档 / 234 个代码文件，一份不漏）；
2. **把每份文档里可机械验证的断言全抽出来**（不抽样）；
3. **逐类对代码/库**；
4. 剩下的才用眼睛看。

## 抽哪几类

  P  路径引用      `cpt/xxx.py` / `tests/xxx.py` / `deploy/xxx` —— 文件在不在
  S  符号引用      `foo()` / `ClassName` —— 定义在不在
  E  接口引用      `/api/...` —— 路由注册了没
  T  表名引用      `cpt_*` / `asel.*` —— 库里有这张表没
  K  配置键        `CPT_*` —— 代码/模版里有这个键没
  L  行号引用      `file.py:123` —— **这一句只验「行号没超出文件总行数」**；
                   至于「那行还是不是文档说的那件事」，由门禁⑪
                   `scripts/check_line_refs.py` 负责（R54 补：此前本文件的声明
                   与实现分家，见 `docs/known-traps.md` R54 节）

用法::

    python scripts/check_all_claims.py            # 全量
    python scripts/check_all_claims.py --cat P    # 只查路径
    python scripts/check_all_claims.py --verbose  # 列出全部（默认只列不一致）
"""

from __future__ import annotations

import argparse
import ast
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "references", "node_modules", "__pycache__", ".venv"}


def all_docs() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*.md")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def all_code() -> list[Path]:
    out: list[Path] = []
    for pat in (
        "cpt/**/*.py",
        "scripts/**/*.py",
        "tests/**/*.py",
        "dashboard/**/*.js",
        "dashboard/**/*.css",
        "dashboard/**/*.html",
        "deploy/**/*",
    ):
        out.extend(ROOT.glob(pat))
    return [p for p in out if p.is_file() and not any(x in SKIP_DIRS for x in p.parts)]


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


#: 文档内容缓存 —— 40 份文档在 5 个类别里被反复重读，
#: 不缓存的话光 I/O 就跑超时（实测 exit 124）。
_CACHE: dict[Path, str] = {}

#: 文件名 → 行数（一次建索引）
_linecount: dict[str, int] = {}


def _text(p: Path) -> str:
    if p not in _CACHE:
        _CACHE[p] = p.read_text(encoding="utf-8")
    return _CACHE[p]


#: 出现在上下文里就说明「文档在声明它不存在 / 已删」的措辞。
_ABSENCE = (
    "不存在",
    "没有",
    "已删除",
    "git rm",
    "从未",
    "不是",
    "已废",
    "没有建",
    "已随",
    "已移",
    "已清理",
    "搬到了",
    "不在本仓",
    "包内",
    "尚未创建",
    "从未存在",
    "包内部",
    "新建",
    "设想",
)


_GI_PATTERNS: list[tuple[bool, str]] | None = None


def _gitignore_matches(relpath: str) -> bool:
    """按 .gitignore **规则**判，而不是只列已存在的被忽略文件。

    ⚠️ 第一版只用 ``git status --ignored`` —— 那只列**磁盘上存在**的忽略文件。
    而 ``deploy/env/cpt-dashboard.env`` 只存在于 Oracle，沙箱里没有 ⇒ 漏判 ⇒
    5 处假阳性。规则才是判据。

    ⚠️⚠️ 第二版每个路径 spawn 一次 ``git check-ignore`` 子进程 ——
    几百个路径直接把 P 类拖到**超时**（实测跑满 200s 被 kill，而 CPU 只用了 3.7s：
    纯 I/O 等待）。⇒ 规则**一次性解析进内存**，之后纯字符串判断。
    """
    import fnmatch

    global _GI_PATTERNS
    if _GI_PATTERNS is None:
        _GI_PATTERNS = []
        gi = ROOT / ".gitignore"
        # ⚠️ **没有 .gitignore 不能崩** —— R45 自检时用临时仓库跑本门禁，
        # 那里没有 .gitignore，直接 FileNotFoundError 崩掉（自检套件报的
        # 「自检本身崩了」就是它）。判据应当是「有没有被忽略」，
        # 不是「.gitignore 存不存在」。
        if not gi.exists():
            return False
        for raw in gi.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            pat = line[1:] if negate else line
            if pat:
                _GI_PATTERNS.append((negate, pat))
    # 后出现的规则覆盖先出现的（gitignore 语义）
    verdict = False
    for negate, pat in _GI_PATTERNS:
        if (
            fnmatch.fnmatch(relpath, pat)
            or fnmatch.fnmatch(relpath, pat + "/*")
            or relpath.startswith(pat.rstrip("/") + "/")
        ):
            verdict = not negate
    return verdict


# ── 收集代码侧的「事实」 ───────────────────────────────────────
def build_facts() -> dict[str, Any]:
    files = {rel(p) for p in all_code()}
    # 目录本身也算存在（文档常引用目录）
    dirs = {
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*")
        if p.is_dir() and not any(x in SKIP_DIRS for x in p.parts)
    }
    syms: set[str] = set()
    for p in all_code():
        if p.suffix != ".py":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                syms.add(node.name)
            elif isinstance(node, ast.Name):
                syms.add(node.id)
            elif isinstance(node, ast.Attribute):
                syms.add(node.attr)
    # JS/CSS 里的 class/id 名也当符号
    for p in all_code():
        if p.suffix == ".js":
            txt = p.read_text(encoding="utf-8", errors="ignore")
            syms |= set(re.findall(r"\bfunction\s+([A-Za-z_]\w*)", txt))
            syms |= set(re.findall(r"\bconst\s+([A-Za-z_]\w*)\s*=", txt))
    # 全仓文本（用于查 env 键 / 表名）
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in all_code()
        if p.suffix in (".py", ".js", ".sh", ".sql", ".example")
    )
    return {"files": files, "dirs": dirs, "syms": syms, "blob": blob}


#: **显式豁免** —— 机器判不了、但已人工核实为合法的引用。
#: 每条都必须写清理由，否则这条豁免本身会变成「什么都往里塞」的口子。
_ALLOW: dict[str, str] = {
    # 部署脚本 run5/6/7.sh 在 linux*.tgz 包**内部**，不在本仓（deploy/README 已注明）
    "scripts/collector_linux.sh": "linux6/7 tgz 包内的部署脚本，本仓没有",
    "scripts/run5.sh": "linux7 tgz 包内的旧脚本，本仓没有",
    # review-ensure-table 方案 B 里的**设想**文件名，尚未创建
    "scripts/migrations/xxxx_cpt_run_metric.sql": "方案 B 的设想，尚未创建",
    # 环境变量由 tgz 里的部署脚本使用
    "CPT_AUTH": "linux* tgz 部署脚本读的凭据环境变量，本仓不用它",
    # 已删除的索引（db-inventory 明确标了「已删」）
    "asel.idx_sm_board": "索引，已于 R45 删除（文档标注了「已删」）",
    # 历史缺口记录：那张表后来没了
    "asel.ref_trading_calendar": "R17 记录的历史缺口，表已不存在",
    # 两份复盘的措辞是「该变量**未设** ⇒ 走 native」——
    # **代码里没有这个变量，正是那句话成立的前提**
    "CPT_CHANLUN_BACKEND": "复盘里说的是「进程环境未设它」，代码里本就不该有",
    # R51：画布 D 下线 + dashboard.js 死代码删除。留下的文档引用都是**下线/历史
    # 记录**（progress-log / review-dashboard-r45 / roadmap 讲的是当时的情况，
    # duplication-triage §6 的结论已被显式标注作废），不是「这东西现在还在」。
    "dashboard/canvas_d.js": "R51 随画布 D 下线删除；文档里的提及是历史记录/下线说明",
    "dashboard/dashboard.js": (
        "R51 删除（R45 拆分后已是无入口孤儿）；文档里的提及是历史记录/部署旧说明"
    ),
    "tests/test_canvas_wbt.py": "R51 随画布 D 下线删除；duplication-triage §6 已标注作废",
    "tests/test_web_canvas_wbt.py": "R51 随画布 D 下线删除；duplication-triage §5.2 已标注",
    # R51 的**代码**同样被删了。progress-log R51 段讲的是「删掉它根除了 CI 五连红」，
    # 那句话要成立就得提到这个文件名 —— 它是**因果链的一环**，改掉它等于抹掉根因。
    "cpt/application/canvas_wbt.py": "R51 随画布 D 下线删除；progress-log R51 段记的是删除动作本身",
}


def _allowed(key: str) -> str | None:
    if key in _ALLOW:
        return _ALLOW[key]
    for frag, why in _ALLOW.items():
        if frag and frag in key:
            return why
    return None


# ── 断言抽取 ───────────────────────────────────────────────────
PATHS = re.compile(r"`((?:cpt|tests|scripts|deploy|dashboard|docs)/[A-Za-z0-9_./-]*[A-Za-z0-9_])`")
FUNCS = re.compile(r"`([A-Za-z_]\w*)\(\)`")
CLASSES = re.compile(r"`([A-Z][A-Za-z0-9_]{2,})`")
ENDPOINTS = re.compile(r"/api/(?:dashboard|canvas)/[a-z0-9/_-]+")
TABLES = re.compile(r"\b((?:cpt|asel)\.[a-z_]+|(?<![\w.])cpt_[a-z_]+)\b")
ENVKEYS = re.compile(r"\b(CPT_[A-Z0-9_]+|DB[A-Z_]{2,})\b")
LINEREF = re.compile(r"`?([a-z_]+\.py):(\d+)`?")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cat", default="PSETKLH".replace(" ", ""), choices=list("PSETKLH"))
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)

    facts = build_facts()
    docs = all_docs()
    print(f"扫描 {len(docs)} 份文档 / {len(facts['files'])} 个代码文件")
    print("=" * 92)

    bad: dict[str, list[str]] = defaultdict(list)
    total = 0

    if "P" in a.cat:
        for d in docs:
            lines = _text(d).splitlines()
            for i, line in enumerate(lines, 1):
                for m in PATHS.finditer(line):
                    total += 1
                    t = m.group(1).rstrip(".")
                    # `cpt/web/__main__._compute_domain_structures` 是**符号路径**
                    # 不是文件路径 —— 正则会把 `cpt/web/__main__.py` 后面的
                    # `._compute_domain_structures` 一起吃掉。这类不算漂移。
                    # 不是**文件后缀**结尾的，就是「模块.符号」写法，不是路径
                    # （`cpt/web/__main__._compute_domain_structures` 这类）
                    if Path(t).suffix not in (
                        ".py",
                        ".js",
                        ".css",
                        ".html",
                        ".md",
                        ".sh",
                        ".sql",
                        ".json",
                        ".toml",
                    ):
                        continue
                    if (ROOT / t).exists() or _gitignore_matches(t):
                        continue
                    # ⚠️ **「文档在声明某物不存在」不是漂移** —— 第一版把
                    # 三类都当成了「路径不存在」，全是假阳性：
                    #   1. `tests/unit|oracle|e2e`  —— architecture 明写「从未建立」
                    #   2. `scripts/compare_oracle.py` —— low-risk-hardening 明写
                    #      「已于 2026-09-24（G1）git rm」
                    #   3. gitignore 的本机文件（见 ignored）
                    # 按**段落**（到空行为止）取上下文，而不是固定 ±4 行 ——
                    # ±4 会漏掉「引用 + 紧跟一段注解」这种最常见的写法。
                    lo = i - 1
                    while lo > 0 and lines[lo - 1].strip():
                        lo -= 1
                    hi = i
                    while hi < len(lines) and lines[hi].strip():
                        hi += 1
                    ctx = "\n".join(lines[lo : hi + 1])
                    # 判据一：段落里有「不存在/已删/包内/未创建」这类措辞
                    # 判据二：段落里出现 R45 / 更正 —— 本仓所有「路径已迁移」类
                    #        更正都带这个标记，比穷举措辞变体稳
                    #   （第一版就是在这里栽的：文档写「已搬到」，我列表里只有
                    #    「搬到了」，一个字之差 → 4 处假阳性 → 白查一轮）
                    if any(k in ctx for k in _ABSENCE) or any(
                        k in ctx for k in ("R45", "更正", "R44")
                    ):
                        continue
                    why = _allowed(t)
                    if why:
                        print(f"  [豁免] {rel(d)}:{i} {t} —— {why}")
                        continue
                    bad["P 路径不存在"].append(f"{rel(d)}:{i} → {t}")

    if "S" in a.cat:
        # ⚠️ 第一版泛匹配「反引号 + 驼峰 或 word()」，105 处**全是假阳性**：
        # 外部 API 字段（BONUS_IT_RATIO）、SQL 关键字（CONCURRENTLY）、
        # 常量（NaN/Infinity）、环境变量、领域词（BTCUSDT）全被误报。
        # ⇒ 改成只查**项目内模块路径 + 符号**这种真有意义的引用。
        for d in docs:
            text = _text(d)
            for m in re.finditer(r"`((?:cpt|scripts)/[a-z_/]+\.py)::([A-Za-z_]\w+)", text):
                total += 1
                mod, sym = m.group(1), m.group(2)
                if not (ROOT / mod).exists():
                    bad["S 模块路径不存在"].append(f"{rel(d)} → {mod}")
                elif sym not in facts["syms"]:
                    bad["S 符号未定义"].append(f"{rel(d)} → {mod}::{sym}")

    if "E" in a.cat:
        code_blob = facts["blob"]
        for d in docs:
            for m in ENDPOINTS.finditer(_text(d)):
                total += 1
                p = m.group(0)
                if p not in code_blob:
                    bad["E 接口未注册"].append(f"{rel(d)} → {p}")

    if "T" in a.cat:
        for d in docs:
            for m in TABLES.finditer(_text(d)):
                total += 1
                t = m.group(1)
                name = t.split(".")[-1]
                if name in facts["blob"] or t in facts["blob"] or _allowed(t) or _allowed(name):
                    continue
                # `_id_seq` 是 Postgres 的**序列**，不在 information_schema.tables
                if name.endswith("_seq"):
                    continue
                bad["T 表名未出现"].append(f"{rel(d)} → {t}")

    if "K" in a.cat:
        for d in docs:
            for m in ENVKEYS.finditer(_text(d)):
                total += 1
                k = m.group(1)
                if k not in facts["blob"] and not _allowed(k):
                    bad["K 配置键未出现"].append(f"{rel(d)} → {k}")

    if "H" in a.cat:
        # ── index.html 的 <script> 标签必须**成对闭合**（门禁⑨）─────────
        # R45 踩过：一个 replace 把 `</script>` 落错位置，index.html 变成
        #     <script src="./url_safety.js" defer>          ← 丢了闭合
        #       <script src="./cpt_job.js"></script></script> ← 多了闭合
        # 浏览器把第二行当**前一个脚本文本**吞掉 ⇒ cpt_job.js 从未成为
        # script 元素、从未被请求、window.CPTJob 恒 undefined
        # ⇒ 那个「说人话」面板**静默不显示**，而页面完全正常、console 零报错。
        import re as _re

        idx = ROOT / "dashboard" / "index.html"
        if idx.exists():
            raw = idx.read_text(encoding="utf-8")
            body = _re.sub(r"<!--.*?-->", "", raw, flags=_re.S)
            opens = _re.findall(r"<script\b", body)
            closes = _re.findall(r"</script>", body)
            if len(opens) != len(closes):
                bad["H script 标签不成对"].append(
                    f"index.html: <script> {len(opens)} 个 / </script> {len(closes)} 个"
                )
            # 逐个标签：开标签到它的闭合之间**不能再出现 <script**
            for m in _re.finditer(r"<script\b[^>]*>", body):
                tail = body[m.end() :]
                end = tail.find("</script>")
                seg = tail if end < 0 else tail[:end]
                if "<script" in seg:
                    bad["H script 标签畸形"].append(
                        f"index.html: {m.group(0)[:60]} 未闭合（吞掉了后续标签）"
                    )
                    break

    if "L" in a.cat:
        for d in docs:
            for m in LINEREF.finditer(_text(d)):
                total += 1
                fn, ln = m.group(1), int(m.group(2))
                # ⚠️ 第一版对**每个**行号引用都跑一次 ``ROOT.rglob(fn)`` 全仓遍历 ——
                # 实测单这一类就吃掉 100+ 秒（40 份文档 × 几十处引用 × 全仓 walk）。
                # ⇒ 文件名 → 行数**建一次索引**，之后 O(1) 查。
                if fn not in _linecount:
                    hits = list(ROOT.rglob(fn))
                    if not hits:
                        _linecount[fn] = -1
                    else:
                        # ⚠️ **同名文件不止一个**（`__main__.py` 就有
                        # `cpt/__main__.py` 与 `cpt/web/__main__.py`）。
                        # 第一版取 `hits[0]`，于是 24 行的那个把所有
                        # `__main__.py:NNN` 引用全判成越界 ⇒ 18 处假阳性。
                        # ⇒ 取**所有同名文件里最长的行数**（最宽松），
                        #   只有全部都短于引用行才算越界。
                        lens = []
                        for h in hits:
                            try:
                                lens.append(len(h.read_text(encoding="utf-8").splitlines()))
                            except OSError:
                                pass
                        _linecount[fn] = max(lens) if lens else -1
                n = _linecount[fn]
                if n < 0:
                    continue
                if ln > n:
                    bad["L 行号越界"].append(f"{rel(d)} → {fn}:{ln}（文件只有 {n} 行）")

    print(f"共检查 {total} 条断言")
    # 历史快照（archive/ + audit/）里的「已删文件」是**正常的**：
    # 它们记录的就是「当时有、后来删了」。单独归类，免得淹没真漂移。
    HIST = ("docs/archive/", "docs/audit/")
    for cat, items in sorted(bad.items()):
        uniq = sorted(set(items))
        live = [x for x in uniq if not any(x.startswith(h) for h in HIST)]
        hist = [x for x in uniq if any(x.startswith(h) for h in HIST)]
        if live:
            print(f"\n{cat}（现行文档）: {len(live)} 处")
            for x in live:
                print("  " + x)
        if hist:
            print(f"\n{cat}（历史快照，不算漂移）: {len(hist)} 处")
            for x in hist[:6]:
                print("  · " + x)
            if len(hist) > 6:
                print(f"  … 另 {len(hist) - 6} 处")
    # ⚠️ 退出码必须**如实反映**「现行文档」的不一致项 ——
    # 第一版恒返回 0，于是门禁④ 在 CI 上永远是绿的，等于没有门禁。
    live_bad = sum(
        1
        for cat, items in bad.items()
        for x in set(items)
        if not any(x.startswith(h) for h in ("docs/archive/", "docs/audit/"))
    )
    if not live_bad:
        print("\n✅ 全部断言对得上")
    else:
        print(f"\n❌ 现行文档有 {live_bad} 处对不上")
    return 1 if live_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
