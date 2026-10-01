"""结构事件流 ``public.cpt_structure_event`` 的读写（R26）。

**store 层不 commit** —— 事务边界归调用方（与 ``signal_event_store`` /
``dashboard_run_store`` / ``llm_call_store`` 同一约定）。

**当前状态从事件流派生**，不建状态表 —— 见迁移 SQL 的说明。派生走
:func:`cpt.domain.structure_events.state_from_event`，把 domain 类型接回来了：
R26 之前 ``StructureState`` 全仓零生产者、零消费者（见 progress-log R24 勘察）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from cpt.domain.models import StructureEvent, StructureState
from cpt.domain.structure_events import state_from_event

_LOG = logging.getLogger(__name__)

__all__ = [
    "append_events",
    "current_states",
    "latest_events",
    "timeline",
]

_COLUMNS = (
    "id",
    "structure_id",
    "event_type",
    "status",
    "revision",
    "payload",
    "occurred_at",
    "created_at",
)


def _ms(value: Any) -> Any:
    """``datetime`` → Unix 毫秒。列是 timestamptz，出参必须能进 json。"""
    return int(value.timestamp() * 1000) if hasattr(value, "timestamp") else value


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        name: _ms(value) if name in ("occurred_at", "created_at") else value
        for name, value in zip(_COLUMNS, row, strict=False)
    }


def append_events(conn: Any, events: Sequence[StructureEvent]) -> int:
    """追加一批事件，返回**真正写入**的条数。

    **空批次是 no-op，不碰存储** —— 这是热路径常态：每轮快照都会 diff 一次，
    而绝大多数轮次没有结构变化。发了 SQL 再回来什么都不写是纯浪费。

    :param conn: psycopg 连接（**不 commit**）。
    :returns: 写入条数。
    """
    if not events:
        return 0

    rows = [
        (
            ev.structure_id,
            ev.event_type,
            str(ev.payload.get("status", "forming")),
            ev.revision,
            json.dumps(ev.payload, ensure_ascii=False, default=str),
            ev.occurred_at,
        )
        for ev in events
    ]
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO public.cpt_structure_event "
                "(structure_id, event_type, status, revision, payload, occurred_at) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, to_timestamp(%s / 1000.0))",
                rows,
            )
            return len(rows)
    except Exception as exc:
        # best-effort：写事件失败不该让快照构造失败
        _LOG.warning("写入结构事件失败 %s 条: %s", len(rows), exc)
        return 0


def latest_events(conn: Any, structure_ids: Sequence[str]) -> dict[str, StructureEvent]:
    """取每个 ``structure_id`` 的**最新一条事件**。

    用 ``DISTINCT ON``（PG 的「每组取一行」），一次查询拿完，别在 Python 里
    拉全表再挑。

    :returns: ``structure_id -> StructureEvent``；不存在的 id **不在结果里**
        （区别于「存在但 payload 为空」——调用方据此判首次）。
    """
    ids = [str(i).strip() for i in structure_ids if str(i).strip()]
    if not ids:
        return {}

    sql = f"""
        SELECT DISTINCT ON (structure_id) {", ".join(_COLUMNS)}
          FROM public.cpt_structure_event
         WHERE structure_id = ANY(%s)
         ORDER BY structure_id, revision DESC
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (list(ids),))
            found = cur.fetchall() or ()
    except Exception as exc:
        _LOG.warning("读取结构最新事件失败 %s 个: %s", len(ids), exc)
        return {}

    out: dict[str, StructureEvent] = {}
    for row in found:
        record = _row_to_dict(row)
        payload = record.get("payload")
        out[record["structure_id"]] = StructureEvent(
            event_type=record["event_type"],
            structure_id=record["structure_id"],
            revision=int(record["revision"]),
            payload=payload if isinstance(payload, dict) else {},
            occurred_at=int(record["occurred_at"]),
        )
    return out


def current_states(conn: Any, structure_ids: Sequence[str]) -> dict[str, StructureState]:
    """从事件流派生「当前状态」。**``StructureState`` 的生产出口在这里。**"""
    return {sid: state_from_event(ev) for sid, ev in latest_events(conn, structure_ids).items()}


def timeline(conn: Any, structure_id: str, *, limit: int = 100) -> tuple[dict[str, Any], ...]:
    """某结构的事件时间线，**revision 升序**（最早在前）。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM public.cpt_structure_event "
                "WHERE structure_id = %s ORDER BY revision ASC LIMIT %s",
                (structure_id, max(1, min(int(limit), 500))),
            )
            rows = cur.fetchall() or ()
    except Exception as exc:
        _LOG.warning("读取结构时间线失败 %s: %s", structure_id, exc)
        return ()
    return tuple(_row_to_dict(row) for row in rows)
