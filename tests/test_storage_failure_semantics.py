"""storage/ 层的**失败语义**契约（R45）。

**全程离线**：不碰 DB、不联网。

## 这组测试为什么必须用真函数，不能 monkeypatch

`tests/test_a_share_rollback.py::test_load_previous_signal_failure_does_not_propagate`
把 `load_previous_signal` monkeypatch 成一个会抛的替身，然后断言「调用方会
rollback」。它**绿**，但真函数当时根本不抛 —— 于是调用方那段
`_rollback_quietly` 是**死代码**，而测试一无所觉。

也就是说：**测试的是 mock 的行为，坏的是真路径。** 这组测试全部走真函数。

## PostgreSQL 的事务语义（实测，非推断）

事务里一条语句失败后，**同一连接**的后续语句全部报：

    InFailedSqlTransaction: current transaction is aborted,
    commands ignored until end of transaction block

所以任何「catch 住 DB 异常并返回空值」的函数，都会把连接留在 aborted 态，
**后面每一个查询都失败**。这不是「降级」，是把一次局部失败放大成整页失败。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _store(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_st_{name}", ROOT / "cpt" / "storage" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_st_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


ses = _store("signal_event_store")
lcs = _store("llm_call_store")


class _FailingCursor:
    """模拟「PG 语句失败」：execute 抛，连接进入 aborted 态。"""

    def __init__(self, conn: "_AbortingConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_FailingCursor":
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def execute(self, *a: Any, **k: Any) -> None:
        self._conn.aborted = True
        raise RuntimeError("simulated PG statement failure")

    def fetchone(self) -> Any:
        return None

    def fetchall(self) -> Any:
        return ()


class _AbortingConn:
    def __init__(self) -> None:
        self.aborted = False
        self.rollbacks = 0

    def cursor(self) -> _FailingCursor:
        return _FailingCursor(self)

    def rollback(self) -> None:
        self.rollbacks += 1
        self.aborted = False


# ---------------------------------------------------------------------------
# 核心契约：读失败必须抛，调用方才有机会 rollback
# ---------------------------------------------------------------------------


def test_load_previous_signal_raises_on_read_failure() -> None:
    """**核心回归**：读失败不能冒充「没有历史」。

    吞掉异常会让调用方的 ``_rollback_quietly`` 变成死代码，
    连接留在 aborted 态，后续查询全废。
    """
    with pytest.raises(ses.SignalEventError):
        ses.load_previous_signal(_AbortingConn(), "first_buy:5:zs1")


def test_load_previous_signal_returns_none_when_row_absent() -> None:
    """对照组：**真的**没有历史 → 返回 ``None``，不抛。

    这条和上面那条一起，把「读不到」与「确实没有」钉成两种不同结果 ——
    混淆它们正是原 bug。
    """

    class _EmptyCursor:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchone(self): return None

    class _EmptyConn:
        def cursor(self): return _EmptyCursor()

    assert ses.load_previous_signal(_EmptyConn(), "first_buy:5:zs1") is None


def test_three_readers_have_consistent_failure_semantics() -> None:
    """同一模块的三个读函数，失败语义必须**一致**（都抛）。

    R45 之前：``latest_status`` 抛、``load_signal_events`` 抛、
    ``load_previous_signal`` 返回 None。同一个模块里三种口径。
    """
    conn = _AbortingConn()
    for fn, args in (
        (ses.latest_status, ("sid",)),
        (ses.load_previous_signal, ("sid",)),
        (ses.load_signal_events, ()),
    ):
        with pytest.raises(ses.SignalEventError):
            fn(conn, *args)


# ---------------------------------------------------------------------------
# 门禁：任何「catch 住 DB 语句失败却返回空值」的函数都会留下 aborted 连接
# ---------------------------------------------------------------------------


def _swallows_db_failure(fn: Any, args: tuple) -> bool:
    """``fn`` 遇到 DB 失败时是否**吞掉**异常（返回而不抛）。"""
    try:
        fn(_AbortingConn(), *args)
    except Exception:
        return False
    return True


@pytest.mark.parametrize(
    ("label", "fn", "args"),
    [
        ("load_previous_signal", ses.load_previous_signal, ("sid",)),
        ("latest_status", ses.latest_status, ("sid",)),
        ("load_signal_events", ses.load_signal_events, ()),
    ],
)
def test_signal_readers_never_swallow_db_failure(label: str, fn: Any, args: tuple) -> None:
    assert not _swallows_db_failure(fn, args), (
        f"{label} 吞掉了 DB 语句失败 —— 连接会留在 aborted 态，后续查询全废"
    )


def test_aborted_connection_breaks_subsequent_queries() -> None:
    """把 PG 语义写死成可执行的断言：一次失败 ⇒ 连接不可再用。

    没有这条，下一个接手的人可能以为「吞掉异常只是少一个字段」。
    """
    conn = _AbortingConn()
    with pytest.raises(ses.SignalEventError):
        ses.load_previous_signal(conn, "sid")
    assert conn.aborted is True, "语句失败后连接应处于 aborted 态"

    # 调用方的责任：rollback 之后连接才恢复
    conn.rollback()
    assert conn.aborted is False
    assert conn.rollbacks == 1
