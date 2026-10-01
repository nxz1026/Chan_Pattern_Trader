"""``cpt.application.dashboard_run_store`` + ``cpt/web/app.py`` 接线测试（R23）。

不依赖 psycopg —— 用一个**内存假库**（:class:`FakeConn`）顶替，它按 ``run_id``
存行、认 ``ON CONFLICT DO NOTHING``、认 ``rowcount``，行为对齐 PG 那一侧。

本文件真正要守住的是**跨重启可比**这条语义：``record_run`` 写进表 →
``clear_runs()``（等价于进程退出，ring 清空）→ 只剩表 → ``/compare`` 仍能拿到
本体。R20 时代这条链路在 ``clear_runs()`` 之后就断了（``run_body_unavailable``），
所以它必须有测试兜着。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from cpt.application.dashboard_run_store import (
    DashboardRunError,
    get_snapshots,
    recent_runs,
    upsert_run,
)
from cpt.application.dashboard_runs import clear_runs, record_run

# --------------------------------------------------------------------------- #
# Mock DB：一个够用的内存假库
# --------------------------------------------------------------------------- #


@dataclass
class FakeCursor:
    """psycopg cursor duck type。"""

    conn: FakeConn
    executed: list[tuple[str, tuple]] = field(default_factory=list)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((sql, params))
        if "INSERT INTO public.cpt_dashboard_run" in sql:
            run_id, dataset_hash, generated_at, body_recorded, snapshot = params
            if run_id in self.conn.rows:
                self.rowcount = 0
            else:
                self.conn.rows[run_id] = {
                    "run_id": run_id,
                    "dataset_hash": dataset_hash,
                    "generated_at": generated_at,
                    "body_recorded": body_recorded,
                    # 库侧存的是 jsonb 文本，取出时反序列化成本体 dict
                    "snapshot": json.loads(snapshot) if snapshot is not None else None,
                }
                self.rowcount = 1
        elif "SELECT run_id, snapshot FROM public.cpt_dashboard_run" in sql:
            (wanted,) = params
            self._result = [
                (rid, row["snapshot"]) for rid, row in self.conn.rows.items() if rid in wanted
            ]
        elif "FROM public.cpt_dashboard_run" in sql and "ORDER BY generated_at DESC" in sql:
            if self.conn.fetch_override is not None:
                self._result = self.conn.fetch_override
            else:
                self._result = [
                    (
                        row["run_id"],
                        row["dataset_hash"],
                        row["generated_at"],
                        row["body_recorded"],
                        "BTCUSDT",  # snapshot->'market'->>'symbol'
                        "3600000",  # snapshot->'market'->>'interval_ms'（jsonb ->> 出文本）
                        "40",  # snapshot->'market'->>'bar_count'
                        "cfg-hash",  # snapshot->'reproducibility'->>'config_hash'
                        "realtime",  # snapshot->'runtime'->>'data_source'
                        "ok",  # snapshot->'runtime'->>'status'
                    )
                    for row in sorted(
                        self.conn.rows.values(),
                        key=lambda r: r["generated_at"],
                        reverse=True,
                    )[: params[0]]
                ]

    rowcount: int = 0
    _result: list[tuple] = field(default_factory=list)

    def fetchall(self) -> list[tuple]:
        return self._result


class FakeConn:
    """psycopg connection duck type —— 跨调用共享同一份 ``rows``。"""

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = rows if rows is not None else {}
        self.commits = 0
        self.cursors: list[FakeCursor] = []
        #: 脏数据用例用：直接顶掉 recent_runs 的 SELECT 结果，绕过自动拼行。
        self.fetch_override: list[tuple] | None = None

    def cursor(self) -> FakeCursor:
        cur = FakeCursor(conn=self)
        self.cursors.append(cur)
        return cur

    def commit(self) -> None:
        self.commits += 1


class BrokenConn:
    """任何 SQL 都炸 —— 用来验证「库挂了不反噬 HTTP」。"""

    def cursor(self) -> Any:
        raise RuntimeError("connection is closed")


def _row(run_id: str = "run-1", dataset_hash: str = "ds-1", generated_at: int = 1_790_000_000_000):
    return {"run_id": run_id, "dataset_hash": dataset_hash, "generated_at": generated_at}


def _snapshot(symbol: str = "BTCUSDT", run_id: str | None = "run-1") -> dict[str, Any]:
    """最小可用 snapshot：``build_run_index`` 只需要 market/runtime/reproducibility。"""
    runtime: dict[str, Any] = {"data_source": "realtime", "status": "ok"}
    if run_id is not None:
        runtime["run_id"] = run_id
    return {
        "market": {"symbol": symbol, "interval_ms": 3_600_000, "bar_count": 40},
        "runtime": runtime,
        "reproducibility": {"dataset_hash": "ds-1", "config_hash": "cfg-hash"},
        "candles": [],
    }


@pytest.fixture(autouse=True)
def _clean_ring():
    """ring 是进程级全局状态；每个用例前后都清干净，别让顺序影响结果。"""
    clear_runs()
    yield
    clear_runs()


# --------------------------------------------------------------------------- #
# upsert_run
# --------------------------------------------------------------------------- #


def test_upsert_run_writes_exactly_five_columns() -> None:
    """5 列方案被用户明确拍板：少一列是回归，多一列是冗余（都能从 jsonb 现抽）。"""
    conn = FakeConn()
    assert upsert_run(conn, _row(), {"a": 1}) is True

    sql, params = conn.cursors[0].executed[0]
    insert_cols = sql[sql.index("(") + 1 : sql.index(")")]
    assert [c.strip() for c in insert_cols.split(",")] == [
        "run_id",
        "dataset_hash",
        "generated_at",
        "body_recorded",
        "snapshot",
    ]
    assert len(params) == 5
    # 幂等靠主键冲突，不是靠应用层先 SELECT
    assert "ON CONFLICT (run_id) DO NOTHING" in sql
    # jsonb 靠 SQL 侧强转，模块保持零 psycopg 依赖
    assert "%s::jsonb" in sql
    assert params[4] == json.dumps({"a": 1})


def test_upsert_run_does_not_commit() -> None:
    """事务边界归调用方：store 层不 commit。"""
    conn = FakeConn()
    upsert_run(conn, _row(), {"a": 1})
    assert conn.commits == 0


def test_upsert_run_body_none_marks_not_recorded() -> None:
    """超 4MB 闸门时 body=None：索引行照写、本体留 NULL、闸门状态可查。"""
    conn = FakeConn()
    assert upsert_run(conn, _row(), None) is True

    _, params = conn.cursors[0].executed[0]
    assert params[3] is False
    assert params[4] is None
    assert conn.rows["run-1"]["body_recorded"] is False


def test_upsert_run_conflict_returns_false() -> None:
    """同一 run_id 重复写（30s 轮询命中同一份缓存）只留一行。"""
    conn = FakeConn()
    assert upsert_run(conn, _row(), {"v": 1}) is True
    assert upsert_run(conn, _row(), {"v": 2}) is False
    assert len(conn.rows) == 1
    # 冲突时**不覆盖**已有本体：DO NOTHING 而非 DO UPDATE
    assert conn.rows["run-1"]["snapshot"] == {"v": 1}


def test_upsert_run_missing_run_id_is_noop_not_crash() -> None:
    """run_id 是主键；残缺 snapshot 不该让整个记账抛出去。"""
    conn = FakeConn()
    assert upsert_run(conn, {"dataset_hash": "ds-1"}, {"a": 1}) is False
    assert conn.cursors == []


def test_upsert_run_missing_dataset_hash_stores_empty_string() -> None:
    """dataset_hash 是 NOT NULL：缺失存空串而不是让整行写失败。"""
    conn = FakeConn()
    assert upsert_run(conn, {"run_id": "r", "generated_at": 1}, {"a": 1}) is True
    assert conn.rows["r"]["dataset_hash"] == ""


@pytest.mark.parametrize(
    ("generated_at", "label"),
    [
        (1_790_000_000_000, "unix 毫秒"),
        ("2026-09-30T23:59:59+00:00", "ISO 串"),
        (datetime(2026, 9, 30, tzinfo=UTC), "datetime"),
        (None, "缺失回落墙钟"),
        ("not-a-date", "不可解析回落墙钟"),
    ],
)
def test_upsert_run_accepts_every_generated_at_shape(generated_at: Any, label: str) -> None:
    """``generated_at`` 是 NOT NULL：什么形态都得能落库，不能因为解析失败丢一次记账。"""
    conn = FakeConn()
    assert upsert_run(
        conn, {"run_id": "r", "dataset_hash": "d", "generated_at": generated_at}, None
    )
    assert isinstance(conn.rows["r"]["generated_at"], datetime)


def test_upsert_run_raises_dashboard_run_error() -> None:
    """写失败必须抛：调用方要靠它知道「这次没落库」，但自己负责 best-effort 吞。"""
    with pytest.raises(DashboardRunError):
        upsert_run(BrokenConn(), _row(), {"a": 1})


# --------------------------------------------------------------------------- #
# get_snapshots
# --------------------------------------------------------------------------- #


def test_get_snapshots_empty_ids_skips_db() -> None:
    conn = FakeConn()
    assert get_snapshots(conn, []) == {}
    assert conn.cursors == []


def test_get_snapshots_distinguishes_missing_row_from_null_body() -> None:
    """两种「拿不到」必须可区分：

    - key 不在 → 库里没这一行 → 调用方**可以**回落 ring；
    - key 在但值 None → 库里明确记了「没有本体」（4MB 闸门）→ **不能**回落，
      否则「闸门拒了」会被「ring 里碰巧有一份」掩盖掉。
    """
    conn = FakeConn()
    upsert_run(conn, _row(run_id="with-body"), {"v": 1})
    upsert_run(conn, _row(run_id="no-body"), None)

    out = get_snapshots(conn, ["with-body", "no-body", "never-written"])
    assert out["with-body"] == {"v": 1}
    assert out["no-body"] is None
    assert "never-written" not in out


def test_get_snapshots_single_query() -> None:
    """一次 ANY(%s) 走主键，不做 N 次单查。"""
    conn = FakeConn()
    for rid in ("a", "b", "c"):
        upsert_run(conn, _row(run_id=rid), {"v": rid})
    conn.cursors.clear()

    get_snapshots(conn, ["a", "b", "c"])
    assert len(conn.cursors) == 1
    assert "= ANY(%s)" in conn.cursors[0].executed[0][0]


def test_get_snapshots_raises_dashboard_run_error() -> None:
    with pytest.raises(DashboardRunError):
        get_snapshots(BrokenConn(), ["a"])


# --------------------------------------------------------------------------- #
# recent_runs
# --------------------------------------------------------------------------- #


def test_recent_runs_row_shape_matches_in_process_ring() -> None:
    """表数据源与 ring 数据源对前端是一视同仁的，字段名/时间口径必须一致。"""
    conn = FakeConn()
    upsert_run(conn, _row(), {"v": 1})
    row = recent_runs(conn, 50)[0]

    assert set(row) >= {
        "run_id",
        "dataset_hash",
        "generated_at",
        "created_at",
        "symbol",
        "interval_ms",
        "bar_count",
        "config_hash",
        "source",
        "status",
    }
    # timestamptz → Unix 毫秒，与 ring 口径一致（前端所有时间轴都吃毫秒）
    assert isinstance(row["generated_at"], int)
    # 前端读的是 created_at；5 列方案不加这一列，所以这里镜像 generated_at
    assert row["created_at"] == row["generated_at"]
    # jsonb ->> 出的是文本，数值要在 Python 侧转回 int（前端要能算）
    assert row["interval_ms"] == 3_600_000
    assert row["bar_count"] == 40
    assert row["body_recorded"] is True


def test_recent_runs_tolerates_dirty_jsonb_numbers() -> None:
    """脏数据（``"interval_ms": "abc"``）少一个字段，而不是把整页打成 SQL 错误。

    数值转换放在 Python 侧正是为此：真在 SQL 里写 ``::integer``，一条脏行就能
    让 ``/api/dashboard/runs`` 整页 500。
    """
    conn = FakeConn()
    conn.fetch_override = [
        (
            "r1",
            "ds",
            datetime(2026, 9, 30, tzinfo=UTC),
            False,
            "X",
            "abc",
            "40",
            "cfg",
            "realtime",
            "ok",
        )
    ]
    row = recent_runs(conn, 10)[0]
    assert row["interval_ms"] is None
    assert row["bar_count"] == 40


def test_recent_runs_raises_dashboard_run_error() -> None:
    with pytest.raises(DashboardRunError):
        recent_runs(BrokenConn(), 10)


# --------------------------------------------------------------------------- #
# 跨重启闭环 —— 本文件的核心
# --------------------------------------------------------------------------- #


def test_snapshot_survives_restart_via_table() -> None:
    """**R23 的存在理由**：重启后 ring 清空，表里的本体仍能取到。

    模拟：新进程 record_run → 写表 → 进程退出（``clear_runs``）→ 换一个「新进程」
    查表。
    """
    conn = FakeConn()

    # —— 旧进程 ——
    record_run(_snapshot(), on_recorded=lambda row, body: upsert_run(conn, row, body))
    assert len(conn.rows) == 1
    assert record_run is not None

    # —— 进程退出：ring 全清（表不受影响）——
    clear_runs()

    # —— 新进程：ring 查不到，表查得到 ——
    from cpt.application.dashboard_runs import run_body

    assert run_body("run-1") is None, "前提：ring 确实是空的"
    restored = get_snapshots(conn, ["run-1"])["run-1"]
    assert restored is not None
    assert restored["market"]["symbol"] == "BTCUSDT"
    assert restored["reproducibility"]["dataset_hash"] == "ds-1"


def test_dedup_hit_does_not_touch_db() -> None:
    """去重命中**不写库**：realtime 30s 一轮里十几次请求命中同一份缓存 snapshot，
    每次都打一次 DB 是纯浪费——这也正是 R20 当年误判「落库即 2,880 行/天」的
    放大来源。"""
    conn = FakeConn()
    calls: list[str] = []
    hook = lambda row, body: (calls.append(row["run_id"]), upsert_run(conn, row, body))  # noqa: E731

    snap = _snapshot()
    record_run(snap, on_recorded=hook)
    record_run(snap, on_recorded=hook)
    record_run(snap, on_recorded=hook)

    assert calls == ["run-1"], "只有首次 append 触发一次双写"
    assert len(conn.rows) == 1


def test_record_run_without_hook_still_works() -> None:
    """``on_recorded`` 可选：既有调用方（R20 的测试与 fixtures 路径）行为不变。"""
    assert record_run(_snapshot())["run_id"] == "run-1"
    assert record_run(_snapshot(run_id="run-2"))["run_id"] == "run-2"


# --------------------------------------------------------------------------- #
# cpt/web/app.py 接线
# --------------------------------------------------------------------------- #


def test_app_load_run_bodies_prefers_table_over_ring() -> None:
    from cpt.web import app

    conn = FakeConn()
    upsert_run(conn, _row(run_id="only-in-db"), {"from": "db"})

    # ring 里也塞一条同名但内容不同的：表优先必须赢
    record_run(_snapshot(run_id="only-in-db"), on_recorded=lambda row, body: None)

    got = app._load_run_bodies(["only-in-db"], conn=conn)
    assert got["only-in-db"] == {"from": "db"}


def test_app_load_run_bodies_falls_back_to_ring_when_table_broken() -> None:
    from cpt.web import app

    record_run(_snapshot(run_id="ring-only"), on_recorded=lambda row, body: None)
    assert app._load_run_bodies(["ring-only"], conn=BrokenConn()) == {}


def test_app_load_run_bodies_empty_ids() -> None:
    from cpt.web import app

    conn = FakeConn()
    assert app._load_run_bodies([], conn=conn) == {}
    assert conn.cursors == []


def test_app_index_row_from_body_recovers_dataset_hash() -> None:
    """跨重启比较时 ring 空，dataset_hash 得从本体反推，否则差异摘要少一个口径字段。"""
    from cpt.web import app

    body = _snapshot()
    row = app._index_row_from_body("run-1", body)
    assert row == {"run_id": "run-1", "dataset_hash": "ds-1"}

    # 本体被闸门拒掉（None）时只给 run_id，不编造 dataset_hash
    assert app._index_row_from_body("run-1", None) == {"run_id": "run-1"}


def test_app_run_index_rows_prefers_table() -> None:
    from cpt.web import app

    conn = FakeConn()
    upsert_run(conn, _row(run_id="db-run"), {"v": 1})
    rows = app._run_index_rows(50, conn=conn)
    assert [r["run_id"] for r in rows] == ["db-run"]


def test_app_run_index_rows_falls_back_to_ring() -> None:
    """表不可用时退化成 R20 行为（只显示本进程历史），而不是 500。"""
    from cpt.web import app

    record_run(_snapshot(run_id="ring-run"), on_recorded=lambda row, body: None)
    rows = app._run_index_rows(50, conn=BrokenConn())
    assert [r["run_id"] for r in rows] == ["ring-run"]


def test_app_persist_run_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    """**回归守卫**：``_persist_run`` 必须 commit，否则数据被静默回滚。

    这条对应一次真实上线事故：store 层按设计不 commit（事务边界归调用方），
    而 ``_persist_run`` 最初也漏了 commit。``AShareLocalClient`` 走裸
    ``psycopg.connect()``（无 autocommit），退出 with 时 ``client.close()``
    把未提交的 INSERT 回滚——**且不抛任何异常**。现场表现是：HTTP 全 200、
    journalctl 一条告警都没有、表里 0 行，极难定位。

    所以这里断言的不是「没抛异常」，而是**确实调了 commit**。
    """
    from cpt.web import app

    conn = FakeConn()
    monkeypatch.setattr(app, "_run_store_conn", lambda c=None: _passthrough(conn))
    app._persist_run(_row(), {"v": 1})
    assert conn.commits == 1, "没有 commit = 这一行 INSERT 会在 close() 时被回滚"
    assert conn.rows["run-1"]["snapshot"] == {"v": 1}


def test_app_persist_run_swallows_db_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """**最关键的一条**：DB 挂了绝不能让用户的 HTTP 响应 500。

    记账是旁路，失败的真实后果只是「这次运行重启后查不到」——日志里留痕即可。
    """
    from cpt.web import app

    @contextmanager
    def _down():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(app, "_run_store_conn", lambda conn=None: _down())
    app._persist_run(_row(), {"v": 1})  # 不抛即通过


@contextmanager
def _passthrough(conn: Any) -> Any:
    """把已有连接直接交出去（不负责关闭），对齐 ``_run_store_conn(conn=...)``。"""
    yield conn
