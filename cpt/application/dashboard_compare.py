"""Read-only A/B dashboard comparison helpers.

**状态：已接线（R22，2026-09-30）**——`GET /api/dashboard/compare?left=&right=`
（`cpt/web/app.py`）的唯一实现，对应 `docs/dashboard-product-roadmap.md` R5
「两份 snapshot 字段级 diff」。入参是**本进程已记录的快照本体**
（`dashboard_runs.run_body`），故进程重启后历史 run 不可比。
"""

from __future__ import annotations

from typing import Any

from cpt.application.dashboard_reproducibility import snapshot_diff


def compare_snapshots(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic summary and field-level differences for two runs."""
    left_runtime = left.get("runtime", {})
    right_runtime = right.get("runtime", {})
    left_repro = left.get("reproducibility", {})
    right_repro = right.get("reproducibility", {})
    return {
        "left_run_id": left_runtime.get("run_id") if isinstance(left_runtime, dict) else None,
        "right_run_id": right_runtime.get("run_id") if isinstance(right_runtime, dict) else None,
        "left_dataset_hash": (
            left_repro.get("dataset_hash") if isinstance(left_repro, dict) else None
        ),
        "right_dataset_hash": (
            right_repro.get("dataset_hash") if isinstance(right_repro, dict) else None
        ),
        "differences": snapshot_diff(left, right),
    }
