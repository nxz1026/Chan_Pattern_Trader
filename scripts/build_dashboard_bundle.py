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

## 共享头只拼 ``ORDER[0]`` 的那份（R77 起是**唯一**一份）

``dash-core.js`` 之外的 6 个模块，``// <<<HEAD`` 段必须是**空的**（只许放指路注释）。
R45 拆分时每个模块各存了一份共享头副本，但 ``build()`` 只取 ``ORDER[0]`` 的头 ——
另外 6 份从来没进过 bundle，是纯死代码，而且会漂移：

- R61：声明只加进 ``dash-chrome.js`` 的头 ⇒ bundle 里没有 ⇒ ``ReferenceError``
  被 ``catch`` 吞掉 ⇒ 页面无报错、后续 DOM 写入全不执行（known-traps #44）；
- R71/R76：改了 ``dash-core.js`` 的 ``SIGNAL_TYPE_LABELS``、6 份副本没跟 ⇒
  **渲染走的是副本里的绑定**，页面一直显示旧文案。两次都是靠人工同步才消掉。

⇒ 副本全部删除，:func:`check_quarantined_head` 把「又在某个模块头里加了一句」
钉成**构建期硬失败**。

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


#: 只在 :func:`check_quarantined_head` 里用：剥掉注释再判「空」。
_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


def _parts(name: str) -> tuple[str, str, str]:
    text = (DASH / name).read_text(encoding="utf-8")
    mh, mf = _HEAD.search(text), _FUNCS.search(text)
    if not mh or not mf:
        raise SystemExit(f"{name} 缺少哨兵（// <<<HEAD / // <<<FUNCS）—— 拆分器版本不对")
    mt = _TAIL.search(text)
    return mh.group(1), mf.group(1), (mt.group(1) if mt else "")


def check_quarantined_head(name: str, head: str) -> None:
    """非权威模块的 ``<<<HEAD`` 必须是**空的**（R77）。

    R45 拆分时，共享头（工具函数、状态文案表、常量）在每个模块里各存了一份
    副本，bundle 只拼 :data:`ORDER` ``[0]`` 的那份 —— 其余 6 份是死代码。
    R61 已经证明这个结构会静默吞掉声明（known-traps #44），R71/R76 又证明
    副本会漂移（改了权威表、副本没改，页面显示的还是旧文案，因为
    **渲染用的是副本里的那个绑定**）。

    R77 起副本全部删除，只留 ``dash-core.js`` 一份。这条检查把
    「以后有人又在某个模块头里加了一句」变成**构建期硬失败**，
    而不是等他上线后去猜为什么页面没变。

    注释不算内容：空哨兵段只放指路注释是合法的。
    """
    body = _COMMENT_RE.sub("", head).strip()
    if body:
        raise SystemExit(
            f"{name} 的 <<<HEAD 不是空的：{body[:120]!r}\n"
            f"  共享声明只许加在 {ORDER[0]} 的 <<<HEAD —— 其它模块的头"
            f"会被**静默丢弃**（bundle 只拼 {ORDER[0]} 的头）。\n"
            f"  R77 起这里应当只剩指路注释。"
        )


def build() -> str:
    head = funcs = tail = ""
    for i, name in enumerate(ORDER):
        h, f, t = _parts(name)
        if i == 0:
            head = h
        else:
            check_quarantined_head(name, h)
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
