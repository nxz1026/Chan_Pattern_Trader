"""我的追踪 — 仓储 + Web API 集成测试。

DB 相关用例 ``importorskip`` 在 fixture 内部触发（无 psycopg / 不可连时**只** skip
该 fixture 依赖的测试，纯逻辑测试照跑）；非 DB 用例覆盖 ``extract_user_id`` /
``route_for``。

⚠️ R59（审计 H7）：CI 原先**从不安装 ``db`` extra**，于是本文件所有仓储用例在 CI
上整组 skip，持久化层实际零门禁 —— 而 skip 在 ``-rsq`` 里只是一行 ``s``。现在 CI
装 ``.[db]``、起 Postgres 服务并设 ``CPT_REQUIRE_DB=1``：那种环境下连不上就是**红**。
"""

from __future__ import annotations

import os
from typing import Any

import pytest


def require_db() -> bool:
    """``CPT_REQUIRE_DB=1`` 时 DB 用例**不许静默 skip**（R59 / 审计 H7）。

    默认关闭：本地没库时照旧整组 skip（不阻塞纯逻辑开发）。
    """
    return os.environ.get("CPT_REQUIRE_DB", "").strip().lower() in {"1", "true", "yes"}


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


def _open_pg_conn() -> Any:
    """建连并 yield；不可达时 skip（``CPT_REQUIRE_DB=1`` 时改为 fail）。

    独立成函数（而非直接写在 fixture 里）是为了**可被单测直接驱动** ——
    见 ``test_pg_connection_is_not_silently_skipped_when_required``。
    """
    # 在 fixture 内 import + skip：**只**让本 fixture 触发的测试跳过，
    # 上面的纯逻辑测试不受影响。R59：``CPT_REQUIRE_DB=1`` 时改成硬失败。
    try:
        import psycopg  # noqa: F401 — 只探可用性
    except ImportError as exc:
        message = f"psycopg 不可用（需要 `pip install -e '.[db]'`）: {exc}"
        if require_db():
            pytest.fail(message)
        pytest.skip(message)

    from cpt.adapters.a_share_local import AShareLocalClient

    client = AShareLocalClient()
    try:
        conn = client._get_conn()  # noqa: SLF001 — 同 trade_api 用法
    except Exception as exc:  # noqa: BLE001
        message = f"DB not reachable: {type(exc).__name__}: {str(exc)[:120]}"
        if require_db():
            pytest.fail(message)
        pytest.skip(message)
    yield conn
    conn.close()


@pytest.fixture
def pg_conn() -> Any:
    """仓储/接口用例的连接。

    不硬连 127.0.0.1 —— 让生产 oracle 与本机都能参与（两边都设 ``CPT_DB_DSN`` 或
    各自 ``~/.dbconfig``）。连不上时本机集成测试整组 skip。
    """
    yield from _open_pg_conn()


def test_pg_connection_is_not_silently_skipped_when_required(monkeypatch: Any) -> None:
    """R59 / 审计 H7：``CPT_REQUIRE_DB=1`` 时连不上必须**失败**，不能静默 skip。

    这是那个修复的**自测**：审计发现 CI 上持久化层整组 skip 而没人注意，
    所以「skip 还是 fail」这件事本身要有测试钉住，否则以后一次重构就能
    悄悄把硬失败改回 skip。
    """
    from cpt.adapters import a_share_local

    def _boom(self: Any) -> Any:  # noqa: ANN401
        raise RuntimeError("connection refused (simulated)")

    monkeypatch.setattr(a_share_local.AShareLocalClient, "_get_conn", _boom)

    monkeypatch.setenv("CPT_REQUIRE_DB", "1")
    with pytest.raises(pytest.fail.Exception, match="DB not reachable"):
        next(_open_pg_conn())

    # 未要求时保持原状：本地没库照旧 skip，不阻塞纯逻辑开发。
    monkeypatch.delenv("CPT_REQUIRE_DB")
    with pytest.raises(pytest.skip.Exception, match="DB not reachable"):
        next(_open_pg_conn())


