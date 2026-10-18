"""P2-1 回归：判因探针必须能区分「DB 不可达」与「这天真没有 bar」。

## 病根（修复前）

``cpt/adapters/a_share_local.fetch_raw_bar_on`` 自己 ``psycopg.connect``，
``except Exception: return None``。消费方 ``_stale_reason`` 拿 ``None`` 直接断言
``stale_reason="upstream_lag"``，面板与巡检告警上写的是**「上游当日 bar 未入库」**。
于是 DB 一挂，运维拿到的是一句**确信的错话** —— 他会去查 emotion-core 的入库管道，
而真相是 CPT 自己连不上库。诊断字段的全部价值就在「确定」二字上。

## 修法

adapters 加 ``raw_bar_exists_on(conn, code, day) -> bool``：走**调用方的共享连接**
（不另建连），**判不了就抛**，由 ``_stale_reason`` 统一 ``except`` → 回滚 → ``None``
（不写原因）。第一态的三种答案于是分得开：``True`` / ``False`` / 抛。

## 本文件与 ``test_ashare_market_overview_r61.py`` 的分工

那里用同名替身钉端到端语义（不依赖 psycopg）；这里用**真的 psycopg 异常类**，
因为要证明的是「真驱动抛出来的东西也是这么走的」—— 尤其是
``psycopg.errors.UndefinedTable``（表被改名/权限丢了）与 ``OperationalError``
（连不上）这两类在真机上完全不同、但都被旧实现吞成一模一样。
"""

from __future__ import annotations

from datetime import date

import pytest

psycopg = pytest.importorskip("psycopg")

from cpt.adapters import a_share_local as local  # noqa: E402
from cpt.application import a_share_snapshot as snap  # noqa: E402


class _Cur:
    """只实现 ``cursor()`` 上下文 + ``execute`` / ``fetchone`` 的最小替身。"""

    def __init__(self, *, row: object = None, exc: BaseException | None = None) -> None:
        self.row = row
        self.exc = exc
        self.sql: str | None = None
        self.params: object = None

    def __enter__(self) -> _Cur:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: object) -> None:
        self.sql = sql
        self.params = params
        if self.exc is not None:
            raise self.exc

    def fetchone(self) -> object:
        return self.row


class _Conn:
    def __init__(self, *, row: object = None, exc: BaseException | None = None) -> None:
        self.cur = _Cur(row=row, exc=exc)
        self.rollbacks = 0

    def cursor(self) -> _Cur:
        return self.cur

    def rollback(self) -> None:
        self.rollbacks = 1 if self.rollbacks == 0 else self.rollbacks
        self.rollbacks += 1


# ── 第一态：raw_bar_exists_on 的三值语义 ───────────────────────────────────────


def test_raw_bar_exists_on_true_when_the_row_has_a_close() -> None:
    conn = _Conn(row=(1286.0,))
    assert local.raw_bar_exists_on(conn, "600519", date(2026, 10, 14)) is True


def test_raw_bar_exists_on_false_when_there_is_no_row() -> None:
    conn = _Conn(row=None)
    assert local.raw_bar_exists_on(conn, "600519", date(2026, 10, 14)) is False


def test_raw_bar_exists_on_false_when_close_is_null() -> None:
    """行在、``close`` 为 NULL ⇒ K 线用不了，与「没有行」等价（对齐旧判据）。"""
    conn = _Conn(row=(None,))
    assert local.raw_bar_exists_on(conn, "600519", date(2026, 10, 14)) is False


def test_raw_bar_exists_on_uses_the_callers_connection_not_a_new_one() -> None:
    """必须走调用方的连接 —— 旧实现自己 ``psycopg.connect``，多建一次连纯属浪费。"""
    conn = _Conn(row=(1.0,))
    local.raw_bar_exists_on(conn, "600519", date(2026, 10, 14))
    assert conn.cur.sql is not None
    assert "public.daily_bar" in conn.cur.sql
    assert "date=%s" in conn.cur.sql
    assert conn.cur.params == ("600519", date(2026, 10, 14))


@pytest.mark.parametrize(
    "exc",
    [
        psycopg.OperationalError("could not connect to server"),
        psycopg.errors.UndefinedTable('relation "public.daily_bar" does not exist'),
        psycopg.errors.UndefinedColumn('column "close" does not exist'),
        psycopg.InterfaceError("connection already closed"),
        TimeoutError("statement timeout"),
    ],
)
def test_raw_bar_exists_on_raises_instead_of_returning_false(exc: BaseException) -> None:
    """★核心：**任何**判不了的情况都抛，绝不返回 ``False``。

    返回 ``False`` 在上游会被翻译成「上游当日 bar 未入库」这个**确信的结论**。
    这里是 ``False`` 与「判不了」唯一的、也是必须守住的分界线。
    """
    conn = _Conn(exc=exc)
    with pytest.raises(type(exc)):
        local.raw_bar_exists_on(conn, "600519", date(2026, 10, 14))


# ── 端到端：_stale_reason 在真异常类型下不写原因 ────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        psycopg.OperationalError("could not connect to server"),
        psycopg.errors.UndefinedTable('relation "public.daily_bar" does not exist'),
        psycopg.InterfaceError("connection already closed"),
    ],
)
def test_stale_reason_omits_the_reason_and_rolls_back(monkeypatch, exc: BaseException) -> None:
    """真 psycopg 异常 ⇒ 不写原因 + 回滚共享连接（事务已 aborted，回滚是唯一正确时机）。"""
    conn = _Conn(exc=exc)
    monkeypatch.setattr(local, "hfq_factor_on", lambda *a, **k: 6.39)

    assert snap._stale_reason(conn, "600519", {date(2026, 10, 14)}) is None
    assert conn.rollbacks >= 1, "判因失败后必须回滚共享连接"


def test_stale_reason_still_classifies_the_three_states(monkeypatch) -> None:
    """回滚只加在 except 分支上 —— 三态分类本身不许被改动（R62 的判据来源）。"""
    monkeypatch.setattr(local, "hfq_factor_on", lambda *a, **k: 6.39)
    assert snap._stale_reason(_Conn(row=None), "600519", {date(2026, 10, 14)}) == (
        "upstream_lag",
        "2026-10-14",
    )

    monkeypatch.setattr(local, "hfq_factor_on", lambda *a, **k: None)
    assert snap._stale_reason(_Conn(row=(1286.0,)), "600519", {date(2026, 10, 14)}) == (
        "factor_missing",
        "2026-10-14",
    )
