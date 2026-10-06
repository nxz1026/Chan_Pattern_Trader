"""``cpt.application.run_metric.metric_from_snapshot`` 的 **health 三态**。

这个模块以前**一个测试都没有**（``metric_from_snapshot`` 与 ``HEALTH_OK`` 在
``tests/`` 里零命中），所以下面这些缺陷能长期存活：

- ``HEALTH_DEGRADED`` 有常量、``scripts/run_inspection.py`` 有消费方
  （``elif row["health"] == "degraded"``）、``cpt.web.__main__._degraded_snapshot``
  有生产者（往快照 ``runtime`` 写 ``degraded=True``）—— **唯独中间的
  ``metric_from_snapshot`` 不读它**，于是「降级但可用」被静默记成 ``ok``。
  owner 确认那是误删，2026-10-06 恢复。

纯函数，零 IO（不碰 DB），所以这里不需要假连接。
"""

from __future__ import annotations

import pytest

from cpt.application.run_metric import (
    HEALTH_DEGRADED,
    HEALTH_FAILING,
    HEALTH_OK,
    metric_from_snapshot,
)


def _snapshot(
    *,
    bars: int = 100,
    gap_count: int = 0,
    stale: bool = False,
    degraded: bool = False,
    degraded_reason: str = "",
) -> dict[str, object]:
    """造一份最小快照：只需要 metric_from_snapshot 真正读的那几个键。"""
    return {
        "candles": [{"open_time": 1_700_000_000_000} for _ in range(bars)],
        "data_quality": {"gap_count": gap_count, "stale": stale},
        "runtime": {
            "buffer_size": bars,
            "stale": stale,
            "degraded": degraded,
            "degraded_reason": degraded_reason,
        },
        "reproducibility": {
            "config_hash": "cfg1",
            "dataset_hash": "ds1",
            "rules_version": "v0",
        },
        "overlays": {"bis": [], "fractals": [], "zhongshus": [], "trend_types": []},
    }


def _health(snap: dict[str, object]) -> str:
    # RunMetric 是 TypedDict，所以返回的是普通 dict，要用下标取值
    row = metric_from_snapshot(snap, market="cn", symbol="600519", backend="NativeBackend")
    return str(row["health"])


def test_healthy_run_is_ok() -> None:
    assert _health(_snapshot()) == HEALTH_OK


def test_degraded_runtime_produces_degraded() -> None:
    """⚠️ 本次修复的核心：快照带 ``runtime.degraded`` ⇒ health=degraded。

    这正是「后端/数据源不可用但仍算出了可展示结果」那类轮次。以前一律记成
    ``ok``，巡检视角里全是绿的。
    """
    assert _health(_snapshot(degraded=True, degraded_reason="wind 探不通")) == HEALTH_DEGRADED


def test_degraded_does_not_mask_a_hard_failure() -> None:
    """降级标记**不能**盖过硬故障：没数据就是 failing，不是 degraded。

    顺序反了会把「压根没算出来」粉饰成「可用但降级」，比不记更坏。
    """
    assert _health(_snapshot(bars=0, degraded=True)) == HEALTH_FAILING
    assert _health(_snapshot(stale=True, degraded=True)) == HEALTH_FAILING
    assert _health(_snapshot(gap_count=3, degraded=True)) == HEALTH_FAILING


def test_failing_conditions_are_independent_of_degraded() -> None:
    assert _health(_snapshot(bars=0)) == HEALTH_FAILING
    assert _health(_snapshot(stale=True)) == HEALTH_FAILING
    assert _health(_snapshot(gap_count=1)) == HEALTH_FAILING


def test_runtime_stale_also_fails() -> None:
    """``runtime.stale`` 与 ``data_quality.stale`` 两个来源都要认。"""
    snap = _snapshot()
    snap["runtime"]["stale"] = True  # type: ignore[index]
    assert _health(snap) == HEALTH_FAILING


@pytest.mark.parametrize("bar_count", [0, 1, 5, 100, 1000])
def test_health_is_stable_across_bar_counts(bar_count: int) -> None:
    """有 bar 就是 ok/degraded 分界，无 bar 就是 failing —— 与数量无关。"""
    expected = HEALTH_OK if bar_count > 0 else HEALTH_FAILING
    assert _health(_snapshot(bars=bar_count)) == expected
