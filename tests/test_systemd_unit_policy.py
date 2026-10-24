"""unit 文件的重启策略不许退回（R84）。

## 这条测试存在的原因

2026-10-19 03:05 `cpt-dashboard.service` 被停，之后 **55 小时**没人知道。
根因不是「restart 没写」，而是写的是 `Restart=on-failure` —— 它**不覆盖
SIGTERM 正常退出**，而系统里所有正常的 shutdown/kill 都走 SIGTERM。

这一类 bug 的特征是：**配置看起来是对的**。「有 Restart 啊」。
所以不能靠读一遍 unit 文件确认，要靠一条能失败的断言钉住。

R84 改成 `always` 之后，`kill <pid>` 会被 10 秒后拉起（已实测
`NRestarts` 0 → 1），`systemctl stop` 不会空转（stop 的作业不触发 Restart）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: ★ unit 真相源在 Emotion-CPT（随契约包分发）—— 本测试从「仓内自检」
#: 升级为「跨仓对账」：CPT 的代码 vs 全局唯一的 unit 真相源。
def _unit(name: str) -> Path:
    import emotion_cpt.deploy

    return Path(emotion_cpt.deploy.__file__).resolve().parent / "systemd" / name


_SERVICE = _unit("cpt-dashboard.service")


def _directives() -> dict[str, str]:
    """把 ``Key=Value`` 收成字典（忽略注释与空白行，后出现的覆盖前面的）。"""
    out: dict[str, str] = {}
    for raw in _SERVICE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def test_service_unit_exists() -> None:
    assert _SERVICE.is_file(), f"找不到 {_SERVICE}"


def test_restart_is_always_not_on_failure() -> None:
    """**核心断言**。``on-failure`` 不覆盖 SIGTERM —— 这是 55 小时停机的那个洞。

    如果谁把它改回去（或改成 ``no``），这条会红。不要为了「看起来更保守」
    改回去：on-failure 在这里不是保守，是**漏**。
    """
    assert _directives().get("Restart") == "always"


def test_restart_sec_is_bounded() -> None:
    """``Restart=always`` 必须配一个有限 ``RestartSec``。

    没有它，systemd 的默认 100ms 会让「反复崩溃」变成启动风暴（真正打满
    CPU 的那种事故）。10s 是 55 小时停机与启动风暴之间的那个点。
    """
    raw = _directives().get("RestartSec")
    assert raw is not None, "Restart=always 却不设 RestartSec，默认 100ms 太快"
    match = re.fullmatch(r"(\d+)(s|sec|seconds)?", raw)
    assert match, f"RestartSec 写法认不出来：{raw!r}"
    assert int(match.group(1)) >= 1, "RestartSec < 1s 会打满启动风暴"


def test_the_sandbox_hardening_was_not_dropped_along_the_way() -> None:
    """R84 只该动 Restart 一项 —— 顺手把 R58 的加固删掉是更贵的事故。

    这条不是「完整性检查」：``ProtectSystem=strict`` / ``ProtectHome=read-only``
    一旦丢失，服务就重新对整个 home 有写权限。
    """
    d = _directives()
    assert d.get("ProtectSystem") == "strict"
    assert d.get("ProtectHome") == "read-only"
    assert d.get("NoNewPrivileges") == "true"
    assert "/home/ubuntu/trade_cpt" in d.get("ReadWritePaths", "")
    assert "/home/ubuntu/.cache/cpt" in d.get("ReadWritePaths", "")


def test_service_is_not_given_home_write_through_a_parent_path() -> None:
    """``ReadWritePaths=/home/ubuntu`` 等于没收敛（R58 的刻意决定）。

    一个手滑把它加进来，前面两条仍然绿 —— 所以单独钉一条。
    """
    paths = _directives().get("ReadWritePaths", "").split()
    assert "/home/ubuntu" not in paths
    assert "/" not in paths


def test_both_service_units_declare_some_restart_policy() -> None:
    """另一份 ``cpt-dashboard-ashare.service`` 是 oneshot 批次，允许没有 Restart，
    但如果有就必须是它自己声明清楚的（不许留空值）。

    这条只防「写了 ``Restart=`` 却什么都不填」这种半截改法。
    """
    other = _SERVICE.parent / "cpt-dashboard-ashare.service"
    if not other.is_file():
        pytest.skip("另一份 unit 不存在")
    for raw in other.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("Restart="):
            assert line.partition("=")[2].strip(), f"{other.name} 的 Restart= 是空的"
