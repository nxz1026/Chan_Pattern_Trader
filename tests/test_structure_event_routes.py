"""结构事件流 HTTP 接口 + ``recent_events`` store 函数（R27-2）。

R26 把事件流接进了生产表，但**没有任何 HTTP 出口** —— `snapshot.events` 稳态为空
（R27 已查明是「本轮无变化」的正常语义），历史事件只有库里有、接口读不到。
本文件守住新接的两条路由与它们背后的 store 函数。

不依赖 psycopg：用内存假库顶替，按 SQL 关键字分派。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from cpt.storage.structure_event_store import recent_events, timeline

from tests.conftest import served

# 刻意**不**用 ``stub_realtime_quote``：那个 fixture 全局替换 ``urllib.request.urlopen``，
# 而本文件恰恰要靠真 urlopen 去打 ``served()`` 起的真 HTTP server。
# 这两条路由也不碰东财行情（纯 DB 只读），没有需要 stub 的出网调用。

# --------------------------------------------------------------------------- #
# 内存假库
# --------------------------------------------------------------------------- #

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


def _row(
    row_id: int,
    structure_id: str,
    event_type: str,
    status: str,
    revision: int,
    kind: str,
    occurred_at: int,
) -> tuple[Any, ...]:
    return (
        row_id,
        structure_id,
        event_type,
        status,
        revision,
        {"kind": kind, "status": status},
        datetime.fromtimestamp(occurred_at / 1000, tz=UTC),
        datetime.fromtimestamp(occurred_at / 1000, tz=UTC),
    )


#: id 越新 = 越晚。occurred_at 故意让 row 2/3 **同毫秒**，用来钉住排序稳定性
#: （只按 occurred_at 排序时并列行的相对顺序是不确定的）。
_ROWS = (
    _row(1, "bi:5:1000", "created", "forming", 1, "bi", 1_000),
    _row(2, "zs:5:2000", "created", "forming", 1, "zhongshu", 2_000),
    _row(3, "bi:5:1000", "confirmed", "confirmed", 2, "bi", 2_000),
    _row(4, "fx:5:3000", "created", "forming", 1, "fractal", 3_000),
)


@dataclass
class FakeCursor:
    conn: FakeConn
    executed: list[tuple[str, tuple]] = field(default_factory=list)
    _result: list[tuple[Any, ...]] = field(default_factory=list)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((sql, params))
        if self.conn.raise_on_execute:
            raise RuntimeError("simulated db down")
        rows = list(self.conn.rows)
        # 参数位置随 WHERE 子句个数变化，所以按 SQL 里占位符出现的顺序取，
        # 不能写死 params[0] —— 两个过滤同时存在时 kind 实际在 params[1]。
        pos = 0
        if "structure_id = %s" in sql:
            rows = [r for r in rows if r[1] == params[pos]]
            pos += 1
        if "event_type = %s" in sql:
            rows = [r for r in rows if r[2] == params[pos]]
            pos += 1
        if "payload->>'kind' = %s" in sql:
            kind = params[pos]
            rows = [r for r in rows if r[5].get("kind") == kind]
        if "ORDER BY occurred_at DESC" in sql:
            rows = sorted(rows, key=lambda r: (r[6], r[0]), reverse=True)
            rows = rows[: params[-1]]
        elif "ORDER BY revision ASC" in sql:
            rows = sorted(rows, key=lambda r: (r[0], r[4]))
            rows = rows[: params[-1]]
        self._result = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._result)

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None


@dataclass
class FakeConn:
    rows: tuple[tuple[Any, ...], ...] = _ROWS
    raise_on_execute: bool = False
    commit_count: int = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commit_count += 1


class FakeClient:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self.closed = False

    def _get_conn(self) -> FakeConn:
        return self._conn

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch) -> FakeConn:
    """把路由里惰性导入的 ``AShareLocalClient`` 换成假客户端。"""
    fake = FakeConn()
    monkeypatch.setattr(
        "cpt.adapters.a_share_local.AShareLocalClient", lambda *a, **k: FakeClient(fake)
    )
    return fake


def _get(base: str, path: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=10) as fh:
            return fh.status, json.loads(fh.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# store：recent_events
# --------------------------------------------------------------------------- #


def test_recent_events_sorted_newest_first(conn: FakeConn) -> None:
    events = recent_events(conn, limit=10)
    assert [e["id"] for e in events] == [4, 3, 2, 1]


def test_recent_events_tie_broken_by_id(conn: FakeConn) -> None:
    """row 2 与 row 3 同毫秒 —— 只按 occurred_at 排会让并列顺序随机漂移。"""
    events = recent_events(conn, limit=10)
    same_ms = [e for e in events if e["id"] in (2, 3)]
    assert [e["id"] for e in same_ms] == [3, 2], "同毫秒必须按 id 降序，不能抖动"


def test_recent_events_filters_by_event_type(conn: FakeConn) -> None:
    events = recent_events(conn, limit=10, event_type="created")
    assert {e["event_type"] for e in events} == {"created"}
    assert len(events) == 3


def test_recent_events_filters_by_kind(conn: FakeConn) -> None:
    events = recent_events(conn, limit=10, kind="bi")
    assert [e["structure_id"] for e in events] == ["bi:5:1000", "bi:5:1000"]


def test_recent_events_combines_filters(conn: FakeConn) -> None:
    events = recent_events(conn, limit=10, event_type="confirmed", kind="bi")
    assert [e["event_type"] for e in events] == ["confirmed"]


def test_recent_events_respects_limit(conn: FakeConn) -> None:
    assert len(recent_events(conn, limit=2)) == 2


def test_recent_events_clamps_limit(conn: FakeConn) -> None:
    """limit 是不可信入参：0/负数不能变成「返回全部」或 SQL 报错。"""
    assert len(recent_events(conn, limit=0)) == 1
    assert len(recent_events(conn, limit=-5)) == 1
    assert len(recent_events(conn, limit=10**9)) == len(_ROWS)


def test_recent_events_raises_on_db_error() -> None:
    """**读接口不许吞异常** —— 吞了 HTTP 层就只能把「查不到」谎报成「没有」。

    降级由 HTTP 层负责（见 test_route_degrades_when_table_missing）。
    """
    with pytest.raises(RuntimeError, match="simulated db down"):
        recent_events(FakeConn(raise_on_execute=True), limit=5)


def test_timeline_raises_on_db_error() -> None:
    with pytest.raises(RuntimeError, match="simulated db down"):
        timeline(FakeConn(raise_on_execute=True), "bi:5:1000", limit=5)


def test_timeline_orders_by_revision_ascending(conn: FakeConn) -> None:
    events = timeline(conn, "bi:5:1000", limit=10)
    assert [e["revision"] for e in events] == [1, 2]


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #


@contextmanager
def _served() -> Any:
    with served() as base:
        yield base


def test_route_lists_recent_events(patch_client: FakeConn) -> None:
    with _served() as base:
        status, body = _get(base, "/api/dashboard/structure-events?limit=10")

    assert status == 200
    assert body["available"] is True
    assert body["basis"] == "structure_event_stream"
    assert body["count"] == 4
    assert [e["id"] for e in body["events"]] == [4, 3, 2, 1]


def test_route_passes_filters_through(patch_client: FakeConn) -> None:
    with _served() as base:
        status, body = _get(base, "/api/dashboard/structure-events?kind=bi&event_type=created")

    assert status == 200
    assert [e["structure_id"] for e in body["events"]] == ["bi:5:1000"]


def test_route_survives_bad_limit(patch_client: FakeConn) -> None:
    """limit 传垃圾不该 500 —— 回落默认值即可。"""
    with _served() as base:
        status, body = _get(base, "/api/dashboard/structure-events?limit=abc")

    assert status == 200
    assert body["limit"] == 50


def test_route_timeline_requires_structure_id(patch_client: FakeConn) -> None:
    """缺 id 是**调用错误**不是降级：回 400，而不是返回全表。"""
    with _served() as base:
        status, body = _get(base, "/api/dashboard/structure-events/timeline")

    assert status == 400
    assert body["error"]["code"] == "invalid_structure_id"


def test_route_timeline_returns_revisions_ascending(patch_client: FakeConn) -> None:
    with _served() as base:
        status, body = _get(base, "/api/dashboard/structure-events/timeline?structure_id=bi:5:1000")

    assert status == 200
    assert body["available"] is True
    assert [e["revision"] for e in body["events"]] == [1, 2]


def test_route_degrades_instead_of_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 挂掉时两条路由都回 available=false，**不 500**。"""

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr("cpt.adapters.a_share_local.AShareLocalClient", _boom)

    with _served() as base:
        s1, b1 = _get(base, "/api/dashboard/structure-events")
        s2, b2 = _get(base, "/api/dashboard/structure-events/timeline?structure_id=bi:5:1000")

    assert s1 == 200
    assert b1["available"] is False
    assert b1["reason"] == "structure_event_stream_unavailable"
    assert b1["events"] == []
    assert s2 == 200
    assert b2["available"] is False


def test_route_degrades_when_table_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """迁移没跑（表不存在）也走同一条降级路径。"""
    monkeypatch.setattr(
        "cpt.adapters.a_share_local.AShareLocalClient",
        lambda *a, **k: FakeClient(FakeConn(raise_on_execute=True)),
    )
    with _served() as base:
        status, body = _get(base, "/api/dashboard/structure-events")

    assert status == 200
    assert body["available"] is False
