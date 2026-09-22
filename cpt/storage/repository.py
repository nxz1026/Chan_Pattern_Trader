"""SQLite 读写接口（标准库 ``sqlite3``，不引入 ORM）。

``Repository`` 是纯协议（Protocol），供测试/mock 与上层依赖注入使用；
``SQLiteRepository`` 是唯一的具体实现，通过标准库 ``sqlite3`` 直连文件或
``:memory:`` 内存库。

存储层只依赖领域层（``cpt.domain``）与同包的 schema 定义（``cpt.storage.models``），
不反向依赖 engine/application/adapters/llm 等上层。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from cpt.domain.models import CanonicalBar, Signal, StructureEvent, StructureState
from cpt.storage.models import ddl_statements

__all__ = ["Repository", "SQLiteRepository"]


def _now_ms() -> int:
    """当前 Unix 毫秒时间戳，用于 ``updated_at`` 等写入字段。"""
    return int(time.time() * 1000)


def _dump_json(value: object) -> str:
    """序列化为 JSON 字符串；tuple 会变成数组，读出时用 ``_load_tuple`` 还原。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_tuple(raw: str) -> tuple[str, ...]:
    """把 JSON 数组字符串还原为字符串元组。"""
    loaded = json.loads(raw)
    return tuple(str(x) for x in loaded)


def _load_dict(raw: str) -> dict[str, object]:
    """把 JSON 对象字符串还原为 dict。"""
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError(f"expected JSON object, got {type(loaded).__name__}")
    return loaded


