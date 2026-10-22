"""R90：盘中封板快照的落库与读取（``cpt/storage/intraday_seal_store.py``）。

核心守的是三条口径：

1. **没行 = 判不了**，不是「没封板」—— 停牌/缺档位不写行。
2. ``sealed=False`` 是「采到了、这一轮没封在板上」。
3. 下游只认「最新一轮」，不能跨轮混算。

另外守采集器的一条：**写库失败不许改 ``outcome``** —— 行情断了与库断了是
两个不同的红灯，混成一个就没法排查。
"""

from __future__ import annotations

import datetime as dt
import sys
from typing import Any

import pytest
from cpt.storage.intraday_seal_store import (
    insert_seal_snapshot,
    load_latest_round,
    load_seal_track,
)

AT = dt.datetime(2026, 10, 22, 9, 30, tzinfo=dt.UTC)


class _Cur:
    """按 SQL 分发的假游标。"""

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def executemany(self, sql: str, params: list[tuple]) -> None:
        self._conn.calls.append(("executemany", sql, params))

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.calls.append(("execute", sql, params))
        if "max(at)" in sql:
            self._conn._result = [(self._conn.latest,)]
        elif "WHERE at = %s" in sql:
            self._conn._result = list(self._conn.round_rows)
        elif "WHERE code = %s" in sql:
            self._conn._result = list(self._conn.track_rows)

    def fetchone(self) -> tuple | None:
        rows = self._conn._result
        return rows[0] if rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._conn._result)


class _Conn:
    def __init__(
        self,
        *,
        latest: Any = None,
        round_rows: list[tuple] | None = None,
        track_rows: list[tuple] | None = None,
    ) -> None:
        self.latest = latest
        self.round_rows = round_rows or []
        self.track_rows = track_rows or []
        self.calls: list[tuple[str, str, Any]] = []
        self._result: list[tuple] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


# ── 写入 ────────────────────────────────────────────────────────────


def test_insert_writes_one_row_per_code_with_assumed_defaults() -> None:
    conn = _Conn()
    n = insert_seal_snapshot(
        conn,
        [{"code": "600293", "sealed": True, "bid1_lots": 847522.54, "limit_price": 5.23}],
        at=AT,
    )
    assert n == 1
    _, sql, params = conn.calls[0]
    assert "INSERT INTO public.intraday_seal_snapshot" in sql
    assert "ON CONFLICT (at, code) DO NOTHING" in sql
    assert params[0] == (AT, "600293", True, 847522.54, 5.23)


def test_insert_omits_judgement_columns_when_unknown() -> None:
    """判不出来的票也要落一行 sealed=False —— 「看过了没封」与「没看见」不同。"""
    conn = _Conn()
    insert_seal_snapshot(conn, [{"code": "000001"}], at=AT)
    _, _, params = conn.calls[0]
    assert params[0] == (AT, "000001", False, None, None)


def test_insert_of_nothing_does_not_touch_the_connection() -> None:
    """空轮次不该发一条空 SQL —— 真库上 ``executemany([])`` 是白跑一次往返。"""
    conn = _Conn()
    assert insert_seal_snapshot(conn, [], at=AT) == 0
    assert conn.calls == []


def test_insert_does_not_commit() -> None:
    """事务边界归调用方（与 ``structure_event_store`` 同款约定）。"""
    conn = _Conn()
    insert_seal_snapshot(conn, [{"code": "600293"}], at=AT)
    assert all(name != "commit" for name, _, _ in conn.calls)
    assert not hasattr(conn, "committed")


# ── 读取 ────────────────────────────────────────────────────────────


def test_latest_round_is_empty_when_table_is_empty() -> None:
    out = load_latest_round(_Conn(latest=None))
    assert out == {"at": None, "rows": [], "counts": {"total": 0, "sealed": 0, "open_board": 0}}


def test_latest_round_counts_sealed_and_open_board() -> None:
    rows = [
        (AT, "600293", True, 847522.5, 5.23),
        (AT, "002249", True, 293674.5, 7.06),
        (AT, "000001", False, None, None),
    ]
    out = load_latest_round(_Conn(latest=AT, round_rows=rows))
    assert out["at"] == AT
    assert out["counts"] == {"total": 3, "sealed": 2, "open_board": 1}
    assert out["rows"][0]["code"] == "600293"


