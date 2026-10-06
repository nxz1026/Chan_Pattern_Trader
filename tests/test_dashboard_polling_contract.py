"""看板轮询契约测试（2026-10-06，③「数据陈旧」误报回归）。

## 钉住的 bug

页面首屏加载成功后开始每 5 秒轮询，但**每轮都命中 3 秒 TTL 的快照缓存并提前
return**，因此永远到不了重新布防的代码；15 秒后 ``state-stale`` 到点，页面在
数据完全新鲜的情况下显示「数据陈旧」，顶栏「更新时间」也不再走。

## 为什么断言的是**结构**而不是行为

这个 bug 的本质是「**该走的路径没走到**」，纯逻辑上无法用单文件测试覆盖：
只要缓存分支里少一个调用，行为就退化成「一切照旧、只是偶尔误报」——
不崩、不报错、CI 全绿。门禁⑦（``check_job_poll_unique.py``）管的是
「轮询实现是否唯一」，与「命中缓存后是否仍重新布防」是两件事，不重叠。

所以这里钉住**唯一汇合点**：``markFresh()`` 必须是两条加载路径
（首屏无缓存 / 稳态命中缓存）的**共同出口**，且它必须同时做两件事——
启动轮询 + 重布 stale 定时器。任何把 ``startPolling`` 挪回单条路径的改动
都会在这里变红。

⚠️ 2026-10-06 记录一次**方法论教训**：定位这个问题时，我先加了一对
``data-probe-*`` 计数器来观测 tick，结果把计数和细节拼在同一个属性里
（``"1#pinned=false"``），下一次 ``Number()`` 读到 NaN → ``NaN || 0`` →
永远写回 1 —— **两个计数器恒显 1**，看起来像「轮询只跑一次就停摆」，
据此差点又去改已经正确的代码。计数器一旦带上非数字后缀就失去意义。
本文件因此只做源码结构断言，不再引入运行时探针。
"""

from __future__ import annotations

import re

from tests.conftest import dashboard_js

#: 去掉注释后的全部看板 JS —— 断言不落在注释文字上。
JS = dashboard_js(strip_comments=True)


def _function_body(source: str, name: str) -> str:
    """截出 ``function name(...) { ... }`` 的函数体（含外层大括号）。

    ⚠️ 不能直接取参数列表之后的第一个 ``{``：``loadSnapshot`` 的签名是
    ``(url, { force = false } = {})``，那个解构占位符也是 ``{``，按 naive
    匹配会截出 ``{ force = false }`` —— 断言全部落空却不报错。
    所以先用括号配平跳过整个参数列表，再找函数体的起始大括号。

    朴素的花括号计数对本文件涉及的这几个函数是安全的：模板字面量里的
    ``${...}`` 是配对的，函数体内没有含未配对花括号的正则字面量。
    """
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", source)
    assert m is not None, f"源码里找不到 function {name}("
    params = m.end() - 1  # 指向 '('
    depth = 0
    for i in range(params, len(source)):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                params = i
                break
    else:  # pragma: no cover - 参数列表不配平属于源码损坏
        raise AssertionError(f"function {name}( 的参数列表没有配平")
    start = source.index("{", params)
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"function {name}( 的花括号没有配平")


