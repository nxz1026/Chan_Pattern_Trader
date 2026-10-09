"""``cpt.application.dashboard_stats`` 的统计口径测试。

R59（审计 M27）：``alert_to_confirmed_rate`` 名实不符 —— 分子是 ``confirmed``
事件条数、分母是**全部事件**条数。数据源是状态跃迁事件（见模块 docstring），
所以诚实口径是「confirmed 事件占全部事件的比例」，已改名 ``confirmed_rate``。
这里同时钉住**后端 key 与前端读取/标签一致**：口径改名只改一边，面板会显示
旧标签 + ``undefined``。
"""

from __future__ import annotations

from pathlib import Path

from cpt.application.dashboard_stats import signal_statistics

_SIGNAL_JS = Path(__file__).resolve().parents[1] / "dashboard" / "dash-signal.js"


def test_signal_statistics_reports_confirmed_rate_and_divergence_counts() -> None:
    result = signal_statistics(
        [
            {"status": "confirmed", "divergence_status": "detected"},
            {"status": "invalidated", "divergence_status": "not_detected"},
            {"status": "alert", "divergence_status": "detected"},
        ]
    )
    assert result["total"] == 3
    assert result["invalidated_count"] == 1
    # 口径 = confirmed 事件 / 全部事件；名字与口径一致（不再叫「转化率」）
    assert result["confirmed_rate"] == 1 / 3
    assert "alert_to_confirmed_rate" not in result
    assert result["divergence_counts"]["detected"] == 2


def test_confirmed_rate_is_none_for_empty_sample() -> None:
    assert signal_statistics([])["confirmed_rate"] is None


def test_frontend_reads_confirmed_rate_with_matching_label() -> None:
    """前端消费方必须与后端 key 同步（改一边不改另一边 = 面板显示 undefined）。"""
    source = _SIGNAL_JS.read_text(encoding="utf-8")
    assert "stats.confirmed_rate" in source
    assert "stats.alert_to_confirmed_rate" not in source
    assert "确认占比" in source
    # 旧标签不得再作为行标签出现（注释里回顾历史名称不算）
    assert 'row("预警→确认率"' not in source
