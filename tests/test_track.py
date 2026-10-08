"""我的追踪 — 仓储 + Web API 集成测试。

DB 相关用例 ``importorskip`` 在 fixture 内部触发（无 psycopg / 不可连时**只** skip
该 fixture 依赖的测试，纯逻辑测试照跑）；非 DB 用例覆盖 ``extract_user_id`` /
``route_for``。
"""

from __future__ import annotations

from typing import Any

import pytest

# ── 纯逻辑：header 提取 + 路由解析 ─────────────────────────────────


class _Headers:
    """模拟 ``BaseHTTPRequestHandler.headers.get`` 的最小接口。"""

    def __init__(self, raw: dict[str, str]) -> None:
        self._raw = {k.lower(): v for k, v in raw.items()}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._raw.get(key.lower(), default)


def test_extract_user_id_uses_default_when_header_missing() -> None:
    from cpt.web.track_api import DEFAULT_USER_ID, extract_user_id

    assert extract_user_id(_Headers({})) == DEFAULT_USER_ID


def test_extract_user_id_strips_whitespace() -> None:
    from cpt.web.track_api import extract_user_id

    assert extract_user_id(_Headers({"X-CPT-User": "  alice  "})) == "alice"


def test_extract_user_id_rejects_empty_after_strip() -> None:
    from cpt.web.track_api import DEFAULT_USER_ID, extract_user_id

    assert extract_user_id(_Headers({"X-CPT-User": "   "})) == DEFAULT_USER_ID


def test_extract_user_id_rejects_non_alnum_chars() -> None:
    from cpt.web.track_api import DEFAULT_USER_ID, extract_user_id

    for bad in ("ali ce", "alice@home", "alice!bob", "alice/bob", "alice+bob"):
        assert extract_user_id(_Headers({"X-CPT-User": bad})) == DEFAULT_USER_ID


def test_extract_user_id_accepts_alnum_dot_dash_underscore() -> None:
    from cpt.web.track_api import extract_user_id

    for ok in ("alice", "alice.bob", "alice-bob", "alice_bob", "u123", "A1"):
        assert extract_user_id(_Headers({"X-CPT-User": ok})) == ok


def test_extract_user_id_truncates_to_max_len() -> None:
    from cpt.web.track_api import MAX_USER_ID_LEN, extract_user_id

    raw = "a" * (MAX_USER_ID_LEN + 50)
    out = extract_user_id(_Headers({"X-CPT-User": raw}))
    assert out == "a" * MAX_USER_ID_LEN


def test_route_for_list_and_advice_and_history() -> None:
    from cpt.web.track_api import route_for

    assert route_for("/api/dashboard/track") == ("list", "")
    assert route_for("/api/dashboard/track/600519/advice") == ("advice", "600519")
    assert route_for("/api/dashboard/track/600519/history") == ("history", "600519")
    assert route_for("/api/dashboard/track/maintenance") == ("maintenance", "")


def test_route_for_rejects_unknown_subpath() -> None:
    from cpt.web.track_api import route_for

    assert route_for("/api/dashboard/track/zzz") is None
    assert route_for("/api/dashboard/track/600519/junk") is None
    assert route_for("/api/trade/decisions") is None
    assert route_for("/") is None


# ── 仓储：需 psycopg ──────────────────────────────────────────────────


@pytest.fixture
def pg_conn() -> Any:
    # 在 fixture 内 import + skip：**只**让本 fixture 触发的测试跳过，
    # 上面的纯逻辑测试不受影响。
    pytest.importorskip("psycopg")
    """用 :class:`AShareLocalClient` 拿连接，跳过若不可达。

    不硬连 127.0.0.1 —— 让生产 oracle 与本机都能参与（两边都设 ``CPT_DB_DSN`` 或
    各自 ``~/.dbconfig``）。连不上时本机集成测试整组 skip。
    """
    from cpt.adapters.a_share_local import AShareLocalClient

    client = AShareLocalClient()
    try:
        conn = client._get_conn()  # noqa: SLF001 — 同 trade_api 用法
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB not reachable: {type(exc).__name__}: {str(exc)[:120]}")
    yield conn
    conn.close()


def test_add_then_list_includes_row(pg_conn: Any) -> None:
    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    code = "998877"  # 测试用代码，不与生产冲突
    try:
        track_store.add(pg_conn, "default", code, note="test")
        items = track_store.list_active(pg_conn, "default")
        codes = [r["code"] for r in items]
        assert code in codes
    finally:
        # 清理
        pg_conn.rollback()
        cur = pg_conn.cursor()
        cur.execute(
            "DELETE FROM public.cpt_track WHERE user_id = %s AND code = %s",
            ("default", code),
        )
        pg_conn.commit()


def test_add_idempotent_when_already_active(pg_conn: Any) -> None:
    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    code = "998878"
    try:
        first = track_store.add(pg_conn, "default", code, note="first")
        first_added_at = first["added_at"]
        second = track_store.add(pg_conn, "default", code, note="second")
        # 已存在不报错，``added_at`` 不变（不重置成 now）
        assert second["added_at"] == first_added_at
    finally:
        pg_conn.rollback()
        cur = pg_conn.cursor()
        cur.execute("DELETE FROM public.cpt_track WHERE user_id=%s AND code=%s", ("default", code))
        pg_conn.commit()