def test_require_db_env_parsing(monkeypatch: Any) -> None:
    for raw in ("1", "true", "TRUE", " yes "):
        monkeypatch.setenv("CPT_REQUIRE_DB", raw)
        assert require_db() is True, raw
    for raw in ("", "0", "no", "false"):
        monkeypatch.setenv("CPT_REQUIRE_DB", raw)
        assert require_db() is False, raw


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
    """既不在活跃也不在回收站 ⇒ ``remove`` 返 False。

    审计 L14：原断言用 ``"000000"``，结果取决于真实库里**恰好**没有这个码；
    现在用独占测试码并先清干净，断言不再依赖外部状态。
    """
    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    code = "998885"  # 独占测试码，不与生产/其他用例共享
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM public.cpt_track WHERE user_id=%s AND code=%s", ("default", code)
            )
        pg_conn.commit()
        assert track_store.remove(pg_conn, "default", code) is False
    finally:
        pg_conn.rollback()
        cur = pg_conn.cursor()
        cur.execute("DELETE FROM public.cpt_track WHERE user_id=%s AND code=%s", ("default", code))
        pg_conn.commit()


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
    """user A 的追踪**不可**被 user B 看到，也删不掉 B 的行。

    两者可以追踪同一只票 —— ``cpt_track`` 主键是 ``(user_id, code)``，
    ``remove`` 的 WHERE 带 ``user_id``，所以 userA 删的是**自己那一行**，
    返 ``True``。（原断言 ``is False`` 把「删不到 userB」误解成了「删不到任何行」，
    2026-10-08 审计列为既有红灯，定性为**测试错**。）
    """
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
        # userA 删自己那行 ⇒ True
        assert track_store.remove(pg_conn, "userA", code) is True
        # 关键：userB 那行必须**原封不动**（这才是隔离）
        assert code in [r["code"] for r in track_store.list_active(pg_conn, "userB")]
        assert code not in [r["code"] for r in track_store.list_active(pg_conn, "userA")]
        # userA 已无活跃行 ⇒ 再删返 False，同样碰不到 userB
        assert track_store.remove(pg_conn, "userA", code) is False
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


def test_prune_snapshots_is_scoped_and_spares_other_users(pg_conn: Any) -> None:
    """prune 删 ``as_of < now - retention_days`` 的，且**只在作用域内**删。

    审计 H6 回归：原用例调用无作用域的 ``prune_snapshots(pg_conn, 31)``，
    而 ``conn`` 指向真实库、该 SQL 无任何主体过滤、函数内部还 ``commit`` ——
    跑一次测试就会清掉**所有用户** 31 天前的快照，``finally`` 的 rollback 救不回来。
    这里同时插入「自己」与「别的用户」两条陈年快照，断言前者被删、后者幸存。
    """
    from datetime import UTC, datetime, timedelta

    from cpt.storage import track_store

    track_store.ensure_table(pg_conn)
    code = "998884"
    other_user = "998884-other"
    try:
        old_ts = datetime.now(UTC) - timedelta(days=SNAPSHOT_DAYS_PLUS_1)
        with pg_conn.cursor() as cur:
            for uid in ("default", other_user):
                cur.execute(
                    "INSERT INTO public.cpt_track_snapshot (user_id, code, as_of, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb)",
                    (uid, code, old_ts, '{"x":1}'),
                )
        pg_conn.commit()
        deleted = track_store.prune_snapshots(
            pg_conn, retention_days=SNAPSHOT_DAYS_PLUS_1, user_id="default", code=code
        )
        assert deleted == 1  # 作用域内恰一条
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM public.cpt_track_snapshot WHERE user_id=%s AND code=%s",
                (other_user, code),
            )
            assert cur.fetchone()[0] == 1  # 别人的陈年快照没被带走
    finally:
        pg_conn.rollback()
        cur = pg_conn.cursor()
        cur.execute(
            "DELETE FROM public.cpt_track_snapshot WHERE user_id IN (%s, %s) AND code=%s",
            ("default", other_user, code),
        )
        pg_conn.commit()


SNAPSHOT_DAYS_PLUS_1 = 31  # > SNAPSHOT_RETENTION_DAYS=30
