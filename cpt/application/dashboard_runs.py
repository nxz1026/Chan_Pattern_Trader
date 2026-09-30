"""Research dashboard run index helpers.

**状态：已接线（R20，2026-09-30）**——``record_run`` 在 ``cpt/web/app.py`` 的
``_with_run_index`` 里被调，注入点刻意放在 **HTTP 响应层**而不是
``build_dashboard_snapshot_v2``。原因：运行历史是**进程级状态**，写进 snapshot
会让它变成非确定性的——同参数两次构建返回不等，直接打破
``tests/test_web_a_share.py::test_provider_caches_snapshot_within_ttl`` 守的缓存
语义（实测红），也会让 ``dashboard_compare`` 的字段级 diff 永远有一处差异。
**这是接线时实测踩到的，不是推演。**

处置取舍见 ``docs/pending-wiring.md``：不建表，因为 6 处生产调用点里 realtime
模式每 30s 一轮，落库即 2,880 行/天的低价值流水；realtime 进程由 systemd 常驻，
环形缓冲在真实运行周期内有效。

字段口径以 ``generated_at`` 为准（与 ``reproducibility.generated_at`` 一致）。
``record_run`` 额外写一份同值的 ``created_at``：前端曾读的是 ``created_at``，
两处不一致且因为 ``runs`` 恒为空而从未暴露，接线时必须同时消掉。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

#: 环形缓冲容量。realtime 模式 30s 一轮 → 50 条 ≈ 25 分钟运行历史。
RUN_RING_SIZE = 50

#: 进程内运行历史。**刻意不落库**（理由见模块 docstring）。
_RUN_RING: deque[dict[str, Any]] = deque(maxlen=RUN_RING_SIZE)


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


def record_run(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """把一次 snapshot 记进进程内环形缓冲，返回最新（或本次）那一条。

    **去重**：``(run_id, dataset_hash)`` 与最后一条相同就**不追加**。调用点在 HTTP
    响应出口，而 realtime 模式下 30s 内可能有十几次请求命中同一份缓存 snapshot ——
    不去重会把「一次运行」记成十几条，运行索引立刻失去意义。

    ``created_at`` 与 ``generated_at`` **同值双写**：前端 ``dashboard.js`` 读的是
    ``created_at``，本模块一直出的是 ``generated_at``。因为 ``runs`` 长期恒为
    ``[]``、前端循环体从未执行，这个不一致一直没暴露——接线的第一件事就是消掉它。

    时间戳**兜底**：``reproducibility.generated_at`` 来自
    ``runtime.generated_at``，而 A 股主看板的 runtime 不传该键（只有 ``as_of_ms``）
    → 不兜底的话运行索引里 A 股那条永远空白，而 A 股恰恰是用户主要看的面板。
    兜底顺序：``runtime.generated_at`` → ``runtime.as_of_ms`` → 当前时间。
    """
    (entry,) = build_run_index([dict(snapshot)])
    row = dict(entry)
    if row.get("generated_at") is None:
        runtime = snapshot.get("runtime") or {}
        fallback = runtime.get("generated_at") or runtime.get("as_of_ms")
        row["generated_at"] = fallback if fallback is not None else time.time() * 1000
    row["created_at"] = row.get("generated_at")
    if _RUN_RING and _same_run(_RUN_RING[-1], row):
        return dict(_RUN_RING[-1])
    _RUN_RING.append(row)
    return row


def _same_run(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (left.get("run_id"), left.get("dataset_hash")) == (
        right.get("run_id"),
        right.get("dataset_hash"),
    )


def recent_runs(limit: int | None = None) -> tuple[dict[str, Any], ...]:
    """最近若干次运行，**最新在前**（运行索引按时间倒序读）。

    注意与 :func:`build_run_index` 的顺序相反：后者按 ``generated_at`` **升序**
    排序（deterministic browser index，有测试锁定），运行索引面板要的是「最新的
    在最上面」。
    """
    if limit is None or limit >= len(_RUN_RING):
        return tuple(reversed(_RUN_RING))
    return tuple(reversed(list(_RUN_RING)[-limit:]))


def clear_runs() -> None:
    """清空环形缓冲（测试与运维用）。"""
    _RUN_RING.clear()
