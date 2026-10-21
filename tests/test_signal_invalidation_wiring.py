"""R71 Step3 失效归因的端到端契约：事件流 → 统计接口 → 前端渲染。

三层各自的单测都在别处：

* 判定语义 → ``tests/test_signal_invalidation.py``
* 统计聚合 → ``tests/test_dashboard_stats.py``
* 落库/还原 → ``tests/test_signal_invalidation.py`` 的落库可达性组

本文件钉的是**跨层接线**：前端读的键名必须与后端产出的键名逐字一致，
并且「失效原因」这一行必须真的会被渲染出来。断线时症状是面板上永远
看不到原因，而三层单测全绿 —— 所以需要一条跨层测试。

前端的**行为**验证不在这里（要起 DOM），用
``.tmp_audit/render_reason_verify.mjs`` 从 bundle 里抽真实函数跑；
本文件只钉静态契约，保证 CI 里不需要 node。
"""

from __future__ import annotations

from pathlib import Path

from cpt.application.dashboard_stats import signal_statistics
from cpt.application.signal_invalidation import REASON_CODES, REASON_TEXT

_DASH = Path(__file__).resolve().parents[1] / "dashboard"


def _row(status: str, reason: str | None = None) -> dict[str, object]:
    return {
        "code": "000006",
        "status": status,
        "divergence_status": "not_detected",
        "invalidated_reason": reason,
        "price": 10.0,
        "transition_time": "2026-10-20T00:00:00+00:00",
    }


def test_stats_emit_the_key_the_frontend_reads() -> None:
    """前端 ``renderStatsPanel`` 读 ``stats.invalidation_reasons``。"""
    stats = signal_statistics([_row("invalidated", "no_divergence_leg")])
    assert "invalidation_reasons" in stats
    signal_js = (_DASH / "dash-signal.js").read_text(encoding="utf-8")
    assert "stats.invalidation_reasons" in signal_js


def test_every_reason_code_has_a_frontend_label() -> None:
    """后端能产生的原因码，前端标签表必须全覆盖，否则界面会掉出原始码。"""
    signal_js = (_DASH / "dash-signal.js").read_text(encoding="utf-8")
    assert "INVALIDATION_REASON_LABELS" in signal_js
    for code in REASON_CODES:
        assert f"{code}:" in signal_js, f"{code} 在前端标签表里缺失，界面会显示英文码"


def test_unknown_bucket_is_labelled_separately() -> None:
    """``unknown`` 是统计层自造的桶（不是领域码），前端必须单独给说法。"""
    signal_js = (_DASH / "dash-signal.js").read_text(encoding="utf-8")
    assert '"unknown"' in signal_js or "'unknown'" in signal_js
    for code in REASON_CODES:
        assert code != "unknown"


def test_history_row_render_is_guarded_by_invalidated_status() -> None:
    """原因行只在 ``status === "invalidated"`` 时渲染，不许无条件打印。"""
    signal_js = (_DASH / "dash-signal.js").read_text(encoding="utf-8")
    assert 'signal.status === "invalidated"' in signal_js
    assert "signal-history-reason" in signal_js


def test_missing_reason_says_so_instead_of_staying_blank() -> None:
    """R71 之前的行没有原因码，界面必须明说「未记录」，不能留空。"""
    signal_js = (_DASH / "dash-signal.js").read_text(encoding="utf-8")
    assert "未记录" in signal_js
    assert "无法反推" in signal_js


def test_reason_text_matches_frontend_wording() -> None:
    """后端 explain 与前端标签讲的是同一件事，不许一边说 A 一边说 B。

    两边措辞长度不同（前端标签要短，后端 explain 是整句），所以只核对
    **同义的语义锚点**必须两边都有：``not_enough_centers`` 两边都得提「中枢」，
    ``no_divergence_leg`` 两边都得说「往下走」。
    """
    signal_js = (_DASH / "dash-signal.js").read_text(encoding="utf-8")
    pairs = {
        "not_in_downtrend": ("下跌", "方向"),
        "not_enough_centers": ("中枢",),
        "no_divergence_leg": ("往下走",),
        "previous_invalidated": ("作废",),
    }
    for code, anchors in pairs.items():
        for anchor in anchors:
            assert anchor in REASON_TEXT[code], f"后端 {code} 文案里没有「{anchor}」"
            assert anchor in signal_js, f"前端 {code} 标签里没有「{anchor}」"


def test_bundle_is_in_sync_with_sources() -> None:
    """改了源文件没重跑构建，线上拿到的还是旧逻辑。"""
    bundle = (_DASH / "dashboard.bundle.js").read_text(encoding="utf-8")
    for marker in ("INVALIDATION_REASON_LABELS", "signal-history-reason", "stats.invalidation_reasons"):
        assert marker in bundle, f"{marker} 没进 bundle，构建没跑"
