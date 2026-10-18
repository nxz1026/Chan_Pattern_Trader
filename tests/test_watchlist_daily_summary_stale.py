"""`scripts/watchlist_daily_summary.py` 在「快照刷新全量失败」时不许静默推陈旧价位。

R67（审计 P2-15）。现场：`refresh_snapshots` 是 best-effort，而**全量**失败
（`refreshed == 0`，通常意味着 8010 仪表盘挂着 / 端口不通）与「一只票失败」
同处置 —— 照发汇总，正文只有现价、**没有任何日期**，退出码也不变。
于是每天 18:10 都在推昨天的价位，收件人看不出来。而这支脚本自己的
`refresh_snapshots` docstring 写着「看着像真的却没有参考价值（比不推更糟）」。

修法（两条一起，缺一不可）：
1. **正文里插警示行**（放最前面，收件人第一眼就看到）—— 让「推了」这件事仍然是
   有价值的（价位仍可参考，只是标了「可能不是今天的」）；
2. **退出码改 5**（`EXIT_STALE`）—— 让 cron 日志能发现。这里**不能用 3**
   （`EXIT_UNDELIVERED`）：`deploy/cron` 的封装会据此打印「当天没有汇总推送」，
   而实际上推了 —— 日志不许撒谎（该封装已为 rc=5 单独写了一行）。

全程离线：假 `read_pairs` / `refresh_snapshots` / `read_rows` / `notify`。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "watchlist_daily_summary.py"


def _load() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("watchlist_daily_summary_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod() -> types.ModuleType:
    return _load()


def _run(
    monkeypatch: pytest.MonkeyPatch,
    mod: types.ModuleType,
    *,
    pairs: list[tuple[str, str]],
    refreshed: int = 0,
    argv: tuple[str, ...] = (),
    webhook: bool = True,
    summary: tuple[str, list[str]] | None = (
        "关注列表收盘汇总",
        ["600519 现价 1500", "000002 现价 12"],
    ),
) -> tuple[int, dict[str, Any]]:
    """跑一次 `main()`，返回 (退出码, 记录)。"""
    calls: dict[str, Any] = {"notified": None}
    monkeypatch.setattr(mod, "read_pairs", lambda: pairs)

    def fake_refresh(ps: list[tuple[str, str]]) -> int:
        calls["refresh_called"] = True
        calls["refresh_pairs"] = list(ps)
        return refreshed

    monkeypatch.setattr(mod, "refresh_snapshots", fake_refresh)
    monkeypatch.setattr(mod, "read_rows", lambda: [{"code": "600519"}])
    monkeypatch.setattr(mod, "webhook_configured", lambda: webhook)
    monkeypatch.setattr(
        mod,
        "signal_notify",
        types.SimpleNamespace(build_watchlist_summary=lambda rows: summary),
    )

    def fake_notify(title: str, lines: list[str], **kwargs: Any) -> bool:
        calls["notified"] = {"title": title, "lines": list(lines)}
        return True

    monkeypatch.setattr(mod, "notify", fake_notify)
    monkeypatch.setattr(sys, "argv", ["watchlist_daily_summary.py", *argv])
    monkeypatch.setattr(mod, "_LOG", __import__("logging").getLogger("test_wl"))
    return mod.main(), calls


PAIRS = [("u1", "600519"), ("u1", "000002"), ("u2", "600418")]


def test_total_refresh_failure_adds_a_warning_line_and_returns_stale(
    monkeypatch: pytest.MonkeyPatch, mod: types.ModuleType
) -> None:
    """★回归：全量刷新失败 ⇒ 警示行在最前面 + rc=5。"""
    rc, calls = _run(monkeypatch, mod, pairs=PAIRS, refreshed=0)
    assert rc == mod.EXIT_STALE == 5, rc
    lines = calls["notified"]["lines"]
    assert lines[0].startswith("⚠️"), lines
    assert "0/3" in lines[0], lines
    # 原内容必须还在（警示是「加」不是「换」）
    assert any("600519" in line for line in lines), lines


def test_partial_refresh_is_not_treated_as_stale(
    monkeypatch: pytest.MonkeyPatch, mod: types.ModuleType
) -> None:
    """少一只票不值得报警：`refresh_snapshots` 的既有语义就是 best-effort。"""
    rc, calls = _run(monkeypatch, mod, pairs=PAIRS, refreshed=1)
    assert rc == mod.EXIT_OK == 0, rc
    assert not calls["notified"]["lines"][0].startswith("⚠️"), calls["notified"]


def test_empty_watchlist_skips_refresh_entirely(
    monkeypatch: pytest.MonkeyPatch, mod: types.ModuleType
) -> None:
    """没人追踪 ⇒ 压根不刷新，也不该报「刷新失败」。"""
    rc, calls = _run(monkeypatch, mod, pairs=[], refreshed=0)
    assert rc == mod.EXIT_OK == 0, rc
    assert "refresh_called" not in calls
    assert calls["notified"] is not None


def test_dry_run_does_not_refresh_and_never_flags_stale(
    monkeypatch: pytest.MonkeyPatch, mod: types.ModuleType
) -> None:
    """`--dry-run` 不写库（所以不刷新），也就谈不上「价位过期」。"""
    rc, calls = _run(monkeypatch, mod, pairs=PAIRS, refreshed=0, argv=("--dry-run",))
    assert rc == mod.EXIT_OK == 0, rc
    assert "refresh_called" not in calls
    assert calls["notified"] is None


def test_undelivered_still_wins_over_stale(
    monkeypatch: pytest.MonkeyPatch, mod: types.ModuleType
) -> None:
    """没 webhook = 根本没送达（rc=3）—— 不能被 5 盖掉。"""
    rc, calls = _run(monkeypatch, mod, pairs=PAIRS, refreshed=0, webhook=False)
    assert rc == mod.EXIT_UNDELIVERED == 3, rc
    assert calls["notified"] is None


def test_all_five_exit_codes_are_distinct(mod: types.ModuleType) -> None:
    """五个码各说一件事：跑完 / 读失败(4) / 没送达(3) / 送了但过期(5)。"""
    assert len({mod.EXIT_OK, mod.EXIT_UNDELIVERED, mod.EXIT_STORE}) == 3
    assert mod.EXIT_STALE not in {mod.EXIT_OK, mod.EXIT_UNDELIVERED, mod.EXIT_STORE}


def test_cron_wrapper_speaks_separately_about_rc5() -> None:
    """封装脚本必须为 rc=5 单独说一句话（否则日志会谎称「没推送」）。"""
    wrapper = (ROOT / "deploy" / "cron" / "watchlist-daily-summary.sh").read_text(encoding="utf-8")
    assert "-eq 5" in wrapper, wrapper[-400:]
    assert "过期价位" in wrapper
