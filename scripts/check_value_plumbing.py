#!/usr/bin/env python3
"""门禁⑯：**值从数据源到屏幕这一路上的三类静默失效**。

## 为什么要这道门（R61 一天里三个 bug，全是"绿的"）

R61 修 A 股个股概览时，一天里抓到三个 bug。它们没有一个是"跑起来红了"
暴露的 —— 全部是「测试全绿、界面上数不对」：

 ① 盘前上游给 ``last=0.0``，昨收是 1274.89，涨跌幅算出 **−100%**。
    除法守卫当时只守**分母**（``if prev <= 0``），没人守分子；
    这个数会同时进「双数据集对比」和「涨跌幅」——
    **每个交易日早盘都亮一次"市场跌没了"，09:30 一到自己变回来。**
    （实测 2026-10-14 09:23 北京时间，新浪 ``hq.sinajs.cn``）

 ② ``dashboard.bundle.js`` 只拼 ``ORDER[0]=dash-core.js`` 的 ``// <<<HEAD``，
    在别的模块头里新增共享声明会被**静默丢弃**：``renderChrome`` 抛
    ReferenceError，被上层 ``catch`` 吞掉 —— 页面**零报错**，
    只是那之后所有 DOM 写入都不执行。单测读的是**源文件拼接**
    （``tests/conftest.py`` 的 ``dashboard_js()``），而丢声明发生在**构建**，
    所以单测永远绿。

 ③ ``dash-core.js`` 的 ``formatNumber`` 内部是 ``Math.abs(value)``（千分位
    格式化用），``formatPrice(-3.12)`` 返回 ``"3.12"`` ——
    **跌了 3 块会显示成涨了 3 块**。无异常、无 NaN、无边界报错，
    返回值"看起来完全正常"。

共因一句话：**失效点都不在当时写的那段源码里** ——
①在真实输入域、②在构建、③在被调用的共享工具。三者的传感器分别是
「哨兵值」「产物/DOM」「读函数体而不是读函数名」。

⇒ 这道门把三个传感器里**可以离线、可以自动化**的部分固定下来，
每次 CI 都跑一遍；**抓到的发现按可修复的粒度写进日志**（见下）。

## 三类规则（两档严重度）

  R1 分母守卫（error）  ``cpt/**/*.py`` 里除法，分母是**行情/计数类裸名字**
                        且函数体内找不到任何 ``> 0 / <= 0 / == 0 / is None /
                        not x / if x`` 守卫 ⇒ 报 error。
                        只判"裸名字"（可穿透 ``float()/int()/abs()`` 包装），
                        不判常量与复合表达式（``/ 1000``、``/(slow+1)``），
                        否则天天假阳性。

  R2 三层一致（error）   源 → 构建产物 → 部署目录。``build()`` 必须等于仓内
                        ``dashboard.bundle.js``；部署目录里同名文件必须与仓内
                        逐字节相同。部署目录不存在就跳过（开发机/CI 上没有）。

  R3 带符号量格式化（error）``dashboard/*.js`` 里 ``formatPrice/Number/Volume``
                        的实参带方向词（change/delta/pct/diff/divergence/…）
                        时，同一行必须有显式符号拼接
                        （``>= 0 ? "+" : "-"`` / ``Math.sign``）。
                        直接传负数进去 = bug ③。

  R4 判型硬化（note）    ``x === null ? … : x.method()`` —— 判的是 ``null``
                        不是类型，键**缺失**（``undefined``）时是 TypeError 而
                        不是降级显示。**只记日志、不让 CI 红**：多数点位上游
                        保证必给该键，红一次的成本高于收益；它是"该硬化"的清单。

## 判据标定纪律（同 R45 的教训：自制的检查器第一版基本都错）

R1 的守卫词表是**跑出来**的，不是想出来的：24 处除法逐处看守卫写法，
把 ``if not ratio or ratio <= 0``、``round(… ) if total else None``、
``(… ) if first.open else None`` 这类真实写法收进词表（见 ``_GUARDS``），
再对全仓跑一遍确认零假阳性。**改词表必须重跑全仓并在这里记一笔。**

## 日志约定（为什么不是 .md）

发现默认写到 ``docs/audit/probe-findings.log``：

- **不能叫 ``.md``**：``scripts/check_all_claims.py`` 会 ``rglob("**/*.md")``
  并把里面的路径/行号当**文档断言**验证 —— 生成物叫 .md 会让
  「改了 dashboard 一行」变成文档门禁红（假阳性）。
- 内容**不含时间戳/HEAD**，逐字节确定性：只有真的多了/少了发现才会 diff 出来。
  运行时间与 HEAD 打在 stdout，供人看。
- 每条发现给「位置 / 证据（源码原文）/ 为什么是问题 / 建议修法」，
  粒度按**能直接照着修**来定。

## 豁免纪律

``_PERMIT`` 里每条都必须写明理由，理由为空直接判失败 ——
否则豁免表会退化成垃圾桶（同 ``check_gate_coverage.py`` 的纪律）。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 默认日志。相对 ``ROOT`` 解析。
DEFAULT_LOG = "docs/audit/probe-findings.log"

#: 部署目录（nginx root）。不存在则 R2 跳过该半边。
DEPLOYED = Path("/var/www/cpt-dashboard")

#: 这些文件名不扫：产物本身（它由源拼出来，扫它会双报）。
_SKIP_JS = {"dashboard.bundle.js"}


# ── R1：分母守卫 ───────────────────────────────────────────────
#: 分母"像行情/计数"的名字。命中才进 R1 的检查范围。
_MARKETISH = re.compile(
    r"(prev|last|close|open|high|low|raw|factor|ratio|price|base|amount|volume|total|count|bars?)\b",
    re.I,
)
#: 可穿透的包装调用：``float(ratio)`` 与 ``ratio`` 是同一个守卫对象。
_UNWRAP = {"float", "int", "abs"}
#: 守卫写法词表。``{d}`` 会被替换成分母名字。
#: 标定于 2026-10-14：全仓 24 处除法，逐处看真实守卫写法后收敛到这些。
_GUARDS = (
    "{d} > 0",
    "{d} > 0.0",
    "{d} >= 1",
    "{d} >= 0",
    "{d} <= 0",
    "{d} == 0",
    "{d} == 0.0",
    "{d} != 0",
    "{d} is None",
    "not {d}",
    "if {d}",
    "{d} else",
    "{d} and",
    "{d} or",
)
#: 按「文件:函数名」豁免，理由必填（空理由判失败）。
_PERMIT: dict[str, str] = {}
#: R4 的豁免：按「文件:行号」或「文件:变量名」，理由必填。
#: 标定纪律同 ``_PERMIT``：每条都要能说出**为什么上游保证必给该键**。
_R4_ALLOW: dict[str, str] = {
    "dashboard/url_safety.js:url": "`url` 是函数形参，契约由调用方给；"
    '且下面紧跟 `typeof url === "string"` 分支，不是 payload 键读法（R61 标定）',
}


@dataclass
class Finding:
    """一条发现。``rule`` 形如 ``R1``；``severity`` 是 ``error`` 或 ``note``。"""

    rule: str
    severity: str
    where: str
    evidence: str
    why: str
    fix: str
    extra: str = ""

    def key(self) -> tuple[str, str, str]:
        return (self.rule, self.where, self.evidence)


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    def add(self, f: Finding) -> None:
        self.findings.append(f)

    def note(self, text: str) -> None:
        self.notes.append(text)


def _unwrap(node: ast.expr) -> ast.expr:
    """穿透 ``float()/int()/abs()`` 包装，拿到真正的被守卫对象。"""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id not in _UNWRAP or len(node.args) != 1:
            break
        node = node.args[0]
    return node


def _dotted(node: ast.expr) -> str | None:
    """``basis.last_close`` → ``"basis.last_close"``；认不出返 None。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _func_spans(tree: ast.AST, src: str) -> list[tuple[int, int, str]]:
    """每个函数的 ``(起始行, 结束行, 函数名)``，按起始行倒序（从内到外找）。"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end = getattr(node, "end_lineno", node.lineno)
            out.append((node.lineno, end, node.name))
    return sorted(out, key=lambda t: t[0], reverse=True)


def check_divisions(root: Path, rep: Report) -> int:
    """R1：扫 ``cpt/**/*.py`` 的除法，分母是行情/计数裸名字却无守卫 ⇒ error。"""
    checked = 0
    for path in sorted((root / "cpt").rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:  # pragma: no cover — 语法错的源码不该到这一步
            continue
        lines = src.splitlines()
        spans = _func_spans(tree, src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            den = _dotted(_unwrap(node.right))
            if den is None or not _MARKETISH.search(den):
                continue
            func = next((n for a, b, n in spans if a <= node.lineno <= b), "<module>")
            checked += 1
            if f"{path.relative_to(root)}:{func}" in _PERMIT:
                continue
            span = next(((a, b) for a, b, _ in spans if a <= node.lineno <= b), (1, len(lines)))
            body = "\n".join(lines[span[0] - 1 : span[1]])
            if any(g.format(d=den) in body for g in _GUARDS):
                continue
            seg = (ast.get_source_segment(src, node) or "").strip().replace("\n", " ")
            rep.add(
                Finding(
                    rule="R1",
                    severity="error",
                    where=f"{path.relative_to(root)}:{node.lineno}（函数 {func}）",
                    evidence=f"{lines[node.lineno - 1].strip()}  ⟵ 表达式 `{seg[:70]}`",
                    why=f"分母 `{den}` 来自行情/库表，可以是 0 或缺失；"
                    "只守分母的写法正是 R61 bug ① 的成因（盘前 last=0.0 → −100%）",
                    fix=f"相除前加 `if not {den} or {den} <= 0: return None`"
                    "（参照 cpt/application/a_share_snapshot.py 的 `_pct_from`）",
                )
            )
    return checked


# ── R2：源 → 构建 → 部署 ───────────────────────────────────────
def check_assembly(root: Path, rep: Report, deployed: Path | None) -> None:
    """R2：``build()`` == 仓内 bundle；部署目录同名文件 == 仓内。"""
    scripts = root / "scripts"
    if not (scripts / "build_dashboard_bundle.py").exists():
        rep.note("R2 跳过：临时仓库里没有 build_dashboard_bundle.py")
        return
    sys.path.insert(0, str(scripts))
    try:
        import build_dashboard_bundle as b  # noqa: PLC0415 — 延迟导入，临时仓库里可能不存在
    except ImportError as exc:  # pragma: no cover
        rep.note(f"R2 跳过：导入构建脚本失败 {exc}")
        return
    try:
        built = b.build()
    except Exception as exc:  # noqa: BLE001 — 构建脚本自身报错要变成发现，不是崩溃
        rep.add(
            Finding(
                rule="R2",
                severity="error",
                where="scripts/build_dashboard_bundle.py",
                evidence=f"{type(exc).__name__}: {exc}",
                why="构建脚本自己抛异常 ⇒ 产物与源的关系无人保证（R61 bug ② 的同族）",
                fix="修构建脚本；`python scripts/build_dashboard_bundle.py` 手工跑一遍看堆栈",
            )
        )
        return
    cur = b.BUNDLE.read_text(encoding="utf-8") if b.BUNDLE.exists() else ""
    if cur != built:
        rep.add(
            Finding(
                rule="R2",
                severity="error",
                where="dashboard/dashboard.bundle.js",
                evidence=f"仓内 bundle {len(cur)} B ≠ build() {len(built)} B",
                why="产物与源不同步 ⇒ 浏览器跑的是旧代码，而所有单测读的是新源文件"
                "（测试全绿、页面不动，这正是 R61 bug ② 的现场）",
                fix="跑 `python scripts/build_dashboard_bundle.py` 重新生成并提交产物",
            )
        )
    if deployed is None or not deployed.is_dir():
        rep.note("R2 部署半边跳过：部署目录不存在")
        return
    dash = root / "dashboard"
    if not dash.is_dir():
        rep.note("R2 部署半边跳过：仓内没有 dashboard/")
        return
    import hashlib  # noqa: PLC0415

    for src in sorted(dash.iterdir()):
        if not src.is_file():
            continue
        dst = deployed / src.name
        if not dst.exists():
            continue  # 部署目录可以多/少文件，只比同名内容
        if hashlib.sha256(src.read_bytes()).digest() != hashlib.sha256(dst.read_bytes()).digest():
            rep.add(
                Finding(
                    rule="R2",
                    severity="error",
                    where=f"{dst}",
                    evidence=f"与仓内 dashboard/{src.name} 内容不同",
                    why="线上跑的不是仓里的文件 ⇒ 「本地已修」与「用户看到的」是两回事",
                    fix="跑 `deploy/dashboard-sync.sh`（或 `--http`）同步，并刷新页面核对",
                )
            )


# ── R3 / R4：前端格式化 ────────────────────────────────────────
_DIRECTION = re.compile(
    r"(change|delta|pct|diff|divergence|spread|drawdown|deviation|pnl|profit|balance)", re.I
)
#: 显式拼符号的写法（R61 修 ③ 用的就是这个）。
_SIGNED = re.compile(r"(>=\s*0\s*\?\s*[\"'\`]\s*\+|>\s*0\s*\?\s*[\"'\`]\s*\+|Math\.sign)")
#: ``x === null ? … : x.foo(`` —— 判 null 不判类型。
_NULL_GUARD_CALL = re.compile(r"\b(\w+)\s*===\s*null\s*\?[^;]*?\b\1\s*\.\s*(\w+)\s*\(")


def check_js_formatting(root: Path, rep: Report, cats: set[str] | None = None) -> int:
    """R3（error）+ R4（note）：带符号量的格式化、``null`` 判型。"""
    want = cats or {"R3", "R4"}
    dash = root / "dashboard"
    if not dash.is_dir():
        rep.note("R3/R4 跳过：仓内没有 dashboard/")
        return 0
    calls = 0
    for path in sorted(dash.glob("*.js")):
        if path.name in _SKIP_JS:
            continue
        rel = path.relative_to(root)
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(r"format(?:Price|Number|Volume)\(", line):
                calls += 1
                if "R3" not in want:
                    continue
                if _DIRECTION.search(line[m.start() :]) and not _SIGNED.search(line):
                    rep.add(
                        Finding(
                            rule="R3",
                            severity="error",
                            where=f"{rel}:{i}",
                            evidence=line.strip()[:110],
                            why="`formatNumber` 内部是 `Math.abs(value)`，负数进去符号就没了 ——"
                            "跌 X 显示成涨 X，且返回值看起来完全正常（R61 bug ③）",
                            fix="调用方自己拼符号："
                            '`${v >= 0 ? "+" : "-"}${formatPrice(Math.abs(v))}`',
                        )
                    )
            if "R4" not in want:
                continue
            guard = _NULL_GUARD_CALL.search(line)
            if not guard:
                continue
            var, method = guard.group(1), guard.group(2)
            if f"{rel}:{var}" in _R4_ALLOW or f"{rel}:{i}" in _R4_ALLOW:
                continue
            # 只认**直接从对象属性读出来的局部变量**（``const div = dc.divergence_pct;``）：
            #   · 函数形参 → 契约由调用方给（`url_safety.js` 的 `url`）；
            #   · ``num(metrics.change_pct)`` / 三元表达式 → 结果只可能是 null 或**数字**，
            #     永远不会是 undefined ⇒ 判 null 已经够了。
            # 不收窄到这一步这条规则就是满屏噪声（标定于 2026-10-14：初版 4 条里 3 条是噪声）。
            if not re.search(
                rf"\b(?:const|let|var)\s+{re.escape(var)}\s*=\s*"
                rf"[A-Za-z_$][\w$]*(?:\?\.)?\.[\w$]+\s*;",
                text,
            ):
                continue
            rep.add(
                Finding(
                    rule="R4",
                    severity="note",
                    where=f"{rel}:{i}",
                    evidence=line.strip()[:110],
                    why=f"判的是 `{var} === null` 而不是类型；键**缺失**（undefined）时"
                    f"走到 `{var}.{method}()` 是 TypeError，不是降级显示",
                    fix=f'用 `typeof {var} === "number" ? … : "—"` 之类的判型'
                    "（判型要对得上这个量的真实类型），"
                    "或确认上游必给该键后写进 `_R4_ALLOW` 并注明理由",
                )
            )
    return calls


# ── 输出 ──────────────────────────────────────────────────────
def render(rep: Report, counters: str) -> str:
    """生成日志正文。**确定性**：不含时间戳/HEAD，只有发现变了才会 diff 出来。"""
    errs = [f for f in rep.findings if f.severity == "error"]
    notes_f = [f for f in rep.findings if f.severity == "note"]
    out = [
        "CPT 值管道探针 · 发现日志",
        "=" * 72,
        "",
        "本文件由 scripts/check_value_plumbing.py 生成（勿手改）。",
        "判据与标定见该脚本 docstring；改判据必须重跑全仓并把标定记在那里。",
        "",
        "扫描范围：cpt/**/*.py 的除法分母守卫（R1）· 源→构建→部署（R2）·",
        "          dashboard/*.js 的带符号量格式化（R3）· null 判型硬化（R4）",
        f"本轮计数：{counters}",
        "",
    ]
    if rep.findings:
        by_rule: dict[str, int] = {}
        for f in rep.findings:
            by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        out += [
            "按判据：" + " · ".join(f"{k} × {v}" for k, v in sorted(by_rule.items())),
            "",
        ]
    for f in errs + notes_f:
        out += [
            f"[{f.severity}] {f.rule} · {f.where}",
            f"  证据：{f.evidence}",
            f"  为什么：{f.why}",
            f"  建议：{f.fix}",
            "",
        ]
    if not rep.findings:
        out += ["无发现：四类规则本轮全过。", ""]
    if rep.notes:
        out += ["附注（跳过/降级，不算发现）："]
        out += [f"  - {n}" for n in rep.notes]
        out += [""]
    out += [
        "-" * 72,
        f"合计 {len(errs)} 个 error、{len(notes_f)} 个 note"
        f"（error 会让本门禁返回 1，note 只进日志）。",
        "",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", default=DEFAULT_LOG, help=f"发现日志（默认 {DEFAULT_LOG}）")
    ap.add_argument("--deployed", default=str(DEPLOYED), help="部署目录（不存在则跳过该半边）")
    ap.add_argument("--no-log", action="store_true", help="只打印，不写日志文件")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument(
        "--cat",
        default="R1,R2,R3,R4",
        help="只跑指定判据（逗号分隔）。给 selftest_gates 的 `脚本#分类` 夹具用",
    )
    a = ap.parse_args(argv)

    cats = {c.strip().upper() for c in a.cat.split(",") if c.strip()}
    unknown = cats - {"R1", "R2", "R3", "R4"}
    if unknown:
        print(f"❌ --cat 不认识：{sorted(unknown)}（可选 R1/R2/R3/R4）")
        return 2

    for table, name in ((_PERMIT, "_PERMIT"), (_R4_ALLOW, "_R4_ALLOW")):
        for key, reason in table.items():
            if not reason.strip():
                print(f"❌ {name} 里有空理由：{key} —— 豁免必须逐条说明原因")
                return 1

    rep = Report()
    divisions = check_divisions(ROOT, rep) if "R1" in cats else 0
    if "R2" in cats:
        check_assembly(ROOT, rep, Path(a.deployed) if a.deployed else None)
    calls = check_js_formatting(ROOT, rep, cats)
    counters = (
        f"判据 {','.join(sorted(cats))} · "
        f"除法 {divisions} 处 · 格式化调用 {calls} 处 · 发现 {len(rep.findings)} 条"
    )

    body = render(rep, counters)
    if not a.no_log:
        log = Path(a.log)
        log = log if log.is_absolute() else ROOT / log
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(body, encoding="utf-8")

    if not a.quiet:
        print(body)
        if not a.no_log:
            print(f"→ 日志已写入 {Path(a.log)}")

    errs = rep.errors
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
