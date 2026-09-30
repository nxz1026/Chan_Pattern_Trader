"""信号事件持久化：``public.cpt_signal_event`` append-only 事件流。

当前状态 = 同 ``signal_id`` 的最新事件。本模块负责：

1. ``load_previous_signal``：从事件流重建最新 ``Signal``，喂给
   ``assess_first_buy(previous=...)`` 做状态推进。
2. ``record_signal_event``：状态跃迁时 append 一条事件。同 status 不重复写
   （30s 轮询不产生垃圾行）。

写入层不直接依赖 ``cpt.domain.signal`` 的状态机逻辑——它只接收已经算好的
``Signal`` 对象，判断 ``status`` 是否变化后 append。状态怎么算、怎么推进，
是 ``cpt.domain.signal`` 与 ``cpt.application.first_buy_bridge`` 的职责。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from cpt.domain.models import Signal

_LOG = logging.getLogger(__name__)

__all__ = ["load_previous_signal", "record_signal_event", "SignalEventError"]


class SignalEventError(RuntimeError):
    """信号事件持久化失败。"""


def _ms_to_timestamptz(ms: int | None) -> datetime | None:
    """Unix 毫秒 → UTC ``datetime``；``None`` / ``<= 0`` 视为未知。"""
    if ms is None or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def _timestamptz_to_ms(dt: datetime | None) -> int | None:
    """UTC ``datetime`` → Unix 毫秒；``None`` → ``None``。"""
    if dt is None:
        return None
    return int(dt.timestamp() * 1000)


def _row_to_signal(row: tuple[Any, ...]) -> Signal:
    """从事件行重建 ``Signal``。

    列顺序必须与 ``load_previous_signal`` 的 SELECT 一致：
    ``signal_id, level, signal_type, status, structure_id, center_ids,
    divergence_status, alert_time, candidate_time, confirmed_time,
    invalidated_time, price, source_revision``
    """
    return Signal(
        signal_id=row[0],
        level=row[1],
        signal_type=row[2],
        status=row[3],
        structure_id=row[4],
        center_ids=tuple(row[5]) if row[5] else (),
        divergence_status=row[6],
        alert_time=_timestamptz_to_ms(row[7]),
        candidate_time=_timestamptz_to_ms(row[8]),
        confirmed_time=_timestamptz_to_ms(row[9]),
        invalidated_time=_timestamptz_to_ms(row[10]),
        price=row[11],
        source_revision=row[12],
    )


def load_previous_signal(conn: Any, signal_id: str) -> Signal | None:
    """加载 ``signal_id`` 的最新状态（最新事件），返回 ``Signal`` 或 ``None``。

    返回的 ``Signal`` 可直接作为 ``assess_first_buy(..., previous=...)`` 的入参。
    无事件 → ``None``（首次评估）。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT signal_id, level, signal_type, status, structure_id,
                          center_ids, divergence_status, alert_time, candidate_time,
                          confirmed_time, invalidated_time, price, source_revision
                   FROM public.cpt_signal_event
                   WHERE signal_id = %s
                   ORDER BY id DESC LIMIT 1""",
                (signal_id,),
            )
            row = cur.fetchone()
        return _row_to_signal(row) if row else None
    except Exception as exc:
        # 事件流不是硬依赖：读失败时降级为首次评估（previous=None），
        # 不把整个快照搞挂。但必须响亮地记日志，不能静默。
        _LOG.warning("加载信号历史失败 %s: %s", signal_id, exc)
        return None


def record_signal_event(
    conn: Any,
    signal: Signal,
    prev_status: str | None,
    code: str,
    event_time: int,
) -> bool:
    """记录信号事件。只在 ``status`` 变化时 append，返回 ``True`` 表示已写入。

    :param conn: psycopg 连接（不 commit，由调用方控制事务边界）。
    :param signal: 当前评估后的 ``Signal``。
    :param prev_status: 上一状态（``None`` = 首次评估）。
    :param code: A 股 6 位代码。
    :param event_time: 本次评估的事件时间（Unix 毫秒）。
    :returns: ``True`` 表示写了一条新事件；``False`` 表示 status 未变、跳过。
    """
    if signal.status == prev_status:
        return False

    transition_time = _ms_to_timestamptz(event_time)
    if transition_time is None:
        transition_time = datetime.now(UTC)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO public.cpt_signal_event
                     (signal_id, code, signal_type, level, structure_id,
                      prev_status, status, transition_time, price, source_revision,
                      center_ids, divergence_status, alert_time, candidate_time,
                      confirmed_time, invalidated_time)
                   VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s,%s)""",
                (
                    signal.signal_id,
                    code,
                    signal.signal_type,
                    signal.level,
                    signal.structure_id,
                    prev_status,
                    signal.status,
                    transition_time,
                    signal.price,
                    signal.source_revision,
                    list(signal.center_ids),
                    signal.divergence_status,
                    _ms_to_timestamptz(signal.alert_time),
                    _ms_to_timestamptz(signal.candidate_time),
                    _ms_to_timestamptz(signal.confirmed_time),
                    _ms_to_timestamptz(signal.invalidated_time),
                ),
            )
    except Exception as exc:
        _LOG.warning("记录信号事件失败 %s: %s", signal.signal_id, exc)
        raise SignalEventError(f"记录信号事件失败: {exc}") from exc

    return True
