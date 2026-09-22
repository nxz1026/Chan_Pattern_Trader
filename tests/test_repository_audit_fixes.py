"""审计报告 fix 回归测试。

覆盖审计报告"测试缺口"前 3 条与问题 1 的回归:

1. 外键真正生效——给不存在的 structure_id 插入事件应报错;
2. UPSERT 不改变 rowid——同一 bar upsert 两次,bar_id 应保持稳定;
3. normalized_bars.raw_bar_id 不悬空——upsert raw_bars 后引用仍有效。
"""

from __future__ import annotations

import sqlite3

import pytest
from cpt.domain.models import (
    CanonicalBar,
    StructureEvent,
    StructureState,
    make_canonical_bar,
)
from cpt.storage.repository import SQLiteRepository


def _bar(open_time: int, close: float = 100.0) -> CanonicalBar:
    return make_canonical_bar(
        open_time=open_time,
        close_time=open_time + 600,
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1.0,
    )


def test_foreign_keys_enabled() -> None:
    """PRAGMA foreign_keys 必须在每连接开启;不开启则幽灵事件落库。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    val = repo._conn.execute("PRAGMA foreign_keys").fetchone()
    assert val is not None and val[0] == 1, f"foreign_keys 应为 1, 实测 {val}"
    repo.close()


def test_ghost_event_insert_rejected_by_fk() -> None:
    """给不存在的 structure_id 插入事件应被外键拒绝(否则是幽灵事件)。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    ghost = StructureEvent(
        event_type="created",
        structure_id="does-not-exist",
        revision=1,
        payload={},
        occurred_at=1_700_000_000_000,
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.append_structure_event(ghost)
    repo.close()


def test_upsert_raw_bars_preserves_bar_id() -> None:
    """同一 bar upsert 两次,bar_id 保持稳定。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    bar = _bar(1000)
    repo.upsert_raw_bars([bar], symbol="BTCUSDT", interval_minutes=5, fetched_at=100)
    first_id = repo._conn.execute("SELECT bar_id FROM raw_bars WHERE symbol='BTCUSDT'").fetchone()[
        0
    ]

    # 二次 upsert 同一根 bar:fetched_at 改变,其他字段保持
    repo.upsert_raw_bars([bar], symbol="BTCUSDT", interval_minutes=5, fetched_at=200)
    second_id = repo._conn.execute("SELECT bar_id FROM raw_bars WHERE symbol='BTCUSDT'").fetchone()[
        0
    ]
    assert first_id == second_id, f"upsert 后 bar_id 应保持稳定, 但 {first_id} -> {second_id}"

    # fetched_at 已被更新(ON CONFLICT DO UPDATE 生效)
    fetched = repo._conn.execute(
        "SELECT fetched_at FROM raw_bars WHERE bar_id=?", (first_id,)
    ).fetchone()[0]
    assert fetched == 200
    repo.close()


def test_normalized_bars_reference_not_orphaned() -> None:
    """upsert raw_bars 不会让 normalized_bars.raw_bar_id 悬空。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    bar = _bar(1000)
    repo.upsert_raw_bars([bar], symbol="BTCUSDT", interval_minutes=5, fetched_at=100)

    # 模拟 normalized_bars 写入(指向 raw_bars.bar_id)
    raw_id = repo._conn.execute("SELECT bar_id FROM raw_bars WHERE symbol='BTCUSDT'").fetchone()[0]
    repo._conn.execute(
        "INSERT INTO normalized_bars (raw_bar_id, direction, bar_hash) VALUES (?, ?, ?)",
        (raw_id, 1, "deadbeef"),
    )
    repo._conn.commit()

    # 再次 upsert 同一根 bar——如果用 INSERT OR REPLACE 会换 bar_id,这里引用会悬空
    repo.upsert_raw_bars([bar], symbol="BTCUSDT", interval_minutes=5, fetched_at=300)

    # 引用仍存在:外键约束下应该没孤儿
    orphans = repo._conn.execute("""
        SELECT COUNT(*) FROM normalized_bars n
        LEFT JOIN raw_bars r ON n.raw_bar_id = r.bar_id
        WHERE r.bar_id IS NULL
    """).fetchone()[0]
    assert orphans == 0, f"应有 0 个孤儿引用,实测 {orphans}"
    repo.close()


def test_repository_context_manager() -> None:
    """SQLiteRepository 支持 with 语句,退出时自动 close。"""
    with SQLiteRepository(path=":memory:") as repo:
        repo.init_schema()
        repo.upsert_raw_bars([_bar(1000)], symbol="BTCUSDT", interval_minutes=5, fetched_at=1)
        assert len(repo.load_raw_bars(symbol="BTCUSDT", interval_minutes=5)) == 1
    # 退出后连接已关闭
    with pytest.raises(sqlite3.ProgrammingError):
        repo._conn.execute("SELECT 1")


def test_append_structure_event_returns_rowid() -> None:
    """append_structure_event 应返回 rowid;即使 assert 被剥离也正确 raise。"""
    repo = SQLiteRepository(path=":memory:")
    repo.init_schema()
    repo.upsert_structure_state(
        StructureState(
            id="p1",
            level=5,
            kind="bi",
            direction=1,
            start_time=1000,
            end_time=2000,
            status="forming",
            revision=1,
            first_seen_at=1_700_000_000_000,
            confirmed_at=None,
            invalidated_at=None,
            source_ids=("f1",),
        )
    )
    eid = repo.append_structure_event(
        StructureEvent(
            event_type="created",
            structure_id="p1",
            revision=1,
            payload={"k": "v"},
            occurred_at=1_700_000_000_000,
        )
    )
    assert isinstance(eid, int) and eid > 0
    repo.close()
