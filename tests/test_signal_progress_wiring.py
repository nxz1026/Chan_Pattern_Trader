"""R71 进度条端到端契约：后端产出 → 快照 → 前端读取。

本文件钉的是**接线**，不是措辞（措辞在 ``tests/test_signal_progress.py``）。
接线断掉时症状是「面板永远 hidden」，单测全绿也发现不了 —— 所以要有一条
跨层测试：生产函数产出的键名必须与前端 ``querySelector`` 的 testid、
``dash-structure`` 的读取路径逐字一致。
"""

from __future__ import annotations

from pathlib import Path

from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.application.signal_progress import first_buy_progress
from cpt.domain.config import RulesConfig

_DASH = Path(__file__).resolve().parents[1] / "dashboard"


class _F:
    has_two_centers = True
    has_divergence_leg = True
    has_reversal_bi = False
    divergence_status = "not_detected"


def test_snapshot_exposes_progress_under_summary_key() -> None:
    """前端读的是 ``summary.signal_progress``，快照必须挂在这个路径下。"""
    progress = first_buy_progress(_F(), segment_status="down")
    snapshot = build_dashboard_snapshot_v2(RulesConfig(), [], signal_progress=progress)
    assert snapshot["summary"]["signal_progress"] == progress
    assert snapshot["summary"]["signal_progress"]["steps"]


def test_snapshot_progress_is_none_when_not_supplied() -> None:
    """不传就得是 None，不能自作主张塞一个空进度冒充「算过了」。"""
    snapshot = build_dashboard_snapshot_v2(RulesConfig(), [])
    assert snapshot["summary"]["signal_progress"] is None


def test_progress_does_not_enter_the_signal_dataclass() -> None:
    """``Signal`` 是受契约钉住的领域对象；进度不许混进去。"""
    progress = first_buy_progress(_F(), segment_status="down")
    snapshot = build_dashboard_snapshot_v2(RulesConfig(), [], signal_progress=progress)
    assert snapshot["signal"] is None
    assert (
        "signal_progress" not in snapshot["summary"]["signal"]
        if snapshot["summary"]["signal"]
        else True
    )


def test_frontend_reads_progress_from_summary_path() -> None:
    structure_js = (_DASH / "dash-structure.js").read_text(encoding="utf-8")
    assert "snapshot.summary" in structure_js
    assert "summary.signal_progress" in structure_js


def test_frontend_render_targets_exist_in_html() -> None:
    """``renderSignalProgress`` 写的每个 testid，HTML 里都必须有对应节点。"""
    html = (_DASH / "index.html").read_text(encoding="utf-8")
    signal_js = (_DASH / "dash-signal.js").read_text(encoding="utf-8")
    for testid in (
        "signal-progress",
        "signal-progress-steps",
        "signal-progress-explain",
        "signal-progress-action",
    ):
        assert f"data-testid={testid}" in signal_js or f'data-testid="{testid}"' in signal_js
        assert f'data-testid="{testid}"' in html, f"{testid} 在 HTML 里不存在，渲染会静默失败"


def test_progress_block_hidden_by_default_and_hidden_when_empty() -> None:
    """「没数据」必须整块藏起来，不能显示一堆占位破折号。"""
    html = (_DASH / "index.html").read_text(encoding="utf-8")
    signal_js = (_DASH / "dash-signal.js").read_text(encoding="utf-8")
    assert 'data-testid="signal-progress" hidden' in html
    assert "node.hidden = true" in signal_js


def test_signal_panel_still_declares_the_measured_numbers() -> None:
    """R88：面板上的实测数字换成 R87 那组（R70 的 +0.02% 是 confirmed 组，已作废）。

    这个测试盯两件事：数字在（删掉横幅要红）、且不许出现 R70 那组老数字
    （老数字与 §8 的可复跑面板层不是同一口径，混用会让文案自相矛盾）。
    """
    html = (_DASH / "index.html").read_text(encoding="utf-8")
    assert "+0.56%" in html
    assert "45.6%" in html
    assert "+0.57%" in html
    assert "44.8%" in html
    assert "它不预测涨跌" in html
    for stale in ("688,496", "+0.11% / +0.18% / +0.02%", "+0.81%"):
        assert stale not in html, f"{stale} 是 R70 口径，已作废"
    assert "data-testid=signal-advice" in html or 'data-testid="signal-advice"' in html


def test_status_labels_do_not_claim_a_buy_point() -> None:
    """状态词不许再用买点语气（「等待触发」「已确认：…」）。"""
    core = (_DASH / "dash-core.js").read_text(encoding="utf-8")
    assert "尚未触发" not in core
    assert "位置已到（不是买点）" in core
