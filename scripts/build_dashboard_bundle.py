#!/usr/bin/env python3
"""把 ``dashboard/dash-*.js`` 拼成 ``dashboard/dashboard.bundle.js``。

## 为什么拆了源文件却仍然只发一个请求

owner 要「按功能拆分」，考虑的是**运行效率**与**维护成本**两头：

- **维护成本** ⇒ 源文件按功能分成 7 份，各自成模块、互相不认识谁在谁前面。
- **运行效率** ⇒ 浏览器**仍然只下载一个文件**。

第二点比「拆成 7 个 ``<script>``」**更好**，不是将就：

| | 7 个 script | 拼接成一个 |
|---|---|---|
| 请求数 | **7** | **1** |
| 跨模块函数可见 | ❌ 各自 IIFE，互相看不见 | ✅ 同一个作用域 |
| 缓存命中 | 改一个文件，其余 6 个仍命中 | 改一个文件，**整个 bundle 失效** |

⚠️ 实测踩过：直接拆成 7 个 IIFE，**88 处跨模块调用**直接断
（`startPolling is not defined` 等）。逐个改调用点就不是「机械搬运」而是
高风险重构了。⇒ 源文件拆分 + 构建时拼接，是唯一**既分得开、又行为不变**的做法。

## 拼接是**逐字**的

本脚本只做三件事：按 :data:`ORDER` 取源文件、剥掉 IIFE 包裹与哨兵、
把 body 首尾相接。**不重排、不改写、不「顺手优化」任何一行** ——
diff 里出现任何非搬家的改动，都是 bug。

## 用法

    python scripts/build_dashboard_bundle.py          # 生成
    python scripts/build_dashboard_bundle.py --check  # 只校验是否同步（CI 用）
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
BUNDLE = DASH / "dashboard.bundle.js"

#: 进 bundle 的模块，顺序 = index.html 的脚本顺序 = tests.conftest.DASHBOARD_JS_ORDER
#: 里这 7 个 dash-* 的**相对顺序**（``tests/test_build_dashboard_bundle.py`` 钉住）。
#: ``ORDER[0]`` 的 ``<<<HEAD`` 是 bundle 唯一的权威头。
ORDER = (
    "dash-core.js",
    "dash-chrome.js",
    "dash-structure.js",
    "dash-signal.js",
    "dash-chart.js",
    "dash-alert.js",
    "dash-ops.js",
)

_HEAD = re.compile(r"// <<<HEAD\n(.*?)\n// >>>HEAD\n", re.S)
_FUNCS = re.compile(r"// <<<FUNCS\n(.*?)\n// >>>FUNCS\n", re.S)
_TAIL = re.compile(r"// <<<TAIL\n(.*?)\n// >>>TAIL", re.S)

BANNER = """// dashboard.bundle.js — **自动生成，请勿手改**
//
// 由 scripts/build_dashboard_bundle.py 从 7 个按功能拆分的源文件拼接而成：
{sources}
//
// ⚠️ 改了任何一个 dash-*.js 都要重跑 `python scripts/build_dashboard_bundle.py`。
//    CI 有门禁检查这个 bundle 是否与源文件同步（见 .github/workflows/ci.yml）。
//
// 为什么拼接成一个而不是发 7 个请求：
//   · 跨模块调用共 88 处，分成 7 个独立 IIFE 会**全部断掉**
//     （实测 `startPolling is not defined`）；
//   · 一个请求对缓存与首屏都更好；
//   · 源文件仍按功能分开，**维护成本**不受影响。
"""


_DECL = re.compile(r"^\s*(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)", re.M)


def _declared_names(head: str) -> set[str]:
    """头段里声明的顶层名字（const/let/var/function）。

    只用于比对"这个声明有没有进 bundle"，不做作用域分析。
    """
    return set(_DECL.findall(head))


def _parts(name: str) -> tuple[str, str, str]:
    text = (DASH / name).read_text(encoding="utf-8")
    mh, mf = _HEAD.search(text), _FUNCS.search(text)
    if not mh or not mf:
        raise SystemExit(f"{name} 缺少哨兵（// <<<HEAD / // <<<FUNCS）—— 拆分器版本不对")
    mt = _TAIL.search(text)
    return mh.group(1), mf.group(1), (mt.group(1) if mt else "")


def check_head_declarations(name: str, head: str, authoritative_head: str) -> None:
    """非权威模块的 ``<<<HEAD`` 不许声明权威头里没有的名字。

    允许其它模块的共享头与权威头有差异（拆分器复制的副本可能暂时落后），
    但**不允许它声明权威头里没有的名字**：bundle 只拼 ORDER[0] 的
    ``<<<HEAD``，其余模块头里的声明会被**静默丢弃**。

    R61 实测的后果：把 SOURCE_LABELS 只加进 dash-chrome.js 的头 →
    bundle 里没有这个声明 → renderChrome 跑到用它的那一行抛
    ReferenceError → 被上层 ``catch`` 吞掉 → 其后所有 DOM
    写入都不执行。页面**没有任何报错**，只是那几行永远停在初始值。
    静默不一致是这类 bug 的唯一入口，所以这里直接失败。
    """
    dropped = _declared_names(head) - _declared_names(authoritative_head)
    if dropped:
        raise SystemExit(
            f"{name} 的 <<<HEAD 声明了 {ORDER[0]} 权威头里没有的名字："
            f"{sorted(dropped)} —— 这些声明会被**静默丢弃**（bundle 只拼 "
            f"{ORDER[0]} 的头）。共享变量请加在 {ORDER[0]}，"
            "并把各模块的头同步成一致。"
        )


def build() -> str:
    head = funcs = tail = ""
    for i, name in enumerate(ORDER):
        h, f, t = _parts(name)
        if i == 0:
            head = h
        elif h.strip() != head.strip():
            check_head_declarations(name, h, head)
        funcs += ("\n\n" if funcs else "") + f
        if t:
            tail = t
    if not tail:
        raise SystemExit("没有模块带 <<<TAIL（导出块 + boot）—— 应只有 dash-core.js 有")
    return (
        BANNER.format(sources="\n".join(f"//   · {n}" for n in ORDER))
        + "\n"
        + head
        + "\n\n"
        + funcs
        + "\n\n"
        + tail
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check", action="store_true", help="只校验 bundle 是否与源文件同步（CI 用），不写盘"
    )
    a = ap.parse_args(argv)

    out = build()
    if a.check:
        cur = BUNDLE.read_text(encoding="utf-8") if BUNDLE.exists() else ""
        if cur == out:
            print("  ✅ bundle 与源文件同步")
            return 0
        print("  ❌ bundle 与源文件**不同步** —— 跑 `python scripts/build_dashboard_bundle.py`")
        return 1

    BUNDLE.write_text(out, encoding="utf-8")
    n = len(out.splitlines())
    print(f"  ✓ 已生成 dashboard.bundle.js（{n} 行 / {len(out)} B）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