def test_latest_round_only_reads_one_timestamp() -> None:
    """**只认一轮** —— 跨轮混算会把「上一轮封着、这一轮开板」读成两只票。"""
    conn = _Conn(latest=AT, round_rows=[])
    load_latest_round(conn)
    sqls = [sql for name, sql, _ in conn.calls if name == "execute"]
    assert any("WHERE at = %s" in s for s in sqls)
    # 没有任何一句按 code 查最新（那是另一种错法）
    assert not any("WHERE code = %s" in s for s in sqls)


def test_seal_track_is_ordered_and_scoped() -> None:
    rows = [
        (AT, True, 100.0, 5.23),
        (AT + dt.timedelta(minutes=2), False, None, None),
    ]
    conn = _Conn(track_rows=rows)
    out = load_seal_track(conn, "600293", since=AT, until=AT + dt.timedelta(days=1))
    assert [r["sealed"] for r in out] == [True, False]
    # 每行必须带 code：``track_summary`` 要用它。第一版漏了，实跑时 KeyError，
    # 假游标喂的是 dict 所以测试没抓到 —— 这条断言就是为那次补的。
    assert [r["code"] for r in out] == ["600293", "600293"]
    _, sql, params = conn.calls[0]
    assert "ORDER BY at" in sql
    assert params == ("600293", AT, AT + dt.timedelta(days=1))


# ── 采集器：写库失败不许改 outcome ───────────────────────────────────


