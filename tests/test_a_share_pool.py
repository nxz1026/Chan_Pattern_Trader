"""``cpt.adapters.a_share_pool`` 测试。

DB 部分用 mock（不依赖 psycopg），自选 JSON 部分用 ``tmp_path``。
"""

from __future__ import annotations

import json
import logging
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
    """文件损坏 ⇒ 抛 ``WatchlistError``，**且不修改文件**。

    R45：文案从「自选文件读取失败」细分成「已损坏…**未做任何修改**」，
    所以断言改为匹配**契约**（异常类型 + 「未做任何修改」）而不是整句措辞 ——
    措辞会变，契约不会。
    """
    path = tmp_path / "watchlist.json"
    path.write_text("not json {", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    store = WatchlistStore(path)
    with pytest.raises(WatchlistError, match="未做任何修改"):
        store.list()
    assert path.read_text(encoding="utf-8") == before, "读取失败不该改动文件"


def test_watchlist_corrupt_file_does_not_wipe_entries(tmp_path: pathlib.Path):
    """R45 核心回归：文件损坏后 ``add()`` **不得**静默清空原有条目。

    原实现 ``except json.JSONDecodeError: data = []`` 之后继续写 ⇒
    用户在损坏文件上加一票，**原有全部条目被静默覆盖**，无异常无日志。
    """
    path = tmp_path / "watchlist.json"
    store = WatchlistStore(path)
    store.add("600519", "a_share")
    store.add("000001", "a_share")

    path.write_text('[{"code": "600519", "market": "a_sh', encoding="utf-8")  # 截断
    with pytest.raises(WatchlistError, match="未做任何修改"):
        store.add("000002", "a_share")

    path.write_text("not json {", encoding="utf-8")
    with pytest.raises(WatchlistError):
        store.remove("600519", "a_share")


def test_watchlist_creates_parent_directory(tmp_path: pathlib.Path):
    path = tmp_path / "nested" / "dir" / "watchlist.json"
    store = WatchlistStore(path)
    store.add("600519", "A")
    assert path.exists()


def test_module_imports_without_fcntl(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``import fcntl`` 曾经无条件写在模块顶层。

    于是整个 ``a_share_pool`` 在 Windows 上 ImportError —— 而它同时被
    ``/api/dashboard/a-share/pool`` 与自选路由依赖：不是「自选用不了」，是
    **这个模块连带依赖它的路由一起起不来**。

    这里按「顶层 import fcntl 会失败」重放一次导入：锁退化为无操作，但
    原子替换 + JSON 解析都不依赖它，读写照常。
    """
    import builtins
    import importlib

    import cpt.adapters.a_share_pool as pool_mod

    real_import = builtins.__import__

    def _no_fcntl(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".")[0] == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_fcntl)
    try:
        reloaded = importlib.reload(pool_mod)
        assert reloaded.fcntl is None
        store = reloaded.WatchlistStore(tmp_path / "wl.json")
        store.add("600519", "A")
        store.add("000001", "A")
        assert [entry.code for entry in store.list()] == ["600519", "000001"]
        assert store.remove("600519", "A") is True
    finally:
        monkeypatch.undo()
        importlib.reload(pool_mod)


# --------------------------------------------------------------------------- #
# ``max(date)`` 的 NULL 守卫（R59 审计 M7）
#
# 改前 ``hot_date = cur.fetchone()[0]`` 直接取用：空表时它是 NULL，被塞进
# ``WHERE date = %s``（变成永远查不到行的 ``= NULL``），或者驱动连行都没返回时
# 在 ``None[0]`` 上抛 TypeError。同文件 ``fetch_limit_pool_marks`` 早有正确守卫。
# --------------------------------------------------------------------------- #


class _RecordingConn:
    """返回**同一个** ``FakeCursor`` 的连接 —— 便于断言实际下发的 SQL/参数。"""

    def __init__(self, rows: list[tuple]) -> None:
        self._cursor = FakeCursor(rows=rows)

    def cursor(self) -> FakeCursor:
        return self._cursor


def test_fetch_hot_pool_never_queries_with_null_date() -> None:
    """空表 ⇒ 不得下发 ``WHERE date = NULL``（改前会下发，参数是 ``(None,)``）。"""
    conn = _RecordingConn(rows=[])
    assert fetch_hot_pool(conn) == []
    params = [p for _sql, p in conn._cursor.executed]
    assert (None,) not in params, "不得把 NULL 当日期参数去查第二张表"


def test_fetch_hot_pool_tolerates_fetchone_returning_none() -> None:
    """驱动返回 ``None``（无行）⇒ 旧代码 ``cur.fetchone()[0]`` 抛 TypeError。"""
    conn = _RecordingConn(rows=[])
    conn._cursor.fetchone = lambda: None  # type: ignore[method-assign]
    assert fetch_hot_pool(conn) == []


def test_fetch_hot_pool_keeps_ladder_when_hot_rank_is_empty() -> None:
    """一个源为空不得影响另一个源 —— 守卫只跳过空源自己的查询。"""
    rows = [
        ("ladder_date", date(2026, 9, 21)),
        ("ladder", date(2026, 9, 21), "001234", 2),
    ]
    conn = _RecordingConn(rows=rows)
    entries = fetch_hot_pool(conn)
    assert [e.code for e in entries] == ["001234"]
    assert entries[0].as_of == date(2026, 9, 21).isoformat()


# --------------------------------------------------------------------------- #
# 自选记录的形状校验（R59 审计 L8）
#
# 改前 ``WatchlistEntry(**item)`` / ``e.get(...)``：一条非 dict 记录就让整个自选
# 列表抛 TypeError、非对象行更是 AttributeError，都不是 ``WatchlistError``。
# 现在：只读路径逐条校验 + 跳过 + warning；写路径抛 ``WatchlistError``。
# --------------------------------------------------------------------------- #

_MALFORMED_RECORDS: list[Any] = [
    "oops",  # 非对象 → 旧代码 `WatchlistEntry(**"oops")` TypeError
    {"code": "000001", "market": "A"},  # 缺 added_at
    {"code": "000002", "market": "A", "added_at": "2026-10-01", "extra": 1},  # 未知字段
    {"code": 123, "market": "A", "added_at": "2026-10-01"},  # code 不是字符串
]


def test_watchlist_list_skips_malformed_records_with_warning(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            [{"code": "600519", "market": "A", "added_at": "2026-10-01T00:00:00+00:00"}]
            + _MALFORMED_RECORDS
        ),
        encoding="utf-8",
    )
    store = WatchlistStore(path)
    with caplog.at_level(logging.WARNING, logger="cpt.adapters.a_share_pool"):
        items = store.list()
    assert [i.code for i in items] == ["600519"]
    skipped = [r for r in caplog.records if "已跳过" in r.getMessage()]
    assert len(skipped) == len(_MALFORMED_RECORDS), "每条坏记录都要留一条可观测痕迹"


def test_watchlist_write_paths_raise_domain_error_on_malformed_record(
    tmp_path: pathlib.Path,
) -> None:
    """写路径**不跳过**坏记录：``_write_atomic`` 会把跳过的那条永久抹掉（R45）。"""
    import cpt.adapters.a_share_pool as pool_mod

    # ⚠️ 从模块属性取异常类，而不是顶层 import 的名字：本文件末尾的
    # ``test_module_imports_without_fcntl`` 会 ``importlib.reload`` 本模块，
    # 重载后 ``WatchlistError`` 是**新的类对象**，顶层名字就接不住了。
    error_cls = pool_mod.WatchlistError
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps(_MALFORMED_RECORDS), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    store = WatchlistStore(path)
    with pytest.raises(error_cls, match="未做任何修改"):
        store.add("600002", "A")
    with pytest.raises(error_cls, match="未做任何修改"):
        store.remove("000001", "A")
    assert path.read_text(encoding="utf-8") == before, "报错时不得改写文件"
