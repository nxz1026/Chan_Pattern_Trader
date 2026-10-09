"""R59 审计项（M23/M24/M25/M26/M29/L17）的回归测试。

每条都对应一个「改前红」的判据：

- **M23** 未知状态/类型降级时不得把 ``data_quality`` 谎报成「0 根」；
- **M24** 没有生产者时 ``factor_coverage`` 不得假装 1.0，且「真实 0%」与
  「未实现」可区分；
- **M25** 参照侧必须收到与被参照侧同一份 ``RulesConfig`` 映射（否则配置差异
  被报成算法差异）；
- **M26** ``rules_version`` 必须跟随领域 ``SCHEMA_VERSION``（v1），不是 v0；
- **M29** ``invalidated → confirmed`` 该落 ``confirmed`` 事件，而不是无事件；
- **L17** 空对照不得报「100% 一致」。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from cpt.adapters import backend_factory
from cpt.adapters.reference_chanlun import ReferenceChanlunConfig
from cpt.application.dashboard_parity import build_parity_snapshot
from cpt.application.dashboard_reproducibility import RULES_VERSION, reproducibility_metadata
from cpt.application.parity_reference import build_parity_snapshot_for
from cpt.application.recommendation import MIN_BARS, build_recommendation
from cpt.application.run_metric import metric_from_snapshot
from cpt.domain.config import SCHEMA_VERSION, RulesConfig
from cpt.domain.models import StructureState, StructureStatus
from cpt.domain.structure_events import EVENT_FOR_STATUS, _event_type_for

# ── M23：未知状态/类型降级不得丢 data_quality ──────────────────────────


def _snapshot_with_signal(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "candles": [{"open_time": i, "close": 1.0} for i in range(MIN_BARS)],
        "signal": signal,
    }


def test_m23_unknown_status_keeps_real_data_quality() -> None:
    """改前：``_degraded`` 退回 ``_data_quality({})`` ⇒ bars=0 / sufficient=False。"""
    out = build_recommendation(
        _snapshot_with_signal({"status": "bogus", "signal_type": "first_buy"})
    )
    assert out["available"] is False
    assert out["data_quality"]["bars"] == MIN_BARS
    assert out["data_quality"]["sufficient"] is True
    assert "未知的信号状态" in out["reason"]


def test_m23_unknown_type_keeps_real_data_quality() -> None:
    out = build_recommendation(
        _snapshot_with_signal({"status": "structure_ready", "signal_type": "bogus"})
    )
    assert out["available"] is False
    assert out["data_quality"]["bars"] == MIN_BARS
    assert out["data_quality"]["sufficient"] is True
    assert "未知的信号类型" in out["reason"]


# ── M24：factor_coverage 语义诚实 ──────────────────────────────────────


def _coverage(metric: Any) -> tuple[float, Any, str]:
    detail = json.loads(str(metric["detail"]))
    return (
        float(metric["factor_coverage"]),
        detail["factor_coverage"],
        detail["factor_coverage_source"],
    )


def test_m24_missing_coverage_is_not_reported_as_full() -> None:
    """改前：``or 1.0`` ⇒ 缺失被报成 1.0（覆盖率 100%）。"""
    value, raw, source = _coverage(
        metric_from_snapshot({}, market="cn", symbol="000011", backend="native")
    )
    assert value == 0.0
    assert raw is None
    assert source == "unimplemented"


def test_m24_real_zero_is_distinguishable_from_unimplemented() -> None:
    """真实 0% 与「没实现」数值都是 0.0，靠 ``factor_coverage_source`` 分开。"""
    zero = metric_from_snapshot(
        {"data_quality": {"factor_coverage": 0.0}}, market="cn", symbol="000011", backend="native"
    )
    assert _coverage(zero) == (0.0, 0.0, "snapshot")

    real = metric_from_snapshot(
        {"data_quality": {"factor_coverage": 0.73}}, market="cn", symbol="000011", backend="native"
    )
    assert _coverage(real) == (0.73, 0.73, "snapshot")


# ── M25：参照侧配置映射 ────────────────────────────────────────────────


def test_m25_reference_side_receives_mapped_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """改前：参照侧恒用 ``ReferenceChanlunConfig()`` 默认值 ⇒ cfg 被静默忽略。"""
    captured: dict[str, Any] = {}

    class _Backend:
        source = "fake"

        def compute_domain_structures(self, bars: Any, config: Any) -> tuple[Any, Any, Any]:
            captured["config"] = config
            return (), (), ()

    monkeypatch.setattr(backend_factory, "resolve_backend", lambda *a, **k: _Backend())

    cfg = RulesConfig(macd_fast=13, macd_slow=34, macd_signal=7, zs_wzgx="zgd")
    out = build_parity_snapshot_for(
        code="600519",
        bars=(),
        fractals=(),
        bis=(),
        zhongshus=(),
        production_backend=object(),
        config=cfg,
    )

    assert out["available"] is True
    ref_cfg = captured["config"]
    assert isinstance(ref_cfg, ReferenceChanlunConfig)
    assert ref_cfg.macd_fast == 13  # 默认是 12
    assert ref_cfg.macd_slow == cfg.macd_slow
    assert ref_cfg.macd_signal == cfg.macd_signal
    assert ref_cfg.use_fx_qj_ck == cfg.fx_qj_ck
    assert ref_cfg.use_bi_type_new == cfg.bi_type_new
    assert ref_cfg.zs_wzgx == cfg.zs_wzgx


# ── M26：rules_version 跟随领域 SCHEMA_VERSION ─────────────────────────


def test_m26_rules_version_follows_domain_schema_version() -> None:
    """改前：默认硬编码 ``"rules.v0"``，与领域 ``SCHEMA_VERSION="v1"`` 矛盾。"""
    assert RULES_VERSION == f"rules.{SCHEMA_VERSION}"
    meta = reproducibility_metadata({})
    assert meta["rules_version"] == RULES_VERSION
    assert meta["rules_version"] != "rules.v0"


# ── M29：invalidated → confirmed 事件 ──────────────────────────────────


def _state(status: StructureStatus, *, end_time: int) -> StructureState:
    return StructureState(
        id="cn:bi:5:1000",
        level=5,
        kind="bi",
        direction=-1,
        start_time=1000,
        end_time=end_time,
        status=status,
        revision=1,
        first_seen_at=0,
        confirmed_at=None,
        invalidated_at=1000,
        source_ids=(),
    )


def test_m29_invalidated_to_confirmed_emits_event() -> None:
    """改前：映射缺档、状态确实变了 ⇒ 返回 None ⇒ 事件流永停 invalidated。"""
    event = _event_type_for(
        _state("invalidated", end_time=2000), _state("confirmed", end_time=3000)
    )
    assert event == "confirmed"
    assert EVENT_FOR_STATUS[("invalidated", "confirmed")] == "confirmed"


def test_m29_unmapped_transition_still_has_no_event() -> None:
    """补一档不等于把所有跃迁都当有事件（``forming → open_end`` 仍无事件）。"""
    assert (
        _event_type_for(_state("forming", end_time=2000), _state("open_end", end_time=3000)) is None
    )


# ── L17：空对照不等于 100% 一致 ────────────────────────────────────────


def test_l17_empty_parity_is_not_reported_as_100_percent() -> None:
    """改前：``if total else 1.0`` ⇒ 空对照报「100% 一致」（本面板最坏的假阳性）。"""
    summary = build_parity_snapshot()["fractals"]["summary"]
    assert summary["total"] == 0
    assert summary["match_rate"] is None
    assert summary["match_rate_available"] is False


def test_l17_nonempty_parity_reports_rate() -> None:
    fractal = {
        "level": 5,
        "bar_index": 1,
        "start_time": 1,
        "end_time": 2,
        "high": 11.0,
        "low": 10.0,
    }
    out = build_parity_snapshot(fractals=[fractal], ref_fractals=[dict(fractal)])
    summary = out["fractals"]["summary"]
    assert summary["total"] == 1
    assert summary["match_rate"] == 1.0
    assert summary["match_rate_available"] is True
