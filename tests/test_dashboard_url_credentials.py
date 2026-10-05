"""看板取数 URL 的**行为**契约（R44）。**全程离线**，不碰网络。

## 这条测试要挡住什么

看板挂在 nginx ``auth_basic`` 后面。若用户以 ``https://user:pwd@host/cpt/``
这种形式打开，``new URL(endpoint, window.location.href)`` 会把 base 的
``user:password@`` **继承**进结果（URL 规范如此），而 ``fetch``/``Request``
**按规范拒绝**带凭据的 URL：

    Failed to execute 'fetch' on 'Window': Request cannot be constructed
    from a URL that includes credentials: /cpt/api/dashboard/snapshot

后果不是报错页，而是**整个看板静默退化成离线 demo**：K 线空白、盘口全空、
结构详情全是 ``—``，只有一条红色报错。R44 真机实测（NDORACLE）确认：
后端 ``/cpt/api/dashboard/snapshot`` 明明 200 + 真实 BTCUSDT K 线。

## 为什么用 node 跑真 JS，而不是 grep 源码

grep ``"username = \"\""`` 只能证明**代码里写了那句话**，证明不了
**运行时真的剥掉了凭据**。本文件把 ``safeFetchUrl``/``resolveUrl`` 的实现
抽出来用 node 真跑一遍 —— 判据是「``new Request(resolved)`` 不抛」，
这与浏览器 ``fetch`` 的规范行为一致（node 的 ``Request`` 同源于 undici，
用的就是 WHATWG fetch 标准）。

⚠️ 踩过的坑（R44）：第一版只改了 ``refreshSelectedSnapshot``，重截一张图
报错**一字未变** —— 因为首屏走的是 ``loadSnapshot(root.dataset.snapshotUrl)``，
那条路径压根不经过被改的函数。所以这里断言的是**所有 fetch 出口**，
而不只是某一个调用点。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import dashboard_js

ROOT = Path(__file__).resolve().parents[1]

#: 从 dashboard.js 里抽出真实实现（而不是复制一份）——
#: 复制的话，源码改了而测试里的副本没改，测试就成了摆设。
_HELPER = "resolveUrl"
_SAFE = "safeFetchUrl"

#: 所有会发请求的出口。少改一个，首屏就还是坏的。
_FETCH_CALLS = (
    "requestJson",
    "fetchInspect",
    "_fetchSnapshot",
    "loadHealth",
    "loadSources",
)


def _node() -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用，跳过 JS 行为契约")
    return node


def _helper_source() -> str:
    """从 dashboard.js 里原样抠出 resolveUrl / safeFetchUrl 两个函数体。"""
    js = dashboard_js()
    out: list[str] = []
    for name in (_HELPER, _SAFE):
        marker = f"function {name}("
        start = js.find(marker)
        assert start >= 0, f"dashboard.js 里找不到 {name}() —— R44 的修复被回退了？"
        depth = 0
        i = js.find("{", start)
        assert i >= 0
        for j in range(i, len(js)):
            if js[j] == "{":
                depth += 1
            elif js[j] == "}":
                depth -= 1
                if depth == 0:
                    out.append(js[start : j + 1])
                    break
        else:  # pragma: no cover - 源码被截断
            raise AssertionError(f"{name}() 大括号不配对")
    return "\n\n".join(out)


def _url_safety_source() -> str:
    """``dashboard/url_safety.js`` 的**真源码**（原样，不抠函数体）。

    ⚠️ R50：这条是**修一个假阴性的关键**。R45 把凭据消毒收敛成唯一实现
    ``url_safety.js`` 之后，``resolveUrl`` / ``safeFetchUrl`` 都变成了
    **薄委托**：

        const shared = window.CPT_URL;
        if (shared && typeof shared.urlObject === "function") { ... }
        const url = new URL(endpoint, window.location.href);   // ← 兜底，无 try

    而 ``try/catch``（"解析不了就返回 null"）**只存在于 url_safety.js 里**。
    沙箱若只抠 dashboard.js 那两个函数体，``window.CPT_URL`` 恒为 undefined ⇒
    永远走**兜底分支** ⇒ ``new URL("http://", base)`` 直接抛 ``ERR_INVALID_URL``。

    结果就是 ``test_safe_fetch_url_keeps_endpoint_when_unresolvable`` 在
    node v22 上红 —— 但它红的**原因不是产品代码坏了**（浏览器里 url_safety.js
    先加载，走的是 shared.safe，返回原值），而是**沙箱没搭成页面真正的样子**。
    这类「测试失真」比测试直接失败危险：它会让人去改本来正确的业务代码。

    ⇒ 沙箱必须按 ``index.html`` 的真实顺序把 url_safety.js 放在最前面
    （``tests/test_url_safety_loads_before_every_consumer`` 守的就是那个顺序）。
    """
    path = ROOT / "dashboard" / "url_safety.js"
    assert path.is_file(), "dashboard/url_safety.js 不存在 —— R45 的唯一实现被删了？"
    return path.read_text(encoding="utf-8")


def _run(body: str) -> dict:
    """把 helper 注入 node 沙箱并执行 ``body``（body 需 return 一个对象）。

    三个踩过的坑，都很隐蔽：

    1. ``body`` 必须包在 IIFE 里 —— ``node -e`` 的顶层不允许 ``return``
       （SyntaxError: Illegal return statement）。
    2. 必须用 ``console.log`` 打印，**不能**只写一句裸表达式
       ``JSON.stringify(...)``。本沙箱里的 node 构建会把「顶层表达式语句」
       的结果直接丢掉（rc=0 但 stdout 为空），于是测试会以
       "list index out of range" 这种与被测代码毫无关系的报错失败。
    3. R50：**必须先加载 url_safety.js**（见 :func:`_url_safety_source`）。
       只抠 dashboard.js 的两个函数体 ⇒ ``window.CPT_URL`` 是 undefined ⇒
       测的是 R45 之后**再也不会在浏览器里执行**的兜底分支。
    """
    script = f"""