class Repository(Protocol):
    """仓储接口协议；不导入具体 SQLite 实现。"""

    def init_schema(self) -> None: ...

    def upsert_raw_bars(
        self,
        bars: Sequence[CanonicalBar],
        *,
        symbol: str,
        interval_minutes: int,
        fetched_at: int,
    ) -> int: ...

    def load_raw_bars(
        self,
        *,
        symbol: str,
        interval_minutes: int,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[CanonicalBar]: ...

    def upsert_structure_state(self, state: StructureState) -> None: ...

    def load_structure_state(self, structure_id: str) -> StructureState | None: ...

    def append_structure_event(self, event: StructureEvent) -> int: ...

    def list_structure_events(self, structure_id: str) -> list[StructureEvent]: ...

    def upsert_signal(self, signal: Signal) -> None: ...

    def load_signal(self, signal_id: str) -> Signal | None: ...


@dataclass(frozen=True, slots=True)
class SQLiteRepository:
    """基于标准库 ``sqlite3`` 的仓储实现。

    实例持有单一持久连接；``:memory:`` 内存库依赖该连接存活（连接关闭即销毁），
    因此不能在每次操作时新开连接。
    """

    path: str  # SQLite 文件路径，":memory:" 表示内存库
    _conn: sqlite3.Connection = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("path 必须是非空字符串")
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        object.__setattr__(self, "_conn", conn)

    def _connect(self) -> sqlite3.Connection:
        return self._conn

    # -- schema ----------------------------------------------------------------

    def init_schema(self) -> None:
        # 不使用 `with`，因为内存库的 self._conn 在 with 退出时会被关闭。
        conn = self._conn
        for stmt in ddl_statements():
            conn.execute(stmt)
        conn.commit()

    # -- raw bars --------------------------------------------------------------

    def upsert_raw_bars(
        self,
        bars: Sequence[CanonicalBar],
        *,
        symbol: str,
        interval_minutes: int,
        fetched_at: int,
    ) -> int:
        rows = [
            (
                symbol,
                interval_minutes,
                b.open_time,
                b.close_time,
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.quote_volume,
                b.trade_count,
                b.taker_buy_base_volume,
                b.taker_buy_quote_volume,
                int(b.is_closed),
                fetched_at,
            )
            for b in bars
        ]
        if not rows:
            return 0
        sql = """\
        INSERT OR REPLACE INTO raw_bars (
            symbol, interval_minutes, open_time, close_time,
            open, high, low, close, volume, quote_volume, trade_count,
            taker_buy_base_volume, taker_buy_quote_volume, is_closed, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._connect() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def load_raw_bars(
        self,
        *,
        symbol: str,
        interval_minutes: int,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[CanonicalBar]:
        sql = """\
        SELECT open_time, close_time, open, high, low, close, volume,
               quote_volume, trade_count, taker_buy_base_volume,
               taker_buy_quote_volume, is_closed
        FROM raw_bars
        WHERE symbol = ? AND interval_minutes = ?
        """
        params: list[object] = [symbol, interval_minutes]
        if start_ms is not None:
            sql += " AND open_time >= ?"
            params.append(start_ms)
        if end_ms is not None:
            sql += " AND open_time < ?"
            params.append(end_ms)
        sql += " ORDER BY open_time ASC"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            CanonicalBar(
                open_time=row["open_time"],
                close_time=row["close_time"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                quote_volume=row["quote_volume"],
                trade_count=row["trade_count"],
                taker_buy_base_volume=row["taker_buy_base_volume"],
                taker_buy_quote_volume=row["taker_buy_quote_volume"],
                is_closed=bool(row["is_closed"]),
            )
            for row in rows
        ]

    # -- structure states -------------------------------------------------------

    def upsert_structure_state(self, state: StructureState) -> None:
        sql = """\
        INSERT OR REPLACE INTO structure_states (
            structure_id, level, kind, direction, start_time, end_time,
            status, revision, first_seen_at, confirmed_at, invalidated_at,
            source_ids, payload, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            state.id,
            state.level,
            state.kind,
            state.direction,
            state.start_time,
            state.end_time,
            state.status,
            state.revision,
            state.first_seen_at,
            state.confirmed_at,
            state.invalidated_at,
            _dump_json(list(state.source_ids)),
            _dump_json({}),
            _now_ms(),
        )
        with self._connect() as conn:
            conn.execute(sql, params)

    def load_structure_state(self, structure_id: str) -> StructureState | None:
        sql = """\
        SELECT structure_id, level, kind, direction, start_time, end_time,
               status, revision, first_seen_at, confirmed_at, invalidated_at,
               source_ids
        FROM structure_states
        WHERE structure_id = ?
        """
        with self._connect() as conn:
            row = conn.execute(sql, (structure_id,)).fetchone()
        if row is None:
            return None
        return StructureState(
            id=row["structure_id"],
            level=row["level"],
            kind=row["kind"],
            direction=row["direction"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            status=row["status"],
            revision=row["revision"],
            first_seen_at=row["first_seen_at"],
            confirmed_at=row["confirmed_at"],
            invalidated_at=row["invalidated_at"],
            source_ids=_load_tuple(row["source_ids"]),
        )

    # -- structure events --------------------------------------------------------

    def append_structure_event(self, event: StructureEvent) -> int:
        sql = """\
        INSERT INTO structure_events (
            structure_id, event_type, revision, payload, occurred_at
        ) VALUES (?, ?, ?, ?, ?)
        """
        params = (
            event.structure_id,
            event.event_type,
            event.revision,
            _dump_json(event.payload),
            event.occurred_at,
        )
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            lastrowid = cur.lastrowid
            assert lastrowid is not None  # INSERT 单行必然返回 rowid
            return lastrowid

    def list_structure_events(self, structure_id: str) -> list[StructureEvent]:
        sql = """\
        SELECT event_type, structure_id, revision, payload, occurred_at
        FROM structure_events
        WHERE structure_id = ?
        ORDER BY occurred_at ASC, event_id ASC
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (structure_id,)).fetchall()
        return [
            StructureEvent(
                event_type=row["event_type"],
                structure_id=row["structure_id"],
                revision=row["revision"],
                payload=_load_dict(row["payload"]),
                occurred_at=row["occurred_at"],
            )
            for row in rows
        ]

    # -- signals ------------------------------------------------------------------

    def upsert_signal(self, signal: Signal) -> None:
        sql = """\
        INSERT OR REPLACE INTO signals (
            signal_id, level, signal_type, status, structure_id, center_ids,
            divergence_status, alert_time, candidate_time, confirmed_time,
            invalidated_time, price, source_revision, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            signal.signal_id,
            signal.level,
            signal.signal_type,
            signal.status,
            signal.structure_id,
            _dump_json(list(signal.center_ids)),
            signal.divergence_status,
            signal.alert_time,
            signal.candidate_time,
            signal.confirmed_time,
            signal.invalidated_time,
            signal.price,
            signal.source_revision,
            _now_ms(),
        )
        with self._connect() as conn:
            conn.execute(sql, params)

    def load_signal(self, signal_id: str) -> Signal | None:
        sql = """\
        SELECT signal_id, level, signal_type, status, structure_id, center_ids,
               divergence_status, alert_time, candidate_time, confirmed_time,
               invalidated_time, price, source_revision
        FROM signals
        WHERE signal_id = ?
        """
        with self._connect() as conn:
            row = conn.execute(sql, (signal_id,)).fetchone()
        if row is None:
            return None
        return Signal(
            signal_id=row["signal_id"],
            level=row["level"],
            signal_type=row["signal_type"],
            status=row["status"],
            structure_id=row["structure_id"],
            center_ids=_load_tuple(row["center_ids"]),
            divergence_status=row["divergence_status"],
            alert_time=row["alert_time"],
            candidate_time=row["candidate_time"],
            confirmed_time=row["confirmed_time"],
            invalidated_time=row["invalidated_time"],
            price=row["price"],
            source_revision=row["source_revision"],
        )
