"""审计 M18（清理逻辑收敛）+ M20（track 路由级用例）。

**M18**：两个 prune 原先没有任何执行者，且是无作用域的全局 DELETE、从未被安全测过。
store 侧已收敛为：作用域可选、幂等、返回删除计数，并新增只读 ``count_prunable_*``
供 web 的 dry-run 用。这里钉住三件事：dry-run 报的数字 == 随后真删的行数（共用同一
谓词）、第二次调用返回 0（幂等）、别人的行不被带走（作用域）。**定时接线归 web /
deploy agent，本文件不碰。**

**M20**：``tests/test_track.py`` 自称集成测试，但 6 个 track CRUD/维护 handler
（``cpt/web/track_api.py`` 的 list / add / remove / restore / history / maintenance）
零 HTTP/路由级用例。这里直调 handler（**不起真服务器**），DB 走真连接：monkeypatch
``track_api._conn`` 成「每次新开一连」的工厂，与生产一致（handler 自己会 close）。
维护 handler 的 ``prune_*`` / ``count_prunable_*`` 都用替身 —— 否则会在共享测试库上跑
无作用域的全局 DELETE，或让 dry-run 的数字随库里陈年数据漂移。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cpt.storage import track_store

_USER = "m18m20_user"
_OTHER = "m18m20_other"


def _require_db() -> bool:
    return os.environ.get("CPT_REQUIRE_DB", "").strip().lower() in {"1", "true", "yes"}


def _open_pg_conn() -> Any:
    """照 ``tests/test_track.py`` 的模式：``CPT_REQUIRE_DB=1`` 时连不上就是红。"""
    try:
        import psycopg  # noqa: F401 — 只探可用性
    except ImportError as exc:
        message = f"psycopg 不可用（需要 `pip install -e '.[db]'`）: {exc}"
        if _require_db():
            pytest.fail(message)
        pytest.skip(message)

    from cpt.adapters.a_share_local import AShareLocalClient

    try:
        return AShareLocalClient()._get_conn()  # noqa: SLF001 — 同 track_api 用法
    except Exception as exc:  # noqa: BLE001
        message = f"DB not reachable: {type(exc).__name__}: {str(exc)[:120]}"
        if _require_db():
            pytest.fail(message)
        pytest.skip(message)


def _cleanup(conn: Any, user_id: str) -> None:
    """按 user_id 清掉本模块造的所有行（先 rollback 防止 aborted 事务）。"""
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM public.cpt_track WHERE user_id = %s", (user_id,))
        cur.execute("DELETE FROM public.cpt_track_snapshot WHERE user_id = %s", (user_id,))
    conn.commit()


@pytest.fixture
def pg_conn() -> Any:
    conn = _open_pg_conn()
    yield conn
    _cleanup(conn, _USER)
    _cleanup(conn, _OTHER)
    conn.close()


# --------------------------------------------------------------------------- #
# M18：store 级 —— count==prune、幂等、作用域
# --------------------------------------------------------------------------- #


def _seed_snapshot(conn: Any, user_id: str, code: str, as_of: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.cpt_track_snapshot (user_id, code, as_of, payload) "
            "VALUES (%s, %s, %s, %s::jsonb)",
            (user_id, code, as_of, '{"x":1}'),
        )
    conn.commit()


def _count_snapshots(conn: Any, user_id: str, code: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.cpt_track_snapshot WHERE user_id = %s AND code = %s",
            (user_id, code),
        )
        return int(cur.fetchone()[0])


def _count_track(conn: Any, user_id: str, code: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.cpt_track WHERE user_id = %s AND code = %s",
            (user_id, code),
        )
        return int(cur.fetchone()[0])


def _set_removed_at(conn: Any, user_id: str, code: str, removed_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.cpt_track SET removed_at = %s WHERE user_id = %s AND code = %s",
            (removed_at, user_id, code),
        )
    conn.commit()


def test_snapshot_dry_run_count_matches_prune(pg_conn: Any) -> None:
    """审计 M18：快照 dry-run 计数 == 随后真删行数；幂等；不越界。"""
    track_store.ensure_table(pg_conn)
    old = datetime.now(UTC) - timedelta(days=31)
    fresh = datetime.now(UTC) - timedelta(days=1)
    code = "998871"
    _seed_snapshot(pg_conn, _USER, code, old)
    _seed_snapshot(pg_conn, _USER, code, old)
    _seed_snapshot(pg_conn, _USER, code, fresh)
    _seed_snapshot(pg_conn, _OTHER, code, old)  # 别人的陈年快照

    counted = track_store.count_prunable_snapshots(pg_conn, 31, user_id=_USER, code=code)
    assert counted == 2
    deleted = track_store.prune_snapshots(pg_conn, 31, user_id=_USER, code=code)
    assert deleted == counted, "dry-run 报的数与真删的数必须一致（共用同一谓词）"

    # 幂等：没得删了就都是 0
    assert track_store.count_prunable_snapshots(pg_conn, 31, user_id=_USER, code=code) == 0
    assert track_store.prune_snapshots(pg_conn, 31, user_id=_USER, code=code) == 0

    # 作用域：别人的陈年快照幸存
    assert _count_snapshots(pg_conn, _OTHER, code) == 1


def test_recycle_dry_run_count_matches_prune_and_is_scoped(pg_conn: Any) -> None:
    """审计 M18：回收站 dry-run 计数 == 真删行数；只清目标用户的过期行。"""
    track_store.ensure_table(pg_conn)
    old = datetime.now(UTC) - timedelta(days=120)
    fresh = datetime.now(UTC) - timedelta(days=1)

    for code in ("998872", "998873"):
        track_store.add(pg_conn, _USER, code)
        _set_removed_at(pg_conn, _USER, code, old)
    track_store.add(pg_conn, _USER, "998874")
    _set_removed_at(pg_conn, _USER, "998874", fresh)  # 刚删，未过期
    track_store.add(pg_conn, _OTHER, "998872")
    _set_removed_at(pg_conn, _OTHER, "998872", old)

    counted = track_store.count_prunable_removed(pg_conn, user_id=_USER)
    assert counted == 2
    deleted = track_store.prune_removed(pg_conn, user_id=_USER)
    assert deleted == counted

    assert track_store.count_prunable_removed(pg_conn, user_id=_USER) == 0
    assert track_store.prune_removed(pg_conn, user_id=_USER) == 0
    assert _count_track(pg_conn, _OTHER, "998872") == 1  # 别人那一行还在


class _ExplodingConn:
    """cursor() 就炸的假连接 —— 验证只读 dry-run 的失败语义。"""

    def __init__(self) -> None:
        self.committed = False

    def cursor(self) -> Any:
        raise RuntimeError("DB is down")

    def commit(self) -> None:
        self.committed = True


def test_count_prunable_failure_raises_without_commit() -> None:
    """回归：dry-run 失败与写路径同口径（抛 TrackStoreError），且**只读不提交**。"""
    conn = _ExplodingConn()
    with pytest.raises(track_store.TrackStoreError):
        track_store.count_prunable_removed(conn)
    assert conn.committed is False, "只读 dry-run 不许做任何写/提交"


# --------------------------------------------------------------------------- #
# M20：6 个 track handler 的路由级用例
# --------------------------------------------------------------------------- #


@pytest.fixture
def route(pg_conn: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """让 handler 走真 DB：``_conn`` 换成「每次新开一连」的工厂。"""
    track_api = pytest.importorskip("cpt.web.track_api")
    from cpt.adapters.a_share_local import AShareLocalClient

    def _factory() -> Any:
        return AShareLocalClient()._get_conn()  # noqa: SLF001

    monkeypatch.setattr(track_api, "_conn", _factory)
    return track_api


def test_route_add_list_history_roundtrip(route: Any) -> None:
    """handle_track_add / list / history：正常路径 + 重复加入幂等。"""
    body = {"code": "998875", "note": "m20"}
    payload, status = route.handle_track_add(_USER, body)
    assert (status, payload["ok"], payload["item"]["code"]) == (200, True, "998875")

    again, status_again = route.handle_track_add(_USER, body)
    assert status_again == 200
    assert again["item"]["added_at"] == payload["item"]["added_at"], "幂等加入不许重置 added_at"

    listing, status_list = route.handle_track_list(_USER)
    assert status_list == 200 and listing["available"] is True
    assert "998875" in [row["code"] for row in listing["items"]]

    history, status_hist = route.handle_track_history(_USER, "998875")
    assert status_hist == 200
    assert history["code"] == "998875"
    assert history["snapshots"] == []


def test_route_remove_is_idempotent_and_restore_flow(route: Any) -> None:
    """handle_track_remove / restore：软删幂等 + 回收站外的 404。"""
    route.handle_track_add(_USER, {"code": "998874"})

    payload, status = route.handle_track_remove(_USER, {"code": "998874"})
    assert (status, payload["removed"]) == (200, True)

    payload, status = route.handle_track_remove(_USER, {"code": "998874"})
    assert (status, payload) == (204, {"ok": True}), "重复删是 DELETE 风格的幂等 204"

    missing, status_missing = route.handle_track_restore(_USER, {"code": "998873"})
    assert (status_missing, missing["error"]) == (404, "not_in_recycle")

    restored, status_restored = route.handle_track_restore(_USER, {"code": "998874"})
    assert (status_restored, restored["ok"], restored["code"]) == (200, True, "998874")


def test_route_add_remove_restore_reject_missing_code(route: Any) -> None:
    """三个写 handler 的参数校验：缺 code 一律 400，不碰 DB。"""
    handlers = (route.handle_track_add, route.handle_track_remove, route.handle_track_restore)
    for handler in handlers:
        payload, status = handler(_USER, {})
        assert (status, payload["field"]) == (400, "code")


def _recorder(sink: list[tuple[str, int]], label: str, value: int) -> Any:
    """造一个「记录自己被调用、返回固定值」的替身。

    用来证明某条路径**不该**被走到：一旦被调用，``sink`` 里就留下痕迹；返回 99
    还会让断言里的计数对不上 —— 双重信号。
    """

    def _fake(conn: Any, days: int) -> int:
        sink.append((label, days))
        return value

    return _fake


def test_route_maintenance_dry_run_counts_without_deleting(
    route: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认（无参数）= dry-run：走 ``count_prunable_*`` 报计数，一行都不删。

    R59（审计 M18/M1）：维护端点的默认语义是**只读**，``apply=True`` 才删。契约由
    web 侧钉成 ``dry_run`` / ``applied`` / ``retention`` + 两个 ``pruned_*`` 计数，
    这里同时验证「没走写路径」与「保留期入参透传」。
    """
    counted: list[tuple[str, int]] = []
    pruned: list[tuple[str, int]] = []

    def _fake_count_snapshots(conn: Any, days: int) -> int:
        counted.append(("snapshots", days))
        return 3

    def _fake_count_removed(conn: Any, days: int) -> int:
        counted.append(("removed", days))
        return 2

    monkeypatch.setattr(route.track_store, "count_prunable_snapshots", _fake_count_snapshots)
    monkeypatch.setattr(route.track_store, "count_prunable_removed", _fake_count_removed)
    monkeypatch.setattr(route.track_store, "prune_snapshots", _recorder(pruned, "snapshots", 99))
    monkeypatch.setattr(route.track_store, "prune_removed", _recorder(pruned, "removed", 99))

    payload, status = route.handle_track_maintenance(
        snapshot_retention_days=7, recycle_retention_days=14
    )
    assert (status, payload) == (
        200,
        {
            "ok": True,
            "dry_run": True,
            "applied": False,
            "retention": {"snapshot_days": 7, "recycle_days": 14},
            "pruned_snapshots": 3,
            "pruned_removed": 2,
        },
    )
    assert counted == [("snapshots", 7), ("removed", 14)]
    assert pruned == [], "dry-run 不允许调用任何 prune"


