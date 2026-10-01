"""Research dashboard run index helpers.

**状态：已接线（R20，2026-09-30）**——``record_run`` 在 ``cpt/web/app.py`` 的
``_with_run_index`` 里被调，注入点刻意放在 **HTTP 响应层**而不是
``build_dashboard_snapshot_v2``。原因：运行历史是**进程级状态**，写进 snapshot
会让它变成非确定性的——同参数两次构建返回不等，直接打破
``tests/test_web_a_share.py::test_provider_caches_snapshot_within_ttl`` 守的缓存
语义（实测红），也会让 ``dashboard_compare`` 的字段级 diff 永远有一处差异。
**这是接线时实测踩到的，不是推演。**

**ring 与 PG 的分工（R20 → R23 修订）**：R20 当时的结论是「不建表」——6 处生产
调用点里 realtime 模式每 30s 一轮，落库即 2,880 行/天的低价值流水，且 realtime
进程由 systemd 常驻、环形缓冲在真实运行周期内有效。**这个结论在 R23 被推翻**：
用户诉求是「跨重启可比」（2026-09-30），而 ring 是进程级的，重启即空，
``/compare`` 与 ``/multi-run`` 只能返回 ``run_body_unavailable``。

现在的处置是**两套并存**，不是替代：

- **ring 留 hot-path**：30s 内同 ``(run_id, dataset_hash)`` 命中同一份缓存
  snapshot 时走快路径，**连 DB 都不碰**（见 :func:`record_run` 的 ``on_recorded``）。
- **表留 cold-path**：``public.cpt_dashboard_run``（R23 建表）记跨重启历史，
  由 ``cpt/storage/dashboard_run_store.py`` 负责读写。

**落库量并没有变少**：fast path 命中根本不写库，30s 一轮只落 1 行而非「十几次
请求 × 每请求一行」——这正是 R20 当时担心的放大问题，而它并不存在。
处置记录见 ``docs/pending-wiring.md``。

字段口径以 ``generated_at`` 为准（与 ``reproducibility.generated_at`` 一致）。
``record_run`` 额外写一份同值的 ``created_at``：前端曾读的是 ``created_at``，
两处不一致且因为 ``runs`` 恒为空而从未暴露，接线时必须同时消掉。
"""

from __future__ import annotations

import copy
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

_LOG = logging.getLogger(__name__)

#: 环形缓冲容量。realtime 模式 30s 一轮 → 50 条 ≈ 25 分钟运行历史。
RUN_RING_SIZE = 50

#: 单份快照本体的序列化体积上限（字节）。
#:
#: 取 4MB 的依据：BTCUSDT 日线 40 根的量级在几十 KB，多品种/多周期
#: 合成快照也只是几百 KB；4MB 是「明显异常」的分界（例如调用方误把整段
#: 原始行情或全量指标矩阵塞进 snapshot）。超过它仍照收不误，只是不存本体，
#: 避免一个病态请求把常驻 realtime 进程的内存吃穿。
RUN_BODY_MAX_BYTES = 4_000_000

#: 进程内运行历史（**索引行**）。**刻意不落库**（理由见模块 docstring）。
_RUN_RING: deque[dict[str, Any]] = deque(maxlen=RUN_RING_SIZE)

#: 与 ``_RUN_RING`` **一一对应**的快照本体缓冲：同一位置必须属于同一 run，
#: 两个 deque 的 ``maxlen`` 相同、且只在 :func:`record_run` 里同时 append，
#: 所以它们的驱逐节奏严格一致，不会错位。
#:
#: 本体是 ``copy.deepcopy`` 的独立副本：快照对象在调用点会被后续请求复用/
#: 覆盖，存引用等于存了一个会变的视图。
#:
#: 内存上界：``RUN_RING_SIZE × RUN_BODY_MAX_BYTES ≈ 50 × 4MB = 200MB``
#: 序列化体积；Python 对象图还有常量倍数的开销，属于本进程的显式预算。
_RUN_BODIES: deque[dict[str, Any] | None] = deque(maxlen=RUN_RING_SIZE)


