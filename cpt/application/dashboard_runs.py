"""Research dashboard run index helpers.

**状态：待接线（pending-wiring，2026-09-25 审核 P0-2）**——生产代码零导入，尚无
调用方；``/api/dashboard/runs`` 路由已在服务，但 ``dashboard_snapshot_v2.py:62``
硬编码 ``v2["runs"] = []``。保留原因与接线计划见 ``docs/pending-wiring.md``，
改动前请先读该文档。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def build_run_index(snapshots: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Project snapshots into a deterministic dataset/run browser index."""
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        market = snapshot.get("market", {})
        runtime = snapshot.get("runtime", {})
        reproducibility = snapshot.get("reproducibility", {})
        rows.append(
            {
                "run_id": runtime.get("run_id") or reproducibility.get("dataset_hash"),
                "symbol": market.get("symbol"),
                "interval_ms": market.get("interval_ms"),
                "bar_count": market.get("bar_count", len(snapshot.get("candles", []))),
                "dataset_hash": reproducibility.get("dataset_hash"),
                "config_hash": reproducibility.get("config_hash"),
                "source": runtime.get("data_source"),
                "generated_at": reproducibility.get("generated_at"),
                "status": runtime.get("status", "unknown"),
            }
        )
    return tuple(
        sorted(rows, key=lambda row: (str(row.get("generated_at")), str(row.get("run_id"))))
    )
