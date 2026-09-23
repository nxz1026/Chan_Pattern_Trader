from __future__ import annotations

import math

import pytest
from cpt.adapters.native_chanlun import NativeChanlunBackend
from cpt.application.multi_level import build_multi_level, structures_for_level
from cpt.domain.config import RulesConfig
from cpt.domain.models import make_canonical_bar


def _wave_bars(count: int = 200, *, interval_ms: int = 60_000) -> tuple:
    """构造能产生足够走势类型的 K 线序列（单边震荡 + 反向）。"""
    return tuple(
        make_canonical_bar(
            open_time=index * interval_ms,
            close_time=index * interval_ms + interval_ms - 1,
            open=100 + 10 * math.sin(index / 4.0),
            high=101 + 10 * math.sin(index / 4.0),
            low=99 + 10 * math.sin(index / 4.0),
            close=100.5 + 10 * math.sin(index / 4.0),
        )
        for index in range(count)
    )


def test_multi_level_returns_seed_level_zero() -> None:
    bars = _wave_bars(120)
    out = build_multi_level(bars, RulesConfig(), NativeChanlunBackend(), levels=(5,))
    assert set(out.keys()) == {5}
    assert isinstance(out[5]["fractals"], tuple)


def test_multi_level_recurses_to_higher_levels() -> None:
    """多级别真实叠加：高级别由 classify_trend→map_trend_types→detect_fractals 真实算出。"""
    bars = _wave_bars(400)
    out = build_multi_level(bars, RulesConfig(), NativeChanlunBackend(), levels=(5, 30))
    assert set(out.keys()) == {5, 30}
    fractals_high = out[30]["fractals"]
    assert all(getattr(f, "level", None) == 30 for f in fractals_high)
    assert out[5]["fractals"]


def test_structures_for_level_returns_requested_level() -> None:
    bars = _wave_bars(200)
    out = build_multi_level(bars, RulesConfig(), NativeChanlunBackend(), levels=(5, 30))
    fractals, bis, zs = structures_for_level(out, 30)
    assert all(f.level == 30 for f in fractals)


def test_structures_for_level_falls_back_to_nearest() -> None:
    bars = _wave_bars(120)
    out = build_multi_level(bars, RulesConfig(), NativeChanlunBackend(), levels=(5,))
    fractals, _, _ = structures_for_level(out, 60)  # 60 不存在 → 回落 5
    assert all(f.level == 5 for f in fractals)


def test_multi_level_handles_empty_bars() -> None:
    out = build_multi_level((), RulesConfig(), NativeChanlunBackend(), levels=(5, 30))
    assert out == {
        5: {"fractals": (), "bis": (), "zhongshus": ()},
        30: {"fractals": (), "bis": (), "zhongshus": ()},
    }


def test_multi_level_rejects_empty_levels() -> None:
    with pytest.raises(ValueError):
        build_multi_level(_wave_bars(120), RulesConfig(), NativeChanlunBackend(), levels=())


def test_higher_level_failure_does_not_wipe_seed_level(monkeypatch) -> None:
    """回归（2026-09-23）：高级别递归失败只影响该级别，不能连坐低级别结构。

    真实行情上一次宽 except 曾让 5m 的 213 个分型全部消失（看板显示“零结构”）。
    """
    import cpt.application.multi_level as ml

    def boom(*args, **kwargs):
        raise ValueError("StructureElement time/range invalid")

    monkeypatch.setattr(ml, "map_trend_types", boom)
    out = build_multi_level(_wave_bars(200), RulesConfig(), NativeChanlunBackend(), levels=(5, 30))
    assert set(out.keys()) == {5, 30}
    assert out[5]["fractals"], "低级别分型不得因高级别失败而清空"
    assert out[5]["bis"], "低级别笔不得因高级别失败而清空"
    assert out[30] == {"fractals": (), "bis": (), "zhongshus": ()}


def test_fallback_multi_level_keeps_primary_structures() -> None:
    """回归（2026-09-23）：多级别递归不可用时，主级别仍须带真实结构。"""
    from cpt.web.__main__ import _fallback_multi_level

    config = RulesConfig()
    marker = ("fx",)
    fallback = _fallback_multi_level(config, marker, ("bi",), ("zs",))  # type: ignore[arg-type]
    primary = config.levels[0]
    assert fallback[primary]["fractals"] == marker
    assert fallback[primary]["bis"] == ("bi",)
    assert fallback[primary]["zhongshus"] == ("zs",)
    for level in config.levels[1:]:
        assert fallback[level] == {"fractals": (), "bis": (), "zhongshus": ()}