def _snapshot_body(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    """深拷一份快照本体；序列化超限则返回 ``None``（**不抛**）。

    序列化本身失败（不可 JSON 化的诡异对象）按超限同等处置：宁可没有本体，
    也不能让一次 HTTP 记账把请求打挂。
    """
    try:
        size = len(json.dumps(snapshot, default=str).encode("utf-8"))
    except Exception:  # noqa: BLE001 —— 记账是旁路，绝不允许反噬主流程
        _LOG.warning("运行快照本体序列化失败，仅保留索引行（不存本体）", exc_info=True)
        return None
    if size > RUN_BODY_MAX_BYTES:
        _LOG.warning(
            "运行快照本体超过体积闸门，仅保留索引行：%d 字节 > %d 字节上限（约 %.1f MB）",
            size,
            RUN_BODY_MAX_BYTES,
            size / 1_000_000,
        )
        return None
    return copy.deepcopy(dict(snapshot))


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


def record_run(
    snapshot: Mapping[str, Any],
    *,
    on_recorded: Callable[[dict[str, Any], dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
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

    :param on_recorded: R23 新增。**只在真正 append 之后**回调
        ``(row, body)``，供调用方把这次命中同步双写进
        ``public.cpt_dashboard_run``（见 ``cpt.storage.dashboard_run_store``）。
        去重命中路径**不回调**——realtime 30s 一轮里可能有十几次请求命中同一份
        缓存 snapshot，快路径每次都打一次 DB 是纯浪费（去重语义见上）。
        本模块保持零 DB 依赖：回调由调用方（``cpt/web/app.py``）注入，
        这里不 import psycopg、也不吞异常——异常交给调用方 best-effort。
    """
    (entry,) = build_run_index([dict(snapshot)])
    row = dict(entry)
    if row.get("generated_at") is None:
        runtime = snapshot.get("runtime") or {}
        fallback = runtime.get("generated_at") or runtime.get("as_of_ms")
        row["generated_at"] = fallback if fallback is not None else time.time() * 1000
    row["created_at"] = row.get("generated_at")
    if _RUN_RING and _same_run(_RUN_RING[-1], row):
        # 去重命中：**本体也不动**。命中意味着这是同一份快照被重复请求，
        # 已存的本体就是它，绝不能用新的一份去覆盖（可能是不同的 runtime 包装）。
        # 同样**不触发 on_recorded**——见参数说明。
        return dict(_RUN_RING[-1])
    # 两个 deque 必须**同步 append**：这里是唯一的写入点，顺序不可调换、
    # 中间不能有任何会抛的语句，否则索引行与本体就错位了。
    body = _snapshot_body(snapshot)
    _RUN_RING.append(row)
    _RUN_BODIES.append(body)
    if on_recorded is not None:
        # 放在两个 append **之后**：ring 是主路径，必须先落住；DB 双写是旁路。
        on_recorded(row, body)
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


def run_body(run_id: str) -> dict[str, Any] | None:
    """取某个 run 的**快照本体**（给 ``compare_snapshots`` / ``align_runs`` 用）。

    找不到、或该 run 没存本体（体积超限 / 已被环形缓冲挤出）都返回 ``None``。
    同一个 ``run_id`` 可能有多条（``dataset_hash`` 不同），取**最新**的那份。

    返回**深拷**：本体是进程级状态，调用方（``dashboard_compare`` 等）若就地
    改动会把环里的那份也改坏，下一轮比较就拿到被污染的基线。
    """
    for row, body in zip(reversed(_RUN_RING), reversed(_RUN_BODIES), strict=False):
        if row.get("run_id") == run_id:
            return copy.deepcopy(body) if body is not None else None
    return None


def find_run(run_id: str) -> dict[str, Any] | None:
    """取某个 run 的**索引行**（从中拿 ``dataset_hash`` 等口径），无则 ``None``。

    与 :func:`run_body` 一样取最新的一条；返回的是副本，调用方改不到环里。
    """
    for row in reversed(_RUN_RING):
        if row.get("run_id") == run_id:
            return dict(row)
    return None


def clear_runs() -> None:
    """清空环形缓冲（测试与运维用）。索引行与本体**必须一起清**，否则错位。"""
    _RUN_RING.clear()
    _RUN_BODIES.clear()
