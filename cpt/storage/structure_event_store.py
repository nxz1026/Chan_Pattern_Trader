"""结构事件流 ``public.cpt_structure_event`` 的读写（R26）。

**store 层不 commit** —— 事务边界归调用方（与 ``signal_event_store`` /
``dashboard_run_store`` / ``llm_call_store`` 同一约定）。

**当前状态从事件流派生**，不建状态表 —— 见迁移 SQL 的说明。派生走
:func:`cpt.domain.structure_events.state_from_event`，把 domain 类型接回来了：
R26 之前 ``StructureState`` 全仓零生产者、零消费者（见 progress-log R24 勘察）。

## 降级纪律：写侧吞、读侧抛（不是不一致，是按调用方分的）

同一个模块里两套相反的异常策略，这是**刻意**的，R27-2 才定下来：

- :func:`append_events` —— **吞**，返回 0。调用方是 recorder（快照构造热路径），
  写事件失败不该让整张快照构造失败。
- :func:`latest_events` / :func:`current_states` —— **吞**，返回 ``{}``。调用方是
  recorder，它自带 try/except 兜底；表不存在或连不通时降级成「无历史」，后续
  append 也会失败，整体仍是 best-effort。
- :func:`timeline` / :func:`recent_events` —— **抛**。调用方是 HTTP 读接口，
  降级成空元组会让接口把「DB 挂了」谎报成「没有事件」。

判据一句话：**吞掉异常会改变答案**时就得抛。写侧吞掉只影响「有没有落库」，
读侧吞掉会让「查不到」变成「没有」—— 前端无从区分。
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
    "recent_events",
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

    # gate: allow-silent: 写失败不阻断 —— 调用方
    # ``structure_event_recorder.record_structure_events`` 的 docstring 明确写了
    # 「``append_events`` 写失败被吞，但本函数照样把算出的事件返回」：
    # 结构事件是**观测产物**，算出来了就返回，存不进去不该让快照 500。
    # ⚠️ 代价：连接留在 aborted 态，调用方需自行 rollback。
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
    """某结构的事件时间线，**revision 升序**（最早在前）。

    :raises Exception: 读库失败**原样抛出**，不降级成空元组。

    ## 为什么这条读接口不 best-effort

    本模块的写侧（:func:`append_events`）刻意吞异常 —— 写事件失败不该让快照构造
    失败，那由调用方的 try/except 兜。但**读接口吞掉异常就是在撒谎**：DB 挂掉时
    返回空元组，HTTP 层只能报 ``available=true, count=0``，把「查不到」说成
    「没有」。前端无法区分这两者，而这两者的处置完全不同（前者该重试/告警，
    后者是正常结果）。

    降级义务因此上移到 HTTP 层：它捕获异常并回 ``available=false`` + reason，
    这才是前端能据以决策的形状。
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM public.cpt_structure_event "
            "WHERE structure_id = %s ORDER BY revision ASC LIMIT %s",
            (structure_id, max(1, min(int(limit), 500))),
        )
        rows = cur.fetchall() or ()
    return tuple(_row_to_dict(row) for row in rows)


#: :func:`recent_events` 的 limit 上限。与 :func:`timeline` 同一个量级：
#: 这张表只在**结构真的变了**时才追加一行，正常轮询绝大多数时候不写，
#: 所以几百行足够覆盖「最近发生了什么」，不必给无限。
_RECENT_LIMIT_CAP = 500


def recent_events(
    conn: Any,
    *,
    limit: int = 50,
    event_type: str | None = None,
    kind: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """最近的��构事件，**occurred_at 倒序**（最新在前），跨所有结构。

    :func:`timeline` 回答「**这一个**结构经历了什么」，本函数回答「**最近**
    发生了什么」—— 前者是 R27 UI 的详情面板，后者是它的列表页。

    :param event_type: 按事件类型过滤（``created`` / ``updated`` / …）。命中
        ``idx_cpt_structure_event_kind_level`` 的前导列。
    :param kind: 按结构类型过滤（``bi`` / ``fractal`` / ``zhongshu`` / …），
        取自 ``payload->>'kind'``。**无索引** —— 见下方取舍。
    :returns: 事件字典元组。
    :raises Exception: 读库失败原样抛出 —— 理由同 :func:`timeline`（读接口
        吞异常会把「查不到」谎报成「没有」），降级由 HTTP 层负责。

    ## 为什么 ``kind`` 过滤不加索引

    表只在结构真变了才追加一行，增长极慢（部署首日 725 行，绝大多数轮次零写入），
    而加一列就把「表不存 market / 不存 kind」这个 R26 就定下的口径破坏掉了 ——
    kind 本来就能从 payload 现抽。加索引前先问「真的慢吗」，答案是还没有。

    真慢了再说：届时加的是 ``CREATE INDEX ... ON ... ((payload->>'kind'))``，
    表达式索引，不需要新增列，迁移也只多一行。
    """
    sql = f"SELECT {', '.join(_COLUMNS)} FROM public.cpt_structure_event"
    where: list[str] = []
    params: list[Any] = []
    if event_type:
        where.append("event_type = %s")
        params.append(str(event_type))
    if kind:
        where.append("payload->>'kind' = %s")
        params.append(str(kind))
    if where:
        sql += " WHERE " + " AND ".join(where)
    # 倒序键用 (occurred_at, id) 而不是裸 occurred_at：同毫秒批量写入时
    # occurred_at 会并列，只按它排序会让同一批次的相对顺序随机漂移，
    # 翻页时可能漏行或重复行。id 是 bigserial 单调，追加它就稳定了。
    sql += " ORDER BY occurred_at DESC, id DESC LIMIT %s"
    params.append(max(1, min(int(limit), _RECENT_LIMIT_CAP)))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() or ()
    return tuple(_row_to_dict(row) for row in rows)
