"""审计 M15 / M16 的持久化回归（需要真实 Postgres）。

- **M15**：``rate_limited`` 会真实落库，但退避等待只活在**内存队列**里；进程在退避
  窗口被杀 ⇒ 该行永停 ``rate_limited``、``finished_at`` 恒 NULL。``mark_interrupted``
  的清扫集合必须含它（连同原有的 queued / running）。
- **M16**：``finish_call`` 的 UPDATE 必须带「当前不是终态」守卫 —— 晚到的回调不能把
  已经 ``interrupted`` 的行拉回 ``ok``。

**隔离手法**：本模块的写入与清扫**都不 commit**，断言在同一事务内完成，收尾
``rollback()``。``mark_interrupted`` 是全局 UPDATE：一旦提交，会把共享测试库里
别人的陈年行一并带走 —— 这正是 H6 的教训（破坏性 SQL 在无作用域下被测试调用）。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_R25 = _ROOT / "scripts" / "migrations" / "2026-10-03_r25_llm_call.sql"

#: 本模块写的行都打这个 purpose 标记（r25 里 purpose 无 CHECK，可自由取值）。
_PURPOSE = "test_m15_m16"


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


@pytest.fixture
def llm_conn() -> Any:
    """应用 R25（幂等）后的连接；收尾清掉本模块写的行。"""
    conn = _open_pg_conn()
    with conn.cursor() as cur:
        cur.execute(_R25.read_text(encoding="utf-8"))
    conn.commit()
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM public.cpt_llm_call WHERE purpose = %s", (_PURPOSE,))
    conn.commit()
    conn.close()


def _new_call_id() -> str:
    return f"test-m15-{uuid.uuid4().hex}"


def _insert_call(conn: Any, call_id: str, *, status: str, created_at: datetime) -> None:
    """直接插行以控制 ``created_at``（``enqueue_call`` 只给 now，压不出水位线）。

    **不 commit** —— 留在外层事务里，收尾一起 rollback。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.cpt_llm_call "
            "(call_id, purpose, subject_id, status, request_hash, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (call_id, _PURPOSE, call_id, status, call_id, created_at),
        )


def _call_row(conn: Any, call_id: str) -> tuple[str, Any, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, result_text, finished_at FROM public.cpt_llm_call WHERE call_id = %s",
            (call_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1], row[2]


# --------------------------------------------------------------------------- #
# M15：重启清扫必须收掉 rate_limited
# --------------------------------------------------------------------------- #


def test_mark_interrupted_sweeps_stale_rate_limited(llm_conn: Any) -> None:
    """审计 M15：陈年 ``rate_limited`` 行必须被收成 ``interrupted`` 且补上 finished_at。"""
    from cpt.storage.llm_call_store import STATUS_INTERRUPTED, STATUS_RATE_LIMITED, mark_interrupted

    call_id = _new_call_id()
    _insert_call(
        llm_conn,
        call_id,
        status=STATUS_RATE_LIMITED,
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    marked = mark_interrupted(llm_conn, before=datetime.now(UTC) - timedelta(hours=1))
    assert marked >= 1
    status, _result, finished_at = _call_row(llm_conn, call_id)
    assert status == STATUS_INTERRUPTED
    assert finished_at is not None, "M15 的另一半：必须在终态时补 finished_at"
    llm_conn.rollback()


def test_mark_interrupted_still_sweeps_queued_and_running(llm_conn: Any) -> None:
    """回归：原有 queued / running 仍被清扫（别为加 rate_limited 把老的丢了）。"""
    from cpt.storage.llm_call_store import (
        STATUS_INTERRUPTED,
        STATUS_QUEUED,
        STATUS_RUNNING,
        mark_interrupted,
    )

    old = datetime.now(UTC) - timedelta(days=2)
    ids = {"queued": _new_call_id(), "running": _new_call_id()}
    _insert_call(llm_conn, ids["queued"], status=STATUS_QUEUED, created_at=old)
    _insert_call(llm_conn, ids["running"], status=STATUS_RUNNING, created_at=old)
    mark_interrupted(llm_conn, before=datetime.now(UTC) - timedelta(hours=1))
    assert _call_row(llm_conn, ids["queued"])[0] == STATUS_INTERRUPTED
    assert _call_row(llm_conn, ids["running"])[0] == STATUS_INTERRUPTED
    llm_conn.rollback()


def test_mark_interrupted_respects_before_watermark(llm_conn: Any) -> None:
    """回归：``before`` 水位线之外（更新）的在途行不许被误伤。

    首次 ``_bootstrap()`` 就可能发生在 ``enqueue_call`` 之后；没有水位线就会把
    本进程刚入队的行标成中断（实测踩过）。
    """
    from cpt.storage.llm_call_store import STATUS_RATE_LIMITED, mark_interrupted

    call_id = _new_call_id()
    _insert_call(
        llm_conn,
        call_id,
        status=STATUS_RATE_LIMITED,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    mark_interrupted(llm_conn, before=datetime.now(UTC) - timedelta(hours=1))
    assert _call_row(llm_conn, call_id)[0] == STATUS_RATE_LIMITED
    llm_conn.rollback()


# --------------------------------------------------------------------------- #
# M16：终态守卫
# --------------------------------------------------------------------------- #


def test_late_finish_call_cannot_overwrite_interrupted(llm_conn: Any) -> None:
    """审计 M16：晚到的 ``finish_call(status='ok')`` 不得覆盖 ``interrupted``。"""
    from cpt.storage.llm_call_store import STATUS_INTERRUPTED, STATUS_OK, finish_call

    call_id = _new_call_id()
    _insert_call(
        llm_conn,
        call_id,
        status=STATUS_INTERRUPTED,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    finish_call(llm_conn, call_id, status=STATUS_OK, result_text="晚到的成功")
    status, result_text, _finished = _call_row(llm_conn, call_id)
    assert status == STATUS_INTERRUPTED
    assert result_text is None, "0 行更新时连 result_text 都不能落进去"
    llm_conn.rollback()


def test_finish_call_still_updates_in_flight_row(llm_conn: Any) -> None:
    """回归：守卫不能把正常路径也挡住（running -> ok）。"""
    from cpt.storage.llm_call_store import STATUS_OK, STATUS_RUNNING, finish_call

    call_id = _new_call_id()
    _insert_call(
        llm_conn,
        call_id,
        status=STATUS_RUNNING,
        created_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    finish_call(llm_conn, call_id, status=STATUS_OK, result_text="完成")
    status, result_text, finished_at = _call_row(llm_conn, call_id)
    assert (status, result_text) == (STATUS_OK, "完成")
    assert finished_at is not None
    llm_conn.rollback()
