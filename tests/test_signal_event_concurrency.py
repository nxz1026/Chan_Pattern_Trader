"""审计 M17 / L12 的持久化回归（需要真实 Postgres）。

- **M17**：``record_signal_event`` 原来是「先读当前状态、再写」的两步去重，而 r21
  迁移**故意不加** ``UNIQUE(signal_id, status)``（同一状态在「失效→重新准备」时可以
  合法重现）。ThreadingHTTPServer + 每请求一连 ⇒ 两个请求可能同时通过去重检查、
  各插一条重复跃迁，污染「当前状态 = 最新事件」这个真相源。修法是按
  ``signal_id`` 取**事务级** ``pg_advisory_xact_lock`` 串行化，并在**锁内**重读最新
  状态 —— 这里就用两条真连接并发验证它。
- **L12**：``load_trade_decisions`` 的日切查询要有 ``transition_time`` 索引
  （R59 迁移）。这里只断言索引存在与列口径；不钉 ``EXPLAIN`` 计划 —— 测试表行数少，
  规划器选顺序扫是合理的，钉计划会变成假红。
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

import pytest
from cpt.domain.models import Signal

_ROOT = Path(__file__).resolve().parents[1]
_R21 = _ROOT / "scripts" / "migrations" / "2026-10-01_r21_signal_event.sql"
_R59 = _ROOT / "scripts" / "migrations" / "2026-10-10_r59_signal_event_transition_time_idx.sql"
_INDEX_NAME = "idx_cpt_signal_event_transition_time"


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
def sig_db() -> Any:
    """应用 R21 + R59（都幂等）后的连接；收尾删掉本模块写的事件。"""
    conn = _open_pg_conn()
    for path in (_R21, _R59):
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()
    created: list[str] = []
    yield conn, created
    for signal_id in created:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.cpt_signal_event WHERE signal_id = %s", (signal_id,))
    conn.commit()
    conn.close()


def _unique_signal_id() -> str:
    return f"first_buy:0:level0:m17{uuid.uuid4().hex[:10]}"


_SignalStatus = Literal["structure_ready", "alert", "candidate", "confirmed", "invalidated"]


def _signal(signal_id: str, status: _SignalStatus) -> Signal:
    return Signal(
        signal_id=signal_id,
        level=0,
        signal_type="first_buy",
        status=status,
        structure_id="level0:zs1",
        center_ids=("zs0", "zs1"),
        divergence_status="not_checked",
        alert_time=None,
        candidate_time=None,
        confirmed_time=None,
        invalidated_time=None,
        price=100.0,
        source_revision=0,
    )


def _count_events(conn: Any, signal_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM public.cpt_signal_event WHERE signal_id = %s",
            (signal_id,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


# --------------------------------------------------------------------------- #
# M17：串行化 + 锁内重读
# --------------------------------------------------------------------------- #


def test_concurrent_same_transition_is_serialized(sig_db: Any) -> None:
    """审计 M17：两条真连接同时写同一个跃迁，只能有一条落库。"""
    from cpt.storage.signal_event_store import record_signal_event

    conn, created = sig_db
    signal_id = _unique_signal_id()
    created.append(signal_id)
    code = "998871"

    # 播种：当前状态 = structure_ready
    assert record_signal_event(
        conn, _signal(signal_id, "structure_ready"), prev_status=None, code=code, event_time=0
    )
    conn.commit()

    target = _signal(signal_id, "alert")
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[BaseException] = []

    def _worker() -> None:
        worker_conn = _open_pg_conn()
        try:
            barrier.wait(timeout=10)
            written = record_signal_event(
                worker_conn, target, prev_status="structure_ready", code=code, event_time=0
            )
            worker_conn.commit()
            results.append(written)
        except BaseException as exc:  # noqa: BLE001 — 线程里的异常必须带回主线程
            errors.append(exc)
            worker_conn.rollback()
        finally:
            worker_conn.close()

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert not any(thread.is_alive() for thread in threads), (
        "并发写入线程超时（advisory lock 泄漏？）"
    )
    assert not errors, f"并发写入抛错: {errors!r}"
    assert sorted(results) == [False, True], "两个都写进去（或都没写）说明没串行化"
    assert _count_events(conn, signal_id) == 2  # 播种 + 唯一一条跃迁


def test_db_side_reread_rejects_duplicate_transition(sig_db: Any) -> None:
    """审计 M17：即使入口 ``prev_status`` 已过期，锁内重读仍挡住重复跃迁。"""
    from cpt.storage.signal_event_store import record_signal_event

    conn, created = sig_db
    signal_id = _unique_signal_id()
    created.append(signal_id)

    assert record_signal_event(
        conn,
        _signal(signal_id, "alert"),
        prev_status="structure_ready",
        code="998871",
        event_time=0,
    )
    conn.commit()

    # 库里 truth 已经是 alert；同一次「跃迁」再来一遍应被拒。
    duplicate = record_signal_event(
        conn,
        _signal(signal_id, "alert"),
        prev_status="structure_ready",
        code="998871",
        event_time=0,
    )
    assert duplicate is False
    conn.rollback()  # 释放重读时拿的 advisory lock，顺带丢掉空事务
    assert _count_events(conn, signal_id) == 1


# --------------------------------------------------------------------------- #
# L12：transition_time 索引
# --------------------------------------------------------------------------- #


def test_transition_time_index_exists(sig_db: Any) -> None:
    """审计 L12：R59 迁移要为日切查询建好 ``transition_time`` 索引。"""
    conn, _created = sig_db
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            " WHERE schemaname = 'public' AND tablename = 'cpt_signal_event' "
            "   AND indexname = %s",
            (_INDEX_NAME,),
        )
        row = cur.fetchone()
    assert row is not None, f"R59 迁移没建出 {_INDEX_NAME}"
    assert "transition_time" in row[0], f"索引列口径不对: {row[0]}"