const window = {{ location: {{ href: "about:blank" }} }};
{_url_safety_source()}
{_helper_source()}

console.log(JSON.stringify((function () {{
{body}
}})()));
"""
    proc = subprocess.run([_node(), "-e", script], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr[:800]}"
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    assert lines, f"node 没有输出（rc=0）。脚本:\n{script[:600]}"
    return json.loads(lines[-1])


def test_credentials_in_location_are_stripped() -> None:
    """核心回归：页面 URL 带凭据时，解析结果必须**不含**凭据。"""
    got = _run(
        """
        window.location.href = "https://admin:ndjack@140.83.62.161/cpt/";
        return { url: resolveUrl("/cpt/api/dashboard/snapshot").toString() };
        """
    )
    assert got["url"] == "https://140.83.62.161/cpt/api/dashboard/snapshot"
    assert "ndjack" not in got["url"]
    assert "admin" not in got["url"]


def test_helper_source_actually_clears_both_fields() -> None:
    """无 node 环境（Oracle 上就没装）时的**兜底**行为断言。

    只靠上面的 node 用例是不够的：CI/Oracle 可能没有 node，而这条 bug
    恰恰是「静默退化成离线」—— 最需要它在任何环境都被挡住。
    所以源码层面也钉一道：清空动作必须同时覆盖 username 与 password。

    ⚠️ R50：判据必须是**唯一实现** ``url_safety.js``，不能只看 dashboard.js
    的兜底分支。R45 收敛之后，dashboard.js 里那两行已经是「shared 缺席时」
    的死路径 —— 断言它只能证明死代码没被改，证明不了产品行为。
    """
    body = _url_safety_source()
    assert 'url.username = ""' in body, "url_safety.js 没有清空 username"
    assert 'url.password = ""' in body, "url_safety.js 没有清空 password"


def test_sandbox_actually_loads_url_safety() -> None:
    """**沙箱失真守门**（R50）。

    :func:`_run` 若哪天漏了 url_safety.js，上面所有 node 用例**仍然会绿** ——
    只是测的不再是浏览器里真正跑的那条路径。这就是「测试静默失效」：
    比红更危险，因为红会被人看见。

    ⇒ 直接断言沙箱里 ``window.CPT_URL`` 真的被装上了，且两个方法都在。
    """
    got = _run(
        """
        return {
          hasShared: typeof window.CPT_URL === "object" && window.CPT_URL !== null,
          hasSafe: typeof (window.CPT_URL || {}).safe === "function",
          hasUrlObject: typeof (window.CPT_URL || {}).urlObject === "function",
        };
        """
    )
    assert got["hasShared"], "沙箱没加载 url_safety.js —— node 用例已失真（见 _url_safety_source）"
    assert got["hasSafe"] and got["hasUrlObject"], (
        "window.CPT_URL 缺 safe/urlObject，url_safety.js 被改坏了"
    )


def test_dashboard_js_thin_delegate_still_has_fallback() -> None:
    """dashboard.js 的兜底分支**不能被删**。

    R45 之后它确实是浏览器里走不到的路径，但它是 url_safety.js 缺席时
    （加载顺序被改坏、脚本 404）唯一的防线。删掉它等于把一次「加载顺序错」
    升级成「白屏 + 抛异常」。
    """
    body = _helper_source()
    assert "window.CPT_URL" in body, "dashboard.js 的 helper 不再委托唯一实现（R45 收敛被回退？）"
    assert 'url.username = ""' in body and 'url.password = ""' in body, (
        "dashboard.js 兜底分支丢了凭据清空 —— url_safety.js 缺席时会漏凭据"
    )


def test_resolved_url_is_actually_fetchable() -> None:
    """判据用「``new Request()`` 不抛」—— 与浏览器 fetch 同一套规范。

    只断言字符串里没有凭据是不够的：``URL`` 的 ``toString()`` 与 ``fetch``
    的校验不是同一件事，必须让 ``Request`` 真的构造一次。
    """
    got = _run(
        """
        window.location.href = "https://admin:ndjack@140.83.62.161/cpt/";
        const u = resolveUrl("/cpt/api/dashboard/snapshot");
        u.searchParams.set("symbol", "BTCUSDT");
        let fetchable = true, err = "";
        try { new Request(u.toString()); }
        catch (e) { fetchable = false; err = e.message; }
        return { url: u.toString(), fetchable, err };
        """
    )
    assert got["fetchable"] is True, f"解析结果仍无法 fetch：{got['err']}"
    assert got["url"].endswith("/cpt/api/dashboard/snapshot?symbol=BTCUSDT")


def test_no_credentials_leaves_behaviour_unchanged() -> None:
    """不带凭据时必须**逐字节**不变 —— 这个 helper 不是为了改变正常路径。"""
    got = _run(
        """
        window.location.href = "https://140.83.62.161/cpt/";
        const u = resolveUrl("/cpt/api/dashboard/snapshot");
        u.searchParams.set("symbol", "BTCUSDT");
        return { url: u.toString() };
        """
    )
    assert got["url"] == "https://140.83.62.161/cpt/api/dashboard/snapshot?symbol=BTCUSDT"


def test_safe_fetch_url_keeps_endpoint_when_unresolvable() -> None:
    """真的解析不了时，``safeFetchUrl`` 必须**原样返回**，不要抛新异常。

    仓里 dashboard 支持 ``file://`` + 内联 JSON 的离线用法
    （见 dashboard.js 里 ``?demo=off`` 那段）。抛异常会把「离线 demo」
    变成「白屏」。

    ⚠️ 实测纠正过一个想当然的假设：``file://`` base + 绝对路径
    （``/cpt/api/...``）**并不会**让 ``new URL`` 抛异常，它会正常解析成
    ``file:///cpt/api/...``。所以这里用一个**真正**解析不了的东西来触发
    兜底分支（``https://`` 之外的畸形协议），而不是拿 file:// 当例子 ——
    那个例子压根走不到 catch，写进去等于没测。
    """
    got = _run(
        """
        window.location.href = "https://h/cpt/";
        return { url: safeFetchUrl("http://") };
        """
    )
    assert got["url"] == "http://", "解析失败时应原样返回 endpoint"


def test_file_url_base_still_resolves_to_file_scheme() -> None:
    """``file://`` 离线路径的**真实**行为：解析成功，且保持 file 协议。

    这条是上一条的对照 —— 说明 catch 兜底只在真正畸形时才生效，
    离线审计的正常路径不会被它吞掉。
    """
    got = _run(
        """
        window.location.href = "file:///tmp/audit/index.html";
        return { url: safeFetchUrl("/cpt/api/dashboard/snapshot") };
        """
    )
    assert got["url"] == "file:///cpt/api/dashboard/snapshot"


def test_safe_fetch_url_strips_credentials_too() -> None:
    got = _run(
        """
        window.location.href = "https://admin:ndjack@140.83.62.161/cpt/";
        return { url: safeFetchUrl("/cpt/api/dashboard/sources") };
        """
    )
    assert got["url"] == "https://140.83.62.161/cpt/api/dashboard/sources"


def test_every_fetch_exit_goes_through_safe_fetch_url() -> None:
    """**所有** fetch 出口都必须过 ``safeFetchUrl``。

    R44 踩过：只改了 ``refreshSelectedSnapshot``，首屏照样报错 ——
    因为首屏走 ``loadSnapshot(root.dataset.snapshotUrl)``，那条路径不经过它。
    这个断言就是为了防止「下次又只改一处」。
    """
    js = _code_without_comments(dashboard_js())
    bare = []
    for line_no, line in enumerate(js.splitlines(), 1):
        if "await fetch(" not in line:
            continue
        # ⚠️ 也要容忍 ``safeUrl``：它是 ``market_a_share.js`` 里的**委托包装**
        # （``(window.CPT_URL.safe || 恒等)(target)``），同样是消毒出口。
        #
        # 拆分前本测试只读 ``dashboard.js``，而那个文件里**只有** safeFetchUrl；
        # 改成读**全部**模块后，market_a_share 的 safeUrl 第一次进入视野 ——
        # 于是测试**无改动却变严了**。
        # 姊妹测试 ``test_every_fetch_exit_goes_through_safe_url`` 本来就容忍它
        # ⇒ 两个姊妹测试判据不一致，**这里对齐**。
        if "safeFetchUrl(" in line or "safeUrl(" in line:
            continue
        bare.append(f"  <看板 JS 拼接>:{line_no}: {line.strip()[:90]}")
    assert not bare, "有 fetch 出口没走凭据消毒（凭据会泄漏进去）：\n" + "\n".join(bare)


def _code_without_comments(js: str) -> str:
    """剥掉注释与字符串字面量，只留**可执行代码**。

    踩过：断言 ``"new URL(window.location.href)" not in js`` 时，
    R44 自己写的那段**解释性注释**里恰好包含这串字面量，于是测试红了 ——
    而代码其实早就改对了。断言必须只看代码，否则「把修复说明写进注释」
    就会让测试失败。
    """
    import re

    out: list[str] = []
    i, n = 0, len(js)
    while i < n:
        two = js[i : i + 2]
        if two == "//":
            j = js.find("\n", i)
            i = n if j < 0 else j
        elif two == "/*":
            j = js.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif js[i] in "\"'`":
            quote = js[i]
            i += 1
            while i < n and js[i] != quote:
                i += 2 if js[i] == "\\" else 1
            i += 1
            out.append('""')
        else:
            out.append(js[i])
            i += 1
    # 注释里可能残留 /* */ 之外的裸文本，再扫一遍行内残留
    return re.sub(r"//.*", "", "".join(out))


def test_no_unguarded_location_url_survives_for_state_writes() -> None:
    """``replaceState`` 用的 URL 必须**已经剥掉凭据**。

    它们不报错，但会把 ``user:pwd@`` **写回地址栏** —— 既是安全问题，
    也会让用户复制链接时把凭据带出去。

    判据不能是「文件里没有 ``new URL(window.location.href)``」——
    :func:`resolveUrl` / :func:`safeLocationUrl` **自己**必须用它当 base
    （那是唯一能拿到当前 origin 的地方）。所以这里找的是**裸露**用法：
    赋值给变量后**没有**紧跟凭据清空的那一处。
    """
    # ⚠️ 拆分后 ``resolveUrl`` 会住在某个 ``dash-*.js`` 里，不再是固定文件 ——
    # 所以这里**遍历全部模块**，而不是按 (文件, 函数) 配死。
    # （第一版硬编码 dashboard.js，拆分后会「找不到」⇒ 测试变成静默通过。）
    for fn in ("resolveUrl", "safeLocationUrl"):
        js = _code_without_comments(dashboard_js())
        start = js.find(f"function {fn}(")
        assert start >= 0, f"全部看板模块里缺少 {fn}()"
        rel = "<全部看板模块>"
        depth, i = 0, js.find("{", start)
        for j in range(i, len(js)):
            if js[j] == "{":
                depth += 1
            elif js[j] == "}":
                depth -= 1
                if depth == 0:
                    helper_body = js[start : j + 1]
                    break

        # 把「合法的那个」挖掉，剩下的就是裸露用法
        rest = js.replace(helper_body, "")
        for line_no, line in enumerate(rest.splitlines(), 1):
            if "new URL(window.location.href)" in line:
                raise AssertionError(
                    f"{rel}:{line_no} 仍有未剥凭据的 location 用法：{line.strip()[:80]}"
                )


def test_ashare_module_delegates_credential_stripping() -> None:
    """A 股模块**委托** ``url_safety.js``，不再自带一份实现。

    R45 更正：这条断言原来要求「自带一份」（``assert 'url.username = ""'``），
    锁住的恰恰是要消灭的坏模式 —— 副本会与唯一实现漂移。
    """
    js = _code_without_comments((ROOT / "dashboard/market_a_share.js").read_text(encoding="utf-8"))
    assert "safeLocationUrl" in js, "replaceState 仍需要 URL 对象版本"
    body = js[js.find("function safeLocationUrl(") :]
    body = body[: body.find("\n    }") + 6]
    assert "CPT_URL.urlObject" in body, "safeLocationUrl 没有委托唯一实现"
    assert 'url.username = ""' not in body, "又留了一份本地实现 —— 会与唯一实现漂移"


def _strip_js_comments(text: str) -> str:
    """剥掉 // 与 /* */ 注释，避免注释里的代码片段干扰计数。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def test_every_fetch_exit_goes_through_safe_url() -> None:
    """**所有** fetch 出口都必须过 ``safeFetchUrl`` / ``safeUrl``。

    R45 补的回归：首次修复只覆盖了 ``dashboard.js`` 里的 5 个出口，
    漏了 4 个 —— ``canvas_d``（画布 D）、``inspection_panel``（巡检面板）、
    ``market_a_share`` ×2（热门池 / 自选增删），以及 ``dashboard.js`` 自己的
    LLM explain POST。实测在带凭据的页面上这 4 个会各自抛
    "Request cannot be constructed from a URL that includes credentials"。
    """
    offenders: list[str] = []
    # ⚠️ 名单按「**真的发请求**」列，不按文件名列。
    #   dash-chart.js / dash-alert.js **一次 fetch 都没有**（纯 SVG 绘制 /
    #   Notification API），要求它们「委托 window.CPT_URL」本身是错的判据。
    for name in (
        "canvas_d.js",
        "inspection_panel.js",
        "market_a_share.js",
        "dashboard.bundle.js",
        "dash-core.js",
        "dash-ops.js",
        "dash-signal.js",
        "dash-chrome.js",
        "dash-structure.js",
    ):
        text = (ROOT / "dashboard" / name).read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            if "fetch(" not in line or line.lstrip().startswith(("*", "//", ".")):
                continue
            if "safeFetchUrl" in line or "safeUrl" in line:
                continue
            offenders.append(f"  dashboard/{name}:{line_no}: {line.strip()[:80]}")
    assert not offenders, "有 fetch 出口绕过了凭据消毒：\n" + "\n".join(offenders)


