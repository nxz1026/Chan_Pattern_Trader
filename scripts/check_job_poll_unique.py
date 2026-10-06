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

（R51 删了 ``dashboard/dashboard.js``，``startPolling`` / ``stopPolling``
现在在 ``dashboard/dash-ops.js`` —— 语义问题不变，位置变了。）

## 为什么现在**阻断**（之前不阻断的结论已被推翻）

第一版是「只报告不阻断」，理由是「清单清空前硬阻断只会让门禁红着被人忽略」。
**这个理由的前提当时就不成立**，现在更是错的：

- 门禁扫的是 ``dashboard/*.js``，而 ``dashboard.bundle.js`` 是
  ``scripts/build_dashboard_bundle.py`` **生成**的（里面是 ``dash-ops.js``
  的一份拷贝）⇒ 扫到它必然命中 ``startPolling``，**永远走「发现违规」分支**。
  一个永远命中、却永远返回 0 的门禁，**不是宽松，是根本没在工作** ——
  真出现第二份轮询时它和「没查」长得一模一样。
- 排除生成物后，剩下的命中项才是**真·源码里的重复实现**，这才该阻断。

⇒ 现在：发现违规 ⇒ 退出码 **1**。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"

#: 唯一实现自己不作数。
ALLOW = {"cpt_job.js"}

#: **按函数名**的显式豁免（不是按文件）—— 2026-10-06，owner 拍板。
#:
#: ``dash-ops.js::startPolling`` / ``stopPolling`` **不是** ``CPTJob.poll`` 的重复
#: 实现，两者是**不同语义**：
#:
#: - ``CPTJob.poll``（``cpt_job.js``）是**有界**轮询：``maxTries`` 到顶就停、
#:   抛错即停，供「等一个任务跑完」用（``market_a_share.js:459`` 就是这么用的）。
#: - ``startPolling`` 是**无尽** ``setInterval``：每 N 秒刷新一次快照直到用户
#:   停掉，单次 fetch 失败要继续下一轮（否则一次网络抖动就让面板永久停更）。
#:   它还额外管一个 ``staleTimer``。
#:
#: 把后者改写成前者的调用，得靠「把 ``maxTries`` 设成无穷大、并在 ``onFail``
#: 里忽略错误」来凑 —— 那是把两种语义硬塞进一个函数，正是本文件头警告的
#: 「比重复更糟」。所以走显式豁免这条路：**写清理由再放行**，与文件头
#: 「收敛办法二选一」的后一条一致。
#:
#: 这次是**代码事实核对后的结论**，不是为了让门禁变绿：owner 最初的口头决定是
#: 「并进 CPTJob.poll」，读到两个实现的真实语义后改为豁免。
ALLOW_FUNCS = {"startPolling", "stopPolling"}

#: **生成物**必须排除，否则本门禁恒定命中。
#: ``dashboard.bundle.js`` 由 ``scripts/build_dashboard_bundle.py`` 拼接而成，
#: 内含各 ``dash-*.js`` 的逐字拷贝（含 ``dash-ops.js`` 的 ``startPolling``）——
#: 扫它等于**拿自己的拷贝判自己重复**。它是否同步由那个 builder 的
#: ``--check`` 单独负责（见 ``fx_bundle_sync``），与「唯一实现」无关。
GENERATED = {"dashboard.bundle.js"}

#: 函数名像轮询
#: ⚠️ 必须 ``IGNORECASE`` —— ``startPolling`` 是大写 P，
#: 大小写敏感会把这一处**漏报成「无重复」**（第一版就如此）。
POLL_NAME = re.compile(r"^(\s*)(async\s+)?function\s+(\w*(poll|watch|retry)\w*)\s*\(", re.M | re.I)


def main() -> int:
    print("═" * 72)
    print("门禁⑦：**等任务**轮询的实现唯一性")
    print("═" * 72)
    offenders: list[str] = []
    allowed: list[str] = []
    for p in sorted(DASH.glob("*.js")):
        if p.name in ALLOW or p.name in GENERATED:
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
            if name in ALLOW_FUNCS:
                allowed.append(f"  dashboard/{p.name}:{line}  function {name}()")
                continue
            offenders.append(f"  dashboard/{p.name}:{line}  function {name}()")

    print("  唯一实现: dashboard/cpt_job.js（window.CPTJob.poll）")
    print("  加载顺序: index.html 第 2 个（紧随 url_safety.js）")
    print("  排除生成物: dashboard/dashboard.bundle.js（由 build_dashboard_bundle.py 拼接）")
    if allowed:
        print(f"\n  ℹ️ {len(allowed)} 处按函数名显式豁免（语义不同，非重复实现）：")
        for a in allowed:
            print(a)
        print("     理由见 ALLOW_FUNCS 的注释。")
    if offenders:
        print(f"\n  ⚠️ 另有 {len(offenders)} 处自己实现的轮询：")
        for o in offenders:
            print(o)
        if any("startPolling" in o for o in offenders):
            print("\n  ⚠️ startPolling 那处是「持续刷新」，**语义不同、故意不合并**；")
            print("     但它仍**阻断** —— 理由见文件头：要合并就合并，别让它一直挂着。")
        print("\n  ⇒ 门禁红。收敛办法二选一：并进 CPTJob.poll，或在本文件里")
        print("     写清理由后加进 ALLOW —— 后者是显式豁免，不是默认放行。")
        return 1
    print("\n  ✅ 无重复轮询实现")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