def _const_ms(source: str, name: str) -> int:
    """求值 ``const NAME = <算术表达式>;``。

    ``STALE_AFTER_MS`` 写成 ``POLL_INTERVAL_MS * 3``，直接求值而不是只匹配
    字面量，这样「谁比谁大」由真实表达式决定，不受写法变化影响。
    """
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*([^;]+);", source)
    assert m is not None, f"源码里找不到 const {name} ="
    expr = m.group(1).strip()
    if name != "POLL_INTERVAL_MS":
        # 只在解析「别的常量」时展开 POLL_INTERVAL_MS 自身，否则会自递归到爆栈。
        expr = re.sub(r"\bPOLL_INTERVAL_MS\b", str(_const_ms(source, "POLL_INTERVAL_MS")), expr)
    assert re.fullmatch(r"[\d\s*+\-()]+", expr), f"{name} 的表达式含非算术内容：{expr!r}"
    return int(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 - 上面已限定为纯算术


def test_poll_interval_is_stricter_than_stale_threshold() -> None:
    """轮询必须**快于**陈旧阈值，否则「持续轮询」在数学上就不可能防住误报。

    这是整个 bug 的算术前提：``STALE_AFTER_MS`` 是一次「多久没拿到新鲜数据
    才算陈旧」的宽限，轮询周期必须显著小于它，宽限才有意义。
    """
    poll = _const_ms(JS, "POLL_INTERVAL_MS")
    stale = _const_ms(JS, "STALE_AFTER_MS")
    assert poll < stale, (
        f"轮询周期 {poll}ms 不小于陈旧阈值 {stale}ms —— "
        f"页面必然在两次刷新之间误报「数据陈旧」"
    )


def test_markfresh_starts_polling() -> None:
    """轮询必须由 ``markFresh`` 启动。

    2026-10-06 的根因：``startPolling`` 原本只挂在「首屏无缓存」那条路径上，
    而首屏之后每轮轮询都命中缓存提前 return —— 够不到启动代码。把它收进
    ``markFresh``（两条路径的唯一汇合点）才不会再漏。
    """
    assert "startPolling(" in _function_body(JS, "markFresh")


def test_markfresh_rearms_the_stale_timer() -> None:
    """``markFresh`` 必须真的重置计时，而不只是改文案。

    只 ``setHidden(state-stale, true)`` 是不够的：那会让陈旧横幅消失，
    但计时器仍在跑，下一次到点照样弹出来 —— 表现就是「闪一下又冒出来」。
    """
    body = _function_body(JS, "markFresh")
    assert "clearTimeout(state.staleTimer)" in body, (
        "markFresh 没有清掉上一轮的 stale 计时器"
    )
    assert "state.staleTimer = window.setTimeout(" in body, (
        "markFresh 没有重新布防 stale 计时器"
    )


def test_cache_hit_path_still_marks_fresh() -> None:
    """命中缓存的稳态路径也必须调用 ``markFresh``。

    这是本次修复的**关键一行**：缓存分支提前 ``return cached``，如果它只
    同步渲染一下就返回，stale 定时器永远不会被重置 —— 页面会在数据完全
    新鲜时谎报「数据陈旧」。

    ⚠️ 断言范围必须切在**缓存分支自己**的边界内（``if (cached)`` →
    ``return cached``）。第一版写成「从 if (cached) 往后的全部正文」，
    结果把后面无缓存路径的 markFresh 也算进来了 —— 把缓存分支那行删掉
    测试**依然全绿**。这正是本仓库文档里反复强调的「测试静默失效比测试
    直接失败危险得多」：它不会报错，只是**测得少了**。
    """
    body = _function_body(JS, "loadSnapshot")
    start = body.index("if (cached)")
    end = body.index("return cached", start)
    branch = body[start:end]
    assert "markFresh(" in branch, (
        "loadSnapshot 的缓存分支没有调用 markFresh —— "
        "稳态轮询不会重布防，15 秒后必然误报「数据陈旧」"
    )


def test_poll_interval_is_the_only_unbounded_loop() -> None:
    """轮询定时器只能在 ``startPolling`` 里创建。

    防止「为了修这个 bug 又顺手在别处加一个 setInterval」，导致两套刷新
    互相踩、问题从「不刷新」变成「刷得太快」。
    """
    armed = [
        name
        for name in ("startPolling", "loadSnapshot", "markFresh", "boot")
        if "setInterval" in _function_body(JS, name)
    ]
    assert armed == ["startPolling"], f"以下函数里出现了 setInterval：{armed}"


def test_background_refresh_failure_is_visible() -> None:
    """后台刷新失败必须落到可见位置，不能静默吞掉。

    原来是 ``.catch(() => undefined)``：真实异常被整个吞掉，页面表现为
    「只是没更新」而不是「报错」，排查成本极高。
    """
    body = _function_body(JS, "loadSnapshot")
    catch = body[body.rindex(".catch(") :]
    assert "() => undefined" not in catch, "后台刷新仍然被静默 catch 吞掉"
    assert "showError" in catch or "setConnection" in catch, (
        "后台刷新的 catch 没有把失败写到状态区 —— 故障会伪装成「没动静」"
    )