def test_credential_stripping_has_a_single_implementation() -> None:
    """凭据消毒必须**只有一份实现**，且它加载在所有使用者之前。

    R45 踩过的坑：第一次修复只覆盖了 ``dashboard.js`` 的 5 个出口、漏了 4 个；
    若当时把同样逻辑复制进另外三个文件，就埋下「改了主副本、副本静默漂移」的地雷
    —— 而「多份实现漂移」正是本仓反复吃的那类亏。

    所以：``url_safety.js`` 是唯一实现，其余文件一律**委托**。
    """
    shared = (ROOT / "dashboard" / "url_safety.js").read_text(encoding="utf-8")
    assert 'url.username = ""' in shared and 'url.password = ""' in shared, (
        "url_safety.js 必须自己清空 username 与 password"
    )
    # ⚠️ R45 拆分后**不能再按文件名列表**遍历 —— dashboard.js 已经不存在，
    # 而拆出来的 7 个 dash-*.js 是**函数分布**、不是「谁该有第二份实现」。
    # ⇒ 统一走 dashboard_js()（它按加载顺序拼**全部**看板 JS）。
    # ⚠️ 名单按「**真的发请求**」列。``dash-chart.js``（纯 SVG 绘制）、
    #    ``dash-alert.js``（Notification API）**一次 fetch 都没有**，
    #    要求它们「委托 window.CPT_URL」本身是错的判据。
    for name in (
        "canvas_d.js",
        "inspection_panel.js",
        "market_a_share.js",
        "dashboard.bundle.js",
        "dash-core.js",
        "dash-ops.js",
        "dash-signal.js",
        "dash-chrome.js",
        "dash-structure.js",
    ):
        text = (ROOT / "dashboard" / name).read_text(encoding="utf-8")
        if name == "url_safety.js":
            continue
        # 允许「委托前的本地兜底」出现，但不允许独立的第二份实现
        clean = _strip_js_comments(text)
        n = clean.count('url.username = ""')
        # ⚠️ R45 拆分后规则要跟着调整：
        # 旧 `dashboard.js` 允许**至多 1 处**「委托前的本地兜底」，
        # 拆出来的 dash-*.js 与 bundle **是同一份代码**，所以同一条规则适用。
        # 若还按「其余文件必须 0」判，就会把**唯一那份合法兜底**判成「第二份实现」。
        if name.startswith("dash-") or name == "dashboard.bundle.js":
            assert n <= 1, (
                f"dashboard/{name} 有 {n} 份凭据清空实现 —— "
                f"本仓只允许 1 处本地兜底，多出来就是副本漂移"
            )
        else:
            assert n == 0, (
                f"dashboard/{name} 自己实现了一遍凭据清空 —— 必须委托 url_safety.js，"
                f"否则副本会与唯一实现漂移"
            )
        # ⚠️ 「字面出现 CPT_URL」这条判据只在**定义方**成立。
        # 拆分前所有代码在一个文件里，所以「每个文件都该出现 CPT_URL」看着合理；
        # 拆分后：``window.CPT_URL`` 只出现在**定义 resolveUrl/safeFetchUrl 的那个模块**
        # （dash-core.js / bundle），其余模块调的是那个模块的**本地包装** ``safeFetchUrl``。
        # ⇒ 判据改成「**定义方**必须真的委托 window.CPT_URL」。
        if name in ("dash-core.js", "dashboard.bundle.js"):
            assert "CPT_URL" in clean, (
                f"dashboard/{name} 定义了 safeFetchUrl/resolveUrl，却没有真的委托 window.CPT_URL"
            )


def test_url_safety_loads_before_every_consumer() -> None:
    """``url_safety.js`` 必须在所有使用它的 ``<script defer>`` **之前**。

    ``defer`` 脚本按文档顺序执行，顺序错了使用者就拿不到 ``CPT_URL``，
    会静默退回 ``(t) => t``（即不做消毒）—— 那正是 R45 修的那个 bug。
    """
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    # ⚠️ 正则原为 ``[a-z_]+`` ⇒ **匹配不到** ``dash-core.js``（带连字符），
    # 拆分后会静默漏掉新模块。⇒ 加上 ``-``。
    order = re.findall(r'<script src="\./([a-z_-]+\.js)" defer', html)
    assert "url_safety.js" in order, "index.html 没有加载 url_safety.js"
    first_use = min(
        (
            i
            for i, n in enumerate(order)
            if n
            in ("canvas_d.js", "inspection_panel.js", "market_a_share.js", "dashboard.bundle.js")
        ),
        default=None,
    )
    assert order.index("url_safety.js") < first_use, (
        f"url_safety.js 必须先于使用者加载；当前顺序: {order}"
    )