def test_route_maintenance_apply_deletes_and_reports_real_counts(
    route: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apply=True`` 才走 ``prune_*``；N/M 是实际删除行数，不再 count。"""
    pruned: list[tuple[str, int]] = []
    counted: list[tuple[str, int]] = []

    def _fake_prune_snapshots(conn: Any, days: int) -> int:
        pruned.append(("snapshots", days))
        return 3

    def _fake_prune_removed(conn: Any, days: int) -> int:
        pruned.append(("removed", days))
        return 2

    monkeypatch.setattr(route.track_store, "prune_snapshots", _fake_prune_snapshots)
    monkeypatch.setattr(route.track_store, "prune_removed", _fake_prune_removed)
    monkeypatch.setattr(
        route.track_store, "count_prunable_snapshots", _recorder(counted, "snapshots", 99)
    )
    monkeypatch.setattr(
        route.track_store, "count_prunable_removed", _recorder(counted, "removed", 99)
    )

    payload, status = route.handle_track_maintenance(apply=True)
    assert (status, payload["dry_run"], payload["applied"]) == (200, False, True)
    assert (payload["pruned_snapshots"], payload["pruned_removed"]) == (3, 2)
    assert pruned == [
        ("snapshots", route.track_store.SNAPSHOT_RETENTION_DAYS),
        ("removed", route.track_store.RECYCLE_RETENTION_DAYS),
    ]
    assert counted == [], "apply 路径不必（也不许）再 count"


def test_route_maintenance_returns_503_on_store_error(
    route: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry-run 的 count 故障降级 503，不抛 500。"""

    def _boom(conn: Any, days: int) -> int:
        raise route.track_store.TrackStoreError("db down")

    monkeypatch.setattr(route.track_store, "count_prunable_snapshots", _boom)
    payload, status = route.handle_track_maintenance()
    assert status == 503
    assert payload["ok"] is False


def test_route_maintenance_apply_returns_503_on_store_error(
    route: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply 路径的 prune 故障同样降级 503，不抛 500。"""

    def _boom(conn: Any, days: int) -> int:
        raise route.track_store.TrackStoreError("db down")

    monkeypatch.setattr(route.track_store, "prune_snapshots", _boom)
    payload, status = route.handle_track_maintenance(apply=True)
    assert (status, payload["ok"]) == (503, False)
