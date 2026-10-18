"""对比脚本的「口径」与「退出码」（R67 / 审计 P1-6）。**离线**。

## 为什么单开这组用例

`scripts/compare_chanlun_backends.py` 是仓内**唯一**做 native↔czsc 交叉核对的地方，
而 2026-10-18（R67）发现它**量错了东西**，两个错各有独立危害：

1. **后端无参构造** ⇒ native 拿到 ``min_bi_len=None``（无门槛），600519/300 根量到
   **99 笔**，而生产（``a_share_snapshot.py`` 传 ``min_bi_len_for("1d") == 6``）是
   **28 笔**。拿两个不同口径互证，结论无论正负都没有意义。
2. **计数不一致只打印 ❌、不进退出码** ⇒ 在脚本/门禁调用链里永远"通过"。
3. 仓内测试占位 ``InMemoryChanlunBackend`` 算进「一致性」判定 ⇒ 它**永远**报 ❌
   （占位按 bar 高低点做极简判定，本来就不等价于真后端）⇒ 报红报成背景噪音。

前两条是「量具坏了」，第三条是「量具永远在喊狼来了」—— 同一个后果：没人再看它。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "compare_backends_mod", ROOT / "scripts" / "compare_chanlun_backends.py"
)
assert _spec is not None and _spec.loader is not None
cmp_mod = importlib.util.module_from_spec(_spec)
sys.modules["compare_backends_mod"] = cmp_mod
_spec.loader.exec_module(cmp_mod)


# ── 口径 ────────────────────────────────────────────────────────────────


def test_default_threshold_comes_from_the_production_config() -> None:
    """**这条用例专门杀老写法**：默认门槛必须等于生产那份配置，不能是"没门槛"。"""
    from cpt.domain.config import RulesConfig

    production = RulesConfig().min_bi_len_for("1d")
    assert production > 0, "生产门槛不该是 0（0 = 关闭）"
    assert cmp_mod.resolve_min_bi_len(None) == production


@pytest.mark.parametrize(("raw", "expected"), [(0, None), (3, 3), (6, 6)])
def test_explicit_threshold_overrides(raw: int, expected: int | None) -> None:
    """``0`` = 关闭门槛（复现 R45 的历史数字），其余原样透传。"""
    assert cmp_mod.resolve_min_bi_len(raw) == expected


def test_backends_are_constructed_with_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端必须**带门槛构造函数**，否则量的是另一个口径（P1-6 的根因）。"""
    seen: dict[str, dict[str, Any]] = {}

    def _fake_load(name: str) -> Any:
        class _Backend:
            def __init__(self, **kwargs: Any) -> None:
                seen[name] = kwargs

        return types.SimpleNamespace(
            NativeChanlunBackend=_Backend,
            InMemoryChanlunBackend=_Backend,
            CzscChanlunBackend=_Backend,
        )

    monkeypatch.setattr(cmp_mod, "_load", _fake_load)
    cmp_mod.available_backends(6)

    assert seen["native_chanlun"] == {"min_bi_len": 6}
    # czsc 的 min_bi_len 是 int，不接 None ⇒ 关闭门槛时退化为 1。
    if "czsc_chanlun" in seen:
        assert seen["czsc_chanlun"] == {"min_bi_len": 6}

    seen.clear()
    cmp_mod.available_backends(None)
    assert seen["native_chanlun"] == {"min_bi_len": None}
    if "czsc_chanlun" in seen:
        assert seen["czsc_chanlun"] == {"min_bi_len": 1}


# ── 退出码 ──────────────────────────────────────────────────────────────


class _FakeBackend:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def compute_structures(self, bars: Any, config: Any) -> dict[str, int]:
        return self._counts


def _prepare(monkeypatch: pytest.MonkeyPatch, backends: list[tuple[str, dict[str, int]]]) -> None:
    monkeypatch.setattr(cmp_mod, "fetch_bars", lambda code, n: [])
    monkeypatch.setattr(cmp_mod, "check_contract", lambda label, res: [])
    monkeypatch.setattr(cmp_mod, "count_of", lambda res: res)
    monkeypatch.setattr(
        cmp_mod,
        "available_backends",
        lambda min_bi_len: [(label, _FakeBackend(c)) for label, c in backends],
    )


_A = {"fractals": 100, "bis": 28, "zhongshus": 4}
_B = {"fractals": 99, "bis": 15, "zhongshus": 2}


def test_count_mismatch_reaches_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**R67 / P1-6 回归**：真后端计数不一致必须 ``return 1``。

    老写法只打印 ``❌ 存在差异`` 却仍 ``return 0`` —— 调用方看到的是"成功"。
    """
    _prepare(monkeypatch, [("native", _A), ("czsc", _B)])
    assert cmp_mod.main(["--code", "600519", "--bars", "1"]) == 1
    assert "真后端计数不一致" in capsys.readouterr().out


def test_agreement_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """对照：口径一致时退出码必须是 0（否则门禁永远红，等于没有门禁）。"""
    _prepare(monkeypatch, [("native", _A), ("czsc", dict(_A))])
    assert cmp_mod.main(["--code", "600519", "--bars", "1"]) == 0
    assert "真后端计数一致: ✅" in capsys.readouterr().out


def test_placeholder_is_excluded_from_the_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """测试占位后端不许影响判定 —— 否则脚本永远报 ❌，报红就成了背景噪音。"""
    _prepare(
        monkeypatch,
        [("native", _A), ("czsc", dict(_A)), (cmp_mod.PLACEHOLDER_LABEL, _B)],
    )
    assert cmp_mod.main(["--code", "600519", "--bars", "1"]) == 0
    out = capsys.readouterr().out
    assert "真后端计数一致: ✅" in out
    assert "已排除在一致性判定之外" in out


def test_single_real_backend_does_not_claim_a_comparison(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """只有一个真后端时不能声称"对比通过" —— 也不能凭空造一个失败。"""
    _prepare(monkeypatch, [("native", _A), (cmp_mod.PLACEHOLDER_LABEL, _B)])
    assert cmp_mod.main(["--code", "600519", "--bars", "1"]) == 0
    assert "不能声称做了对比" in capsys.readouterr().out
