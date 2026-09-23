from __future__ import annotations

import sqlite3

import pytest
from cpt.storage.repository import SQLiteRepository


def test_signals_reject_ghost_structure(tmp_path) -> None:
    with SQLiteRepository(str(tmp_path / "signals.db")) as repo:
        repo.init_schema()
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            repo._conn.execute(
                "INSERT INTO signals (signal_id, level, signal_type, status, structure_id, "
                "center_ids, divergence_status, price, source_revision, updated_at) "
                "VALUES ('s1', 5, 'buy1', 'candidate', 'ghost', '[]', 'none', 1.0, 0, 0)"
            )


def test_existing_signals_schema_is_migrated(tmp_path) -> None:
    path = str(tmp_path / "legacy.db")
    with SQLiteRepository(path) as repo:
        repo.init_schema()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE signals RENAME TO signals__legacy")
    conn.execute(
        "CREATE TABLE signals (signal_id TEXT PRIMARY KEY, level INTEGER NOT NULL, "
        "signal_type TEXT NOT NULL, status TEXT NOT NULL, structure_id TEXT NOT NULL, "
        "center_ids TEXT NOT NULL, divergence_status TEXT NOT NULL, alert_time INTEGER, "
        "candidate_time INTEGER, confirmed_time INTEGER, invalidated_time INTEGER, "
        "price REAL NOT NULL, source_revision INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO signals SELECT * FROM signals__legacy")
    conn.execute("DROP TABLE signals__legacy")
    conn.commit()
    conn.close()
    with SQLiteRepository(path) as repo:
        repo.init_schema()
        foreign_keys = repo._conn.execute("PRAGMA foreign_key_list(signals)").fetchall()
        assert any(row[2] == "structure_states" for row in foreign_keys)
