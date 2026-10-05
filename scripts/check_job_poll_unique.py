#!/usr/bin/env python3
r"""门禁⑦：异步任务轮询必须是**唯一**实现（``dashboard/cpt_job.js``）。

## 为什么

R45 加 LLM 摘要时我**自己发明了一套**轮询（间隔/上限/失败/幂等读回），
而仓库里已有 ``dashboard.js`` 的 3 个 ``setTimeout``/``setInterval`` 轮询、
``market_a_share.js`` 1 个 —— 四份各写各的。

这与凭据消毒的处境**完全一样**：R44 发现 9 处重复实现，
收敛成 ``url_safety.js`` 唯一实现 + 门禁。**同一个坑不踩第二遍。**

## 判据

禁止 ``dashboard/*.js`` 里出现「自己实现**等任务**轮询」的函数：

  ✗ ``async function pollXxx(...)``  +  内部 setTimeout/setInterval
  ✓ ``window.CPTJob.poll(...)``

⚠️ **不禁止** ``setTimeout`` 本身 —— 一次性延迟（按钮复位、动画帧、
  ``loadLlmCalls`` 里的延迟重试）**不是轮询**。判据只抓
  「函数名像轮询 **且** 内部有定时器」的组合。

### 已知但**故意不合并**的一处

``dashboard.js::startPolling``（持续刷新快照）**名字像轮询但语义不同**：
它是「每 N 秒刷新一次，直到用户停掉」，而 ``CPTJob.poll`` 是
「等一个任务出结果，拿到就停」。前者还带 ``pinnedRange`` 判断与
A 股短路（日线收盘后不变，轮询是浪费）。
**硬塞进 CPTJob.poll 会让两个语义混在一个函数里** —— 那比重复更糟。

⚠️ 第一版正则漏了它：``\w*(poll|...)`` **大小写敏感**，
而它是 ``startPolling``（大写 P）⇒ 报成「无重复轮询实现」，
**漏报**。已加 ``re.IGNORECASE``。

本脚本**只报告不阻断**：清单清空前硬阻断只会让门禁红着被人忽略。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"

#: 唯一实现自己不作数。
ALLOW = {"cpt_job.js"}

#: 函数名像轮询
#: ⚠️ 必须 ``IGNORECASE`` —— ``startPolling`` 是大写 P，
#: 大小写敏感会把这一处**漏报成「无重复」**（第一版就如此）。
POLL_NAME = re.compile(r"^(\s*)(async\s+)?function\s+(\w*(poll|watch|retry)\w*)\s*\(", re.M | re.I)


def main() -> int:
    print("═" * 72)
    print("门禁⑦：**等任务**轮询的实现唯一性")
    print("═" * 72)
    offenders: list[str] = []
    for p in sorted(DASH.glob("*.js")):
        if p.name in ALLOW:
            continue
        text = p.read_text(encoding="utf-8")
        for m in POLL_NAME.finditer(text):
            name = m.group(3)
            # 取该函数体，看里面有没有定时器
            start = m.end()
            re.search(r"^\s*(?:async\s+)?function\s+\w+|^\s*\}\s*$", text[m.start() :], re.M)
            body = text[start : start + 2500]
            if not re.search(r"setTimeout|setInterval", body):
                continue
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"  dashboard/{p.name}:{line}  function {name}()")

    print("  唯一实现: dashboard/cpt_job.js（window.CPTJob.poll）")
    print("  加载顺序: index.html 第 2 个（紧随 url_safety.js）")
    if offenders:
        print(f"\n  ⚠️ 另有 {len(offenders)} 处自己实现的轮询：")
        for o in offenders:
            print(o)
        print("\n  ⚠️ 上面这处（startPolling）是「持续刷新」，**语义不同、故意不合并**。")
        print("  ⇒ **只报告不阻断**。硬阻断前先把清单清空，")
        print("     否则门禁长期红着就会被忽略 —— 那比没有门禁更糟。")
        return 0
    print("\n  ✅ 无重复轮询实现")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