def _load_collectd() -> Any:
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "intraday_collectd", root / "scripts" / "intraday_collectd.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_persist_failure_does_not_change_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """库断了只记 ``db=warn:*``，``outcome`` 仍是 ok —— 行情采到了就是采到了。"""
    mod = _load_collectd()
    monkeypatch.setattr(mod, "DB_WRITE_ENABLED", True)
    # 真把 ``psycopg`` 打掉 —— 不是 mock 掉 ``_persist``，
    # 否则测的是「函数被调用」，不是「它在失败时怎么收敛」。
    monkeypatch.setitem(sys.modules, "psycopg", None)
    record: dict[str, Any] = {"at": "2026-10-22T09:30:00+00:00", "outcome": "ok"}
    mod._persist(record, {"sealed": [], "candidates": [{"code": "000001"}]})
    assert record["outcome"] == "ok"
    assert record["db"].startswith("warn:")


def test_persist_is_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_collectd()
    monkeypatch.setattr(mod, "DB_WRITE_ENABLED", False)
    record: dict[str, Any] = {"at": "2026-10-22T09:30:00+00:00", "outcome": "ok"}
    mod._persist(record, {"sealed": [], "candidates": []})
    assert record["db"] == "disabled"
    assert record["outcome"] == "ok"


def test_persist_covers_candidates_that_did_not_seal(monkeypatch: pytest.MonkeyPatch) -> None:
    """候选里没封板的也要落行 —— 只有封板行会让下游无法区分「开板」与「没采到」。

    ## 为什么用 ``monkeypatch.setitem`` 而不是自己 ``pop``（R90 实测踩过）

    第一版是手动写 ``sys.modules[...] = fake`` 再在 ``finally`` 里 ``pop`` 掉。
    代价不是「多一个模块对象」这么轻 —— 它会让**后续所有 import 拿到另一个类
    对象**，而且只在「真模块先前已被导入过」时才成立：

    1. 塞假模块时真 ``cpt.adapters.a_share_local`` 早已被别的用例导入，
       ``cpt.adapters`` 这个**包对象**上的 ``a_share_local`` 属性仍指向真模块；
    2. ``finally`` 只 ``pop`` 了 ``sys.modules`` 的键，**没清包属性**；
    3. 之后 ``from cpt.adapters.a_share_local import ...`` 走包属性拿旧模块，
       而 ``import cpt.adapters.a_share_local`` 会重新执行 => 同一进程里出现
       两个 ``AShareNoDataError`` 类；
    4. ``a_share_snapshot`` 在模块顶层绑定了旧类，于是 ``except AShareNoDataError``
       抓不到新类，落到最外层 ``except Exception`` => ``db_error:AShareNoDataError``。

    实测症状：``tests/test_web_a_share_routes.py`` 的两条 reason 用例**单跑绿、
    全量红**；二分到最小复现是「``test_a_share_factor.py`` + 本用例 + 那两条」
    三件套。``monkeypatch`` 记下原值并在 teardown 还原。

    ## 为什么这更糟：不是「模块回来了」，而是「模块被换成一个新的」

    实测 ``sys.modules`` 被 pop 之后，下一次 ``import cpt.adapters.a_share_local``
    会**重新执行模块体**（``real_after is real_first`` 为 False），``cpt.adapters``
    包属性也被重写成新模块 —— ``sys.modules`` 与包属性*内部*自洽，只是**与所有
    先前 ``from ... import`` 出去的类绑定都不再自洽**。所以「手动 pop 掉、让它
    重新导入回来」不是恢复原状，是换了一个新类。只有 ``monkeypatch.setitem``
    （真还原原对象）能避掉。
    """
    mod = _load_collectd()
    captured: dict[str, Any] = {}

    def fake_insert(conn: Any, rows: list[dict], *, at: Any) -> int:
        captured["rows"] = rows
        return len(rows)

    import types

    fake_pg = types.ModuleType("psycopg")
    fake_pg.connect = lambda **kw: _ClosingCtx()  # type: ignore[attr-defined]
    fake_local = types.ModuleType("cpt.adapters.a_share_local")
    fake_local.connection_kwargs = lambda: {}  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "insert_seal_snapshot", fake_insert)
    monkeypatch.setattr(mod, "DB_WRITE_ENABLED", True)
    monkeypatch.setitem(sys.modules, "psycopg", fake_pg)
    monkeypatch.setitem(sys.modules, "cpt.adapters.a_share_local", fake_local)
    record: dict[str, Any] = {"at": "2026-10-22T09:30:00+00:00", "outcome": "ok"}
    mod._persist(
        record,
        {
            "sealed": [{"code": "600293", "bid1_lots": 100.0, "limit_price": 5.23}],
            "candidates": [{"code": "600293"}, {"code": "000001"}],
        },
    )

    by_code = {r["code"]: r for r in captured["rows"]}
    assert by_code["600293"]["sealed"] is True
    assert by_code["000001"]["sealed"] is False
    assert "ok:2" in record["db"]


def test_module_cache_stays_consistent_after_fake_a_share_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归钉子：假 ``cpt.adapters.a_share_local`` 撤掉后，异常类必须仍是同一个对象。

    R90 那条「单跑绿、全量红」的根因就在这里（见上一条用例的 docstring）。
    只要有人把夹具改回手动 ``pop``，这里立刻红 —— 而且比目标用例红得更早、
    更好定位（目标用例躺在一个完全无关的文件里）。
    """
    import cpt.adapters.a_share_local as _before
    import cpt.application.a_share_snapshot as snap

    mod = _load_collectd()
    fake_pg = __import__("types").ModuleType("psycopg")
    fake_pg.connect = lambda **kw: _ClosingCtx()  # type: ignore[attr-defined]
    fake_local = __import__("types").ModuleType("cpt.adapters.a_share_local")
    fake_local.connection_kwargs = lambda: {}  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "DB_WRITE_ENABLED", True)
    monkeypatch.setattr(mod, "insert_seal_snapshot", lambda *a, **k: 0)
    monkeypatch.setitem(sys.modules, "psycopg", fake_pg)
    monkeypatch.setitem(sys.modules, "cpt.adapters.a_share_local", fake_local)
    mod._persist({"at": "2026-10-22T09:30:00+00:00", "outcome": "ok"}, {})

    import cpt.adapters.a_share_local as real

    if real is not _before:
        import pytest as _pytest

        _pytest.fail(
            "夹具撤掉假 cpt.adapters.a_share_local 之后，模块被重新执行了一次"
            "（不是还原，是换了一个新的）—— 改用 monkeypatch.setitem"
        )
    assert real.AShareNoDataError is snap.AShareNoDataError
    assert real.AShareNoFactorError is snap.AShareNoFactorError
    assert real.ASharePlaceholderRowsError is snap.ASharePlaceholderRowsError


class _ClosingCtx:
    def __enter__(self) -> _ClosingCtx:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def commit(self) -> None:
        return None
