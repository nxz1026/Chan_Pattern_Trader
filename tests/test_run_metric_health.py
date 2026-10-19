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

import json

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


# ── 自证不实：末端时间戳（2026-10-19，审计 P2-bl）────────────────────────────


def _aged_snapshot(*, bar_time: int = 1_700_000_000_000, as_of: int = 1_700_000_000_000):
    """造一份「自带时刻」的快照 —— 本判据必须能从快照内部自证，不读时钟。"""
    snap = _snapshot(bars=30)
    snap["candles"] = [{"open_time": bar_time}]  # type: ignore[list-item]
    snap["runtime"]["generated_at"] = as_of  # type: ignore[index]
    return snap


def test_fresh_snapshot_is_ok() -> None:
    """正常轮次：末根 K 线就是几十分钟前 —— 不能被新判据误伤。"""
    base = 1_700_000_000_000
    assert _health(_aged_snapshot(bar_time=base + 29 * 3_600_000, as_of=base + 30 * 3_600_000)) == (
        HEALTH_OK
    )


def test_synthetic_bar_time_is_failing() -> None:
    """★本次修复：``_synthetic_bar`` 的 ``1_700_000_000_000`` 落在 2023-11。

    这是测试用假 client 时真实写进生产库的那一行（实测污染 141 行）。它的
    ``bar_count=30`` 让旧判据算出 ``ok``，而 ``last_bar_time`` 距快照自述时刻
    有 1053 天 —— 序列自称来自本轮运行，时间戳却在三年前。
    """
    assert _health(_aged_snapshot(bar_time=1_700_000_000_000, as_of=1_792_000_000_000)) == (
        HEALTH_FAILING
    )


def test_future_bar_time_is_failing_too() -> None:
    """超前与滞后同样不可能是真的（取绝对值）。"""
    base = 1_700_000_000_000
    assert _health(_aged_snapshot(bar_time=base + 253 * 86_400_000, as_of=base)) == HEALTH_FAILING


def test_no_self_reported_clock_means_no_judgement() -> None:
    """拿不到自述时刻时**放弃**判定，而不是当成「很旧」。

    这是本判据能纯函数化的关键：``_snapshot()`` 那批老快照没有
    ``generated_at``/``as_of_ms``，它们必须继续是 ``ok``。
    """
    snap = _snapshot(bars=30)
    assert "generated_at" not in snap["runtime"]  # type: ignore[operator]
    assert _health(snap) == HEALTH_OK


def test_as_of_ms_is_accepted_as_the_clock() -> None:
    """A 股快照不传 ``generated_at``，只传 ``as_of_ms``（见 dashboard_runs 兜底顺序）。"""
    snap = _snapshot(bars=30)
    snap["candles"] = [{"open_time": 1_700_000_000_000}]  # type: ignore[list-item]
    snap["runtime"]["as_of_ms"] = 1_792_000_000_000  # type: ignore[index]
    assert _health(snap) == HEALTH_FAILING


def test_implausible_bar_time_is_explained_in_detail() -> None:
    """判成 failing 必须说清是哪条 —— 否则面板上只剩一堆正常的计数字段。"""
    row = metric_from_snapshot(
        _aged_snapshot(bar_time=1_700_000_000_000, as_of=1_792_000_000_000),
        market="crypto",
        symbol="BTCUSDT",
        backend="NativeBackend",
    )
    detail = json.loads(str(row["detail"]))
    assert detail["bar_time_implausible"] is True
    assert detail["bar_lag_ms"] == 1_792_000_000_000 - 1_700_000_000_000


def test_hard_failure_wins_over_implausible_time() -> None:
    """优先级与 stale/gap 同级，但都在「无 bar」之后（无 bar 是更硬的事实）。"""
    snap = _aged_snapshot(bar_time=1_700_000_000_000, as_of=1_792_000_000_000)
    snap["candles"] = []
    snap["runtime"]["buffer_size"] = 0  # type: ignore[index]
    assert _health(snap) == HEALTH_FAILING


def test_implausible_time_does_not_mask_degraded() -> None:
    """反过来也要成立：时间戳不可信时不该报 degraded（那说轻了）。"""
    snap = _aged_snapshot(bar_time=1_700_000_000_000, as_of=1_792_000_000_000)
    snap["runtime"]["degraded"] = True  # type: ignore[index]
    assert _health(snap) == HEALTH_FAILING


def test_documented_threshold_matches_the_gap_in_real_data() -> None:
    """阈值必须有依据地落在实测空档里（写下这条以免以后被随手改小）。

    实测（2026-10-19，生产 `cpt_run_metric` 48216 行）：
    A 股最慢的**合法**滞后是 368.01h ≈ 15.3 天（收盘后入库的日频批次），
    合成样本是 1053.5 ~ 1067.9 天。30 天两侧各留 2 倍以上余量。
    """
    from cpt.application.run_metric import MAX_PLAUSIBLE_BAR_LAG_MS

    as_seconds = MAX_PLAUSIBLE_BAR_LAG_MS / 1000
    assert as_seconds > 15.3 * 86_400, "不能小到把 A 股日频批次判成故障"
    assert as_seconds < 1053.5 * 86_400, "不能大到放过合成样本"