def test_add_resurrects_after_remove(pg_conn: Any) -> None:
    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    code = "998879"
    try:
        track_store.add(pg_conn, "default", code)
        assert track_store.remove(pg_conn, "default", code) is True
        # 软删后，``add`` 复活
        resurrected = track_store.add(pg_conn, "default", code)
        assert resurrected["removed_at"] is None
    finally:
        pg_conn.rollback()
        cur = pg_conn.cursor()
        cur.execute("DELETE FROM public.cpt_track WHERE user_id=%s AND code=%s", ("default", code))
        pg_conn.commit()


def test_remove_then_list_active_excludes_but_list_removed_includes(pg_conn: Any) -> None:
    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    code = "998880"
    try:
        track_store.add(pg_conn, "default", code)
        assert track_store.remove(pg_conn, "default", code) is True
        active = [r["code"] for r in track_store.list_active(pg_conn, "default")]
        removed = [r["code"] for r in track_store.list_removed(pg_conn, "default")]
        assert code not in active
        assert code in removed
    finally:
        pg_conn.rollback()
        cur = pg_conn.cursor()
        cur.execute("DELETE FROM public.cpt_track WHERE user_id=%s AND code=%s", ("default", code))
        pg_conn.commit()


def test_remove_returns_false_for_unknown_code(pg_conn: Any) -> None:
    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    # 既不在活跃也不在回收站
    assert track_store.remove(pg_conn, "default", "000000") is False


def test_restore_returns_false_for_active_row(pg_conn: Any) -> None:
    """活跃行 ``restore`` 是 no-op 且返 False（不在回收站里）。"""
    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    code = "998881"
    try:
        track_store.add(pg_conn, "default", code)
        assert track_store.restore(pg_conn, "default", code) is False
    finally:
        pg_conn.rollback()
        cur = pg_conn.cursor()
        cur.execute("DELETE FROM public.cpt_track WHERE user_id=%s AND code=%s", ("default", code))
        pg_conn.commit()


def test_users_are_isolated(pg_conn: Any) -> None:
    """user A 的追踪**不可**被 user B 看到。"""
    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    code = "998882"
    try:
        track_store.add(pg_conn, "userA", code)
        track_store.add(pg_conn, "userB", code, note="other")
        a_codes = [r["code"] for r in track_store.list_active(pg_conn, "userA")]
        b_codes = [r["code"] for r in track_store.list_active(pg_conn, "userB")]
        assert code in a_codes
        assert code in b_codes
        # userA 不能 remove userB 的
        assert track_store.remove(pg_conn, "userA", code) is False  # userB 的仍在
        assert track_store.remove(pg_conn, "userB", code) is True
    finally:
        pg_conn.rollback()
        cur = pg_conn.cursor()
        cur.execute(
            "DELETE FROM public.cpt_track WHERE user_id IN (%s, %s) AND code=%s",
            ("userA", "userB", code),
        )
        pg_conn.commit()


def test_record_snapshot_updates_last_advice(pg_conn: Any) -> None:
    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    code = "998883"
    try:
        track_store.add(pg_conn, "default", code)
        payload = {"code": code, "as_of": "2026-10-08T10:00:00Z", "current": {"price": 100}}
        track_store.record_snapshot(
            pg_conn,
            "default",
            code,
            payload,
            current_summary=payload["current"],
        )
        items = track_store.list_active(pg_conn, "default")
        row = next(r for r in items if r["code"] == code)
        assert row["last_advice_at"] is not None
        assert row["last_current"]["price"] == 100
    finally:
        pg_conn.rollback()
        cur = pg_conn.cursor()
        cur.execute("DELETE FROM public.cpt_track WHERE user_id=%s AND code=%s", ("default", code))
        cur.execute(
            "DELETE FROM public.cpt_track_snapshot WHERE user_id=%s AND code=%s",
            ("default", code),
        )
        pg_conn.commit()


def test_prune_snapshots_removes_old_rows(pg_conn: Any) -> None:
    """prune 删 ``as_of < now - retention_days`` 的。"""
    from datetime import UTC, datetime, timedelta

    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    code = "998884"
    try:
        # 插一条"老"快照
        old_ts = datetime.now(UTC) - timedelta(days=SNAPSHOT_DAYS_PLUS_1)
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.cpt_track_snapshot (user_id, code, as_of, payload) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                ("default", code, old_ts, '{"x":1}'),
            )
        deleted = track_store.prune_snapshots(pg_conn, retention_days=SNAPSHOT_DAYS_PLUS_1)
        assert deleted >= 1
    finally:
        pg_conn.rollback()
        cur = pg_conn.cursor()
        cur.execute(
            "DELETE FROM public.cpt_track_snapshot WHERE user_id=%s AND code=%s",
            ("default", code),
        )
        pg_conn.commit()


SNAPSHOT_DAYS_PLUS_1 = 31  # > SNAPSHOT_RETENTION_DAYS=30
