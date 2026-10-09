"""Read-only research statistics projections.

**状态：已接线（R22，2026-09-30）**——`GET /api/dashboard/signal-stats?days=&code=`
（`cpt/web/app.py`）的唯一实现，事件来自 `cpt.storage.signal_event_store.load_signal_events`；
对应 `docs/archive/plans-and-acceptance.md` Phase 6 P2「一买统计」。响应必须自报
`basis: "signal_event_transitions"`：这是**状态跃迁事件**分布，不是「当前若干只票
的状态」。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def signal_statistics(signals: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Summarize signal status and invalidation reasons without changing inputs."""
    statuses: dict[str, int] = {}
    divergences: dict[str, int] = {}
    for signal in signals:
        status = str(signal.get("status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1
        divergence = str(signal.get("divergence_status", "unknown"))
        divergences[divergence] = divergences.get(divergence, 0) + 1
    total = len(signals)
    confirmed = statuses.get("confirmed", 0)
    return {
        "total": total,
        "status_counts": statuses,
        "divergence_counts": divergences,
        # R59（审计 M27）：原名 ``alert_to_confirmed_rate`` 名实不符 —— 分子是
        # ``confirmed`` 事件的条数，分母是**全部事件**条数。它不是「预警后来被
        # 确认的比例」（事件流里没有从 alert 出发的配对），也不是「确认 / 预警」。
        # 数据源是**状态跃迁事件**（见模块 docstring），故诚实口径是
        # 「confirmed 事件占全部事件的比例」。名字与前端标签
        # （``dashboard/dash-signal.js``）必须同步改，否则面板仍把占比当转化率讲。
        "confirmed_rate": confirmed / total if total else None,
        "invalidated_count": statuses.get("invalidated", 0),
    }
