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
import shutil
import subprocess
from pathlib import Path

import pytest

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
    js = (ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8")
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


def _run(body: str) -> dict:
    """把 helper 注入 node 沙箱并执行 ``body``（body 需 return 一个对象）。

    两个踩过的坑，都很隐蔽：

    1. ``body`` 必须包在 IIFE 里 —— ``node -e`` 的顶层不允许 ``return``
       （SyntaxError: Illegal return statement）。
    2. 必须用 ``console.log`` 打印，**不能**只写一句裸表达式
       ``JSON.stringify(...)``。本沙箱里的 node 构建会把「顶层表达式语句」
       的结果直接丢掉（rc=0 但 stdout 为空），于是测试会以
       "list index out of range" 这种与被测代码毫无关系的报错失败。
    """
    script = f"""
const window = {{ location: {{ href: "about:blank" }} }};
{_helper_source()}

console.log(JSON.stringify((function () {{
{body}
}})()));
"""
    proc = subprocess.run(
        [_node(), "-e", script], capture_output=True, text=True, timeout=60
    )
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
    """
    body = _helper_source()
    assert 'url.username = ""' in body, "resolveUrl 没有清空 username"
    assert 'url.password = ""' in body, "resolveUrl 没有清空 password"


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
    js = _code_without_comments((ROOT / "dashboard/dashboard.js").read_text(encoding="utf-8"))
    bare = []
    for line_no, line in enumerate(js.splitlines(), 1):
        if "await fetch(" not in line:
            continue
        if "safeFetchUrl(" in line:
            continue
        bare.append(f"  dashboard.js:{line_no}: {line.strip()[:90]}")
    assert not bare, "有 fetch 出口没走 safeFetchUrl（凭据会泄漏进去）：\n" + "\n".join(bare)


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
    for rel, fn in (
        ("dashboard/dashboard.js", "resolveUrl"),
        ("dashboard/market_a_share.js", "safeLocationUrl"),
    ):
        js = _code_without_comments((ROOT / rel).read_text(encoding="utf-8"))
        start = js.find(f"function {fn}(")
        assert start >= 0, f"{rel} 缺少 {fn}()"
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


def test_ashare_module_has_its_own_credential_stripper() -> None:
    """A 股模块是独立文件，拿不到 dashboard.js 的内部函数 —— 自带一份。

    这里断言它**真的清空**了 username/password，而不是只判断存在。
    """
    js = _code_without_comments((ROOT / "dashboard/market_a_share.js").read_text(encoding="utf-8"))
    assert "safeLocationUrl" in js
    body = js[js.find("function safeLocationUrl(") :]
    body = body[: body.find("\n    }") + 6]
    assert 'url.username = ""' in body
    assert 'url.password = ""' in body


def test_every_fetch_exit_goes_through_safe_url() -> None:
    """**所有** fetch 出口都必须过 ``safeFetchUrl`` / ``safeUrl``。

    R45 补的回归：首次修复只覆盖了 ``dashboard.js`` 里的 5 个出口，
    漏了 4 个 —— ``canvas_d``（画布 D）、``inspection_panel``（巡检面板）、
    ``market_a_share`` ×2（热门池 / 自选增删），以及 ``dashboard.js`` 自己的
    LLM explain POST。实测在带凭据的页面上这 4 个会各自抛
    "Request cannot be constructed from a URL that includes credentials"。
    """
    import re

    offenders: list[str] = []
    for name in ("dashboard.js", "canvas_d.js", "inspection_panel.js", "market_a_share.js"):
        text = (ROOT / "dashboard" / name).read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            if "fetch(" not in line or line.lstrip().startswith(("*", "//", ".")):
                continue
            if "safeFetchUrl" in line or "safeUrl" in line:
                continue
            offenders.append(f"  dashboard/{name}:{line_no}: {line.strip()[:80]}")
    assert not offenders, "有 fetch 出口绕过了凭据消毒：\n" + "\n".join(offenders)


def test_other_files_can_reach_the_shared_helper() -> None:
    """另外三个文件必须能拿到 ``safeFetchUrl``，且拿不到时有自算的兜底。

    它们是独立的 ``<script defer>``，执行顺序不保证在 ``dashboard.js`` 之后，
    所以不能假设 ``window.CPTDashboard`` 一定就绪。
    """
    for name in ("canvas_d.js", "inspection_panel.js", "market_a_share.js"):
        text = (ROOT / "dashboard" / name).read_text(encoding="utf-8")
        assert "CPTDashboard && window.CPTDashboard.safeFetchUrl" in text, (
            f"dashboard/{name} 没有尝试复用 dashboard.js 的 safeFetchUrl"
        )
        assert 'url.username = ""' in text, f"dashboard/{name} 缺少自算兜底"
    dash = (ROOT / "dashboard" / "dashboard.js").read_text(encoding="utf-8")
    assert "safeFetchUrl," in dash, "safeFetchUrl 没有暴露到 window.CPTDashboard"
