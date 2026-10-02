"""parity 视图模型的契约测试（R35 从 R20 的删除里恢复 + 补新行为）。

R20（``a5365bd``）把本模块连同它的两条自证用例一起删了，理由是「参照实现已删除 ⇒
永久 unavailable」。R35 参照侧改成「czsc 优先，回落腾讯」之后，那条前提不成立 ——
所以模块与测试一起复活。**下面两条是 R20 删掉前的原文**（逐字恢复），它们钉住的
是前端 28 处消费点依赖的形状；改动这个模块时它们必须继续过。
"""

from __future__ import annotations

from typing import Any

from cpt.application.dashboard_parity import build_parity_snapshot, build_parity_view

# --- R20 删除前的原文（逐字恢复） ---------------------------------------------


def test_parity_classifies_missing_extra_and_match() -> None:
    result = build_parity_view(
        [{"kind": "top", "bar_index": 1}],
        [{"kind": "top", "bar_index": 1}, {"kind": "bottom", "bar_index": 2}],
        kind="fractal",
    )
    # R20 删除前的原文是**全等**断言。R35 往 summary 里加了三个容差相关键
    # （drifted / max_drift_pct / value_tolerance_pct），全等会因此失败 ——
    # 但那六个原始键的**语义**才是 R20 钉住的东西，所以逐个校验它们。
    summary = result["summary"]
    assert {
        k: summary[k] for k in ("matched", "missing", "extra", "mismatched", "total", "match_rate")
    } == {
        "matched": 1,
        "missing": 1,
        "extra": 0,
        "mismatched": 0,
        "total": 2,
        "match_rate": 0.5,
    }
    assert any(item["status"] == "missing" for item in result["items"])


def test_parity_snapshot_uses_explicit_oracle_inputs() -> None:
    item = {"kind": "top", "bar_index": 1}
    result = build_parity_snapshot(fractals=(item,), ref_fractals=(item,))
    assert result["fractals"]["summary"]["matched"] == 1
    assert result["fractals"]["summary"]["extra"] == 0


# --- R35 补的：语义与新增字段 -------------------------------------------------


def test_missing_and_extra_describe_the_cpt_side() -> None:
    """**status 描述的是 CPT 侧的偏差**。

    CPT 没有 → ``missing``（参照有）；CPT 多出来 → ``extra``。
    这条容易被"顺手改成对称"写反，所以单独钉一条。
    """
    cpt = [{"level": 1, "start_time": 10, "end_time": 20, "direction": "up"}]
    ref = cpt + [{"level": 1, "start_time": 30, "end_time": 40, "direction": "down"}]
    view = build_parity_view(cpt, ref, kind="bi")
    assert view["summary"]["missing"] == 1
    assert view["summary"]["extra"] == 0

    view2 = build_parity_view(ref, cpt, kind="bi")
    assert view2["summary"]["extra"] == 1
    assert view2["summary"]["missing"] == 0


def test_level_is_part_of_the_key() -> None:
    """不同层级的同区间结构**不能**配成一对。"""
    a = [{"level": 1, "start_time": 10, "end_time": 20, "direction": "up"}]
    b = [{"level": 2, "start_time": 10, "end_time": 20, "direction": "up"}]
    view = build_parity_view(a, b, kind="bi")
    assert view["summary"]["matched"] == 0
    assert view["summary"]["missing"] == 1
    assert view["summary"]["extra"] == 1


def test_placeholder_time_never_counts_as_a_match() -> None:
    """``PLACEHOLDER_TIME`` 是"没算出来"的哨兵。

    两侧都带占位时如果参与对照，会得到一个**绿色的假象**（matched+1）——
    所以必须滤掉，且滤掉之后 total 不含它们。
    """
    placeholder: dict[str, Any] = {
        "level": 1,
        "start_time": -1,
        "end_time": -1,
        "direction": "up",
    }
    real: dict[str, Any] = {
        "level": 1,
        "start_time": 10,
        "end_time": 20,
        "direction": "up",
    }
    view = build_parity_view([placeholder, real], [placeholder, real], kind="bi")
    assert view["summary"]["total"] == 1
    assert view["summary"]["matched"] == 1


def test_unavailable_payload_carries_reason_and_no_kinds() -> None:
    """``available=False`` 时**不能**带三个 kind —— 前端会按 kinds 渲染表格。"""
    out = build_parity_snapshot(available=False, reason="reference_unavailable")
    assert out["available"] is False
    assert out["reason"] == "reference_unavailable"
    for kind in ("fractals", "bis", "zhongshus"):
        assert kind not in out


def test_reference_metadata_is_always_present() -> None:
    """参照侧是谁必须写在 payload 里（owner 2026-10-02：czsc 优先，回落腾讯）。"""
    out = build_parity_snapshot(reference="tencent_hfq", reference_detail="回落")
    assert out["reference"] == {"source": "tencent_hfq", "detail": "回落"}
    assert out["available"] is True
