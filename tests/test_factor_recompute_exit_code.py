"""因子重算的**退出码**契约（R44）。**全程离线**：不碰 DB、不联网。

## 为什么退出码值得单独立一组测试

2026-10-02 真机日志：

    ===== 2026-10-02T13:47:45Z 开始（源=eastmoney scope=placeholder）=====
    ===== 2026-10-02T13:50:15Z 结束 rc=0 =====

**2 分半钟，rc=0** —— 一次「启动就撞上 EastmoneyActionError、整轮死掉」的任务，
被 cron 记成了一次**成功**。rc=0 意味着任何外部监控（cron 邮件、CI、看门狗）
都看不出它死了。

这比崩溃更危险：崩溃至少有 stacktrace 和非零码，而这里日志两行都是「正常」的。
§4 的 bug 之所以能连续存在，就是被这个 rc=0 掩护的。

所以契约是：**本轮被打断 ⇒ 退出码非 0**。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = (ROOT / "scripts/factor_recompute.py").read_text(encoding="utf-8")
CRON_SH = (ROOT / "deploy/cron/factor-recompute-daily.sh").read_text(encoding="utf-8")


def _main_body() -> str:
    """抠出 ``main()`` 的函数体。

    ⚠️ 不能只按大括号配平来切：``main()`` 里有一堆 ``state = {...}`` 这种
    **字典字面量**，会在中途就把 depth 归零，于是只截到函数的前半截
    （踩过：body 里既没有 ``stopped_reason`` 也没有 ``return 3``，看起来
    像「代码没生效」，其实是提取器写错了）。

    正确做法：从 ``def main(`` 往后找**行首缩进 4 空格**的 ``return``，
    那是函数体的收尾；再回溯到第一条 ``async``/``def`` 之后。
    """
    start = MAIN_PY.index("def main(")
    lines = MAIN_PY[start:].splitlines()
    end = next(
        (i for i, ln in enumerate(lines) if re.match(r"^    return \d", ln)),
        None,
    )
    assert end is not None, "main() 里没找到行首 return，函数体提取失败"
    return "\n".join(lines[: end + 1])


def test_interrupted_run_returns_nonzero() -> None:
    """核心契约：``stopped_reason`` 存在时必须 ``return 3``。"""
    body = _main_body()
    assert "stopped_reason" in body, "main() 里没有 stopped_reason 局部标志"
    # return 3 必须**在** stopped_reason 判定之后
    m = re.search(r"if stopped_reason:.*?return (\d+)", body, re.S)
    assert m, "没有「if stopped_reason: ... return N」这条分支"
    assert m.group(1) != "0", "被打断时仍然返回 0 —— 就是 2026-10-02 那个 bug"


def test_uninterrupted_run_returns_zero() -> None:
    """反过来也要钉住：正常跑完必须是 0，否则 cron 会天天误报。"""
    body = _main_body()
    tail = body[body.rindex("return 0") :]
    assert len(tail) < 200, "函数末尾的 return 0 之前还有大量代码，可能不是收尾分支"


def test_stale_stopped_marker_is_cleared() -> None:
    """本轮跑完要清掉上一轮遗留的 ``stopped``，否则它永远挂在进度文件里。

    不清的后果：bug 修好、任务也恢复正常跑之后，``--report`` 仍然打印
    「上次因 X 提前停止」—— 一个早就修好的问题看起来还在。
    """
    body = _main_body()
    assert 'state.pop("stopped"' in body, "跑完没有清除 stale 的 stopped 标记"
    assert "save_state(state)" in body[body.index('state.pop("stopped"') :][:300], (
        "清掉 stopped 后必须落盘，否则下次还是旧的"
    )


def test_stopped_reason_is_per_run_not_persisted() -> None:
    """``stopped_reason`` 必须是**局部**变量，不能读 ``state["stopped"]``。

    进度文件里的 ``stopped`` 会跨轮次存活：上一轮死了、这一轮正常跑完，
    若拿它当判据，这一轮也会被误报成「失败」。这正是加局部标志的原因。
    """
    body = _main_body()
    assert "stopped_reason: str | None = None" in body
    # 判定处不能直接用 state 里的 stopped
    assert not re.search(r"if state\.get\(\"stopped\"\):\s*\n\s*print", body), (
        "仍在用 state['stopped'] 做退出码判据 —— 跨轮次会误报"
    )


def test_cron_script_flags_rc3() -> None:
    """cron 脚本要把 rc=3 写进日志，让「死过」可 grep。"""
    assert "-eq 3" in CRON_SH, "cron 脚本没有识别 rc=3"
    assert "本轮未完成" in CRON_SH, "cron 脚本没有可 grep 的失败标记"
    assert "exit $rc" in CRON_SH, "cron 脚本必须把退出码传出去，否则修了也没用"


def test_cron_script_propagates_exit_code() -> None:
    """``rc=$?`` 必须**紧跟**在 python 调用之后，中间不能插别的东西。

    ``timeout ... python ... \\`` 是**续行**，命令真正结束在下一个不以 ``\\``
    结尾的那行 —— 找错行就会误判（踩过：按 ``\\`` 找，拿到的是续行本身）。
    """
    lines = CRON_SH.splitlines()
    start = next(
        i for i, ln in enumerate(lines) if "scripts/factor_recompute.py" in ln and "timeout" in ln
    )
    end = next(i for i in range(start, len(lines)) if not lines[i].rstrip().endswith("\\"))
    assert "rc=$?" in lines[end + 1], (
        f"rc 捕获被隔开了：命令在第 {end + 1} 行结束，下一行是 {lines[end + 1]!r}"
    )
