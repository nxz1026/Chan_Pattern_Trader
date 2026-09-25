"""``cpt.adapters.a_share_pool`` 测试。

DB 部分用 mock（不依赖 psycopg），自选 JSON 部分用 ``tmp_path``。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest
from cpt.adapters.a_share_pool import (
    LimitPoolMark,
    WatchlistError,
    WatchlistStore,
    fetch_hot_pool,
    fetch_limit_pool_marks,
)

# --------------------------------------------------------------------------- #
# Mock DB
# --------------------------------------------------------------------------- #


@dataclass
class FakeCursor:
    rows: list[tuple]
    filter: dict = field(default_factory=dict)
    executed: list = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        s = sql.strip().lower()
        if s.startswith("select max(date) from public.hot_rank"):
            self.filter["hot_max"] = max(
                (r[1] for r in self.rows if r[0] == "hot_rank_date"), default=None
            )
        elif s.startswith("select code, rank from public.hot_rank"):
            self.filter["hot"] = [
                r for r in self.rows if r[0] == "hot_rank" and r[1] == self.filter.get("hot_max")
            ]
        elif s.startswith("select max(date) from public.ladder_day"):
            self.filter["lad_max"] = max(
                (r[1] for r in self.rows if r[0] == "ladder_date"), default=None
            )
        elif s.startswith("select code, cont_days from public.ladder_day"):
            self.filter["lad"] = [
                r for r in self.rows if r[0] == "ladder" and r[1] == self.filter.get("lad_max")
            ]
        elif s.startswith("select max(date) from public.limit_pool_em"):
            self.filter["lim_max"] = max(
                (r[1] for r in self.rows if r[0] == "limit_pool_date"), default=None
            )
        elif s.startswith("select code, name, cont_days_em, pool_type"):
            self.filter["lim"] = [r for r in self.rows if r[0] == "limit_pool"]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        s = self.executed[-1][0].strip().lower()
        if s.startswith("select max(date) from public.hot_rank"):
            return (self.filter["hot_max"],)
        if s.startswith("select max(date) from public.ladder_day"):
            return (self.filter["lad_max"],)
        if s.startswith("select max(date) from public.limit_pool_em"):
            return (self.filter["lim_max"],)
        raise AssertionError(f"Unexpected fetchone: {s}")

    def fetchall(self):
        s = self.executed[-1][0].strip().lower()
        if s.startswith("select code, rank"):
            return [(r[2], r[3]) for r in self.filter["hot"]]
        if s.startswith("select code, cont_days"):
            return [(r[2], r[3]) for r in self.filter["lad"]]
        if s.startswith("select code, name, cont_days_em, pool_type"):
            return [(r[1], r[2], r[3], r[4]) for r in self.filter["lim"]]
        raise AssertionError(f"Unexpected fetchall: {s}")


@dataclass
class FakeConn:
    rows: list[tuple]

    def cursor(self):
        return FakeCursor(rows=self.rows)


# --------------------------------------------------------------------------- #
# fetch_hot_pool
# --------------------------------------------------------------------------- #


def test_fetch_hot_pool_unions_top100_and_ladder_day():
    # 行格式：(table_marker, date, code, value)
    rows = [
        # hot_rank 最新日 top100（用前 3 只）
        ("hot_rank_date", date(2026, 9, 21)),
        ("hot_rank", date(2026, 9, 21), "000002", 5),
        ("hot_rank", date(2026, 9, 21), "000504", 12),
        ("hot_rank", date(2026, 9, 21), "600519", 88),
        # ladder_day 最新日 cont_days>=2
        ("ladder_date", date(2026, 9, 21)),
        ("ladder", date(2026, 9, 21), "000504", 3),  # 与 hot_rank 重叠
        ("ladder", date(2026, 9, 21), "001234", 2),
    ]
    conn = FakeConn(rows=rows)
    entries = fetch_hot_pool(conn)
    codes = [e.code for e in entries]
    # 000504 在两源都有 → hot_rank 优先（带 rank 字段）
    # 000002 / 600519 仅 hot_rank；001234 仅 ladder_day
    assert set(codes) == {"000002", "000504", "600519", "001234"}
    rank_504 = next(e for e in entries if e.code == "000504")
    assert rank_504.source == "hot_rank"
    assert rank_504.rank == 12
    rank_1234 = next(e for e in entries if e.code == "001234")
    assert rank_1234.source == "ladder_day"
    assert rank_1234.cont_days == 2


def test_fetch_hot_pool_sorts_by_rank_then_cont_days():
    rows = [
        ("hot_rank_date", date(2026, 9, 21)),
        ("hot_rank", date(2026, 9, 21), "000002", 50),
        ("hot_rank", date(2026, 9, 21), "600519", 5),
        ("ladder_date", date(2026, 9, 21)),
        ("ladder", date(2026, 9, 21), "999999", 5),  # 仅连板，cont_days=5
    ]
    conn = FakeConn(rows=rows)
    entries = fetch_hot_pool(conn)
    # 期望顺序：600519(rank=5) → 000002(rank=50) → 999999(无 rank，按 code 排序)
    assert [e.code for e in entries] == ["600519", "000002", "999999"]


def test_fetch_hot_pool_empty_when_no_data():
    """DB 全空时返回空 list（不抛错，调用方自己处理）。"""
    conn = FakeConn(rows=[])
    entries = fetch_hot_pool(conn)
    assert entries == []


def test_fetch_hot_pool_limit_keeps_top_ranks():
    """``limit`` 取排序后的前 N 条（A 股下拉只要 Top5，见 a_share_routes）。"""
    rows = [
        ("hot_rank_date", date(2026, 9, 21)),
        ("hot_rank", date(2026, 9, 21), "000002", 50),
        ("hot_rank", date(2026, 9, 21), "600519", 5),
        ("hot_rank", date(2026, 9, 21), "000504", 12),
        ("ladder_date", date(2026, 9, 21)),
        ("ladder", date(2026, 9, 21), "999999", 5),
    ]
    conn = FakeConn(rows=rows)
    assert [e.code for e in fetch_hot_pool(conn, limit=2)] == ["600519", "000504"]
    # limit=None 保留全量能力（将来"展开全部"要用）
    assert len(fetch_hot_pool(conn, limit=None)) == 4
    assert len(fetch_hot_pool(conn)) == 4


# --------------------------------------------------------------------------- #
# fetch_limit_pool_marks
# --------------------------------------------------------------------------- #


def test_fetch_limit_pool_marks_returns_only_today():
    rows = [
        ("limit_pool_date", date(2026, 9, 21)),
        ("limit_pool", "000001", "万科A", 1, "limit_up"),
        ("limit_pool", "000002", "万 科Ａ", 2, "limit_up"),
    ]
    conn = FakeConn(rows=rows)
    marks = fetch_limit_pool_marks(conn)
    assert len(marks) == 2
    assert all(isinstance(m, LimitPoolMark) for m in marks)
    assert marks[0].code == "000001"


def test_fetch_limit_pool_marks_with_explicit_date():
    rows = [
        ("limit_pool_date", date(2026, 9, 21)),
        ("limit_pool", "000001", "万科A", 1, "limit_up"),
    ]
    conn = FakeConn(rows=rows)
    marks = fetch_limit_pool_marks(conn, trade_date="2026-09-21")
    assert marks[0].trade_date == "2026-09-21"


# --------------------------------------------------------------------------- #
# WatchlistStore
# --------------------------------------------------------------------------- #


def test_watchlist_add_and_list(tmp_path: pathlib.Path):
    path = tmp_path / "watchlist.json"
    store = WatchlistStore(path)
    store.add("600519", "A")
    store.add("000001", "A")
    items = store.list()
    assert len(items) == 2
    assert {i.code for i in items} == {"600519", "000001"}


def test_watchlist_add_is_idempotent(tmp_path: pathlib.Path):
    path = tmp_path / "watchlist.json"
    store = WatchlistStore(path)
    store.add("600519", "A")
    store.add("600519", "A")
    store.add("600519", "A")
    assert len(store.list()) == 1


def test_watchlist_remove_returns_true_when_existing(tmp_path: pathlib.Path):
    path = tmp_path / "watchlist.json"
    store = WatchlistStore(path)
    store.add("600519", "A")
    assert store.remove("600519", "A") is True
    assert len(store.list()) == 0


def test_watchlist_remove_returns_false_when_missing(tmp_path: pathlib.Path):
    path = tmp_path / "watchlist.json"
    store = WatchlistStore(path)
    assert store.remove("999999", "A") is False


def test_watchlist_remove_does_not_affect_other_market(tmp_path: pathlib.Path):
    path = tmp_path / "watchlist.json"
    store = WatchlistStore(path)
    store.add("BTCUSDT", "crypto")
    store.add("600519", "A")
    store.remove("600519", "A")
    items = store.list()
    assert len(items) == 1
    assert items[0].code == "BTCUSDT"
    assert items[0].market == "crypto"


def test_watchlist_persists_added_at(tmp_path: pathlib.Path):
    path = tmp_path / "watchlist.json"
    store = WatchlistStore(path)
    e = store.add("600519", "A")
    assert e.added_at.endswith("+00:00") or e.added_at.endswith("Z")


def test_watchlist_corrupt_file_raises(tmp_path: pathlib.Path):
    path = tmp_path / "watchlist.json"
    path.write_text("not json {", encoding="utf-8")
    store = WatchlistStore(path)
    with pytest.raises(WatchlistError, match="自选文件读取失败"):
        store.list()


def test_watchlist_creates_parent_directory(tmp_path: pathlib.Path):
    path = tmp_path / "nested" / "dir" / "watchlist.json"
    store = WatchlistStore(path)
    store.add("600519", "A")
    assert path.exists()
