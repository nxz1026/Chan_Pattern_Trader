"""A 股快照的**事务边界**回归测试（R27-1）。

## 背景：这个缺陷为什么测起来不像同义反复

R24 起台账一直挂着一条：``a_share_snapshot.py`` 的 except 分支**缺
``conn.rollback()``**。裸断言「某函数被调用了 rollback」是同义反复 —— 证明不了
生产故障被修掉了。

所以这里的假连接**如实模拟 psycopg 的事务语义**：

1. 一条语句在事务内报错 → 整条连接进 **aborted** 态；
2. aborted 态下**任何**语句都抛 ``InFailedSqlTransaction``；
3. 只有 ``rollback()`` 能把它解除。

于是测试的判据可以是**行为**而不是调用记录：故意让中间某条 SQL 失败，然后
断言**后续**查询仍能成功。修复前必然红（后续查询被毒死），修复后绿。
这正是生产上的故障形态：一只查失败，后面几十只全部降级。
"""

from __future__ import annotations

import types
from typing import Any

import pytest
from cpt.application import a_share_snapshot as mod


class InFailedSqlTransaction(RuntimeError):
    """模拟 psycopg 的 aborted 态报错。"""


class _AbortableCursor:
    """按 SQL 关键字决定成功/失败，并维护连接的 aborted 态。"""

    def __init__(self, conn: _AbortableConn) -> None:
        self._conn = conn
        self._result: list[Any] = []

    def execute(self, sql: str, *args: Any) -> None:
        flat = " ".join(sql.split()).lower()
        self._conn.queries.append(flat)
        if self._conn.aborted:
            # aborted 态：真 PG 在这里无条件拒绝，与具体 SQL 无关
            raise InFailedSqlTransaction("current transaction is aborted, commands ignored")
        for marker in self._conn.poison_on:
            if marker in flat:
                self._conn.aborted = True
                self._conn.poisoned.append(marker)
                raise RuntimeError(f"simulated sql failure on {marker}")
        self._result = [(True,)] if "trade_calendar" in flat else []

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[Any]:
        return list(self._result)

    def __enter__(self) -> _AbortableCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _AbortableConn:
    def __init__(self, poison_on: list[str] | None = None) -> None:
        #: 命中任一关键字的 SQL 会失败并把连接打成 aborted
        self.poison_on: list[str] = poison_on or []
        self.aborted = False
        self.rollback_count = 0
        self.commit_count = 0
        self.queries: list[str] = []
        self.poisoned: list[str] = []
        self.rollback_broken = False

    def cursor(self) -> _AbortableCursor:
        return _AbortableCursor(self)

    def commit(self) -> None:
        self.commit_count += 1
        self.aborted = False

    def rollback(self) -> None:
        if self.rollback_broken:
            raise RuntimeError("rollback itself failed")
        self.rollback_count += 1
        self.aborted = False


class _AbortableClient:
    """``AShareLocalClient`` 的 duck type，共享同一条连接。"""

    def __init__(self, poison_on: list[str] | None = None) -> None:
        self.conn = _AbortableConn(poison_on)

    def _get_conn(self) -> _AbortableConn:
        return self.conn

    def close(self) -> None:
        return None

    def fetch_daily_tags(self, code: str, start_ms: int, end_ms: int) -> list[Any]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM public.derived_bar WHERE code = %s", code)
            return list(cur.fetchall())

    def fetch_security_name(self, code: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT name FROM asel.security_master WHERE code = %s", code)
            row = cur.fetchone()
        return str(row[0]) if row else ""


def _still_usable(client: _AbortableClient) -> bool:
    """后续查询还能不能跑 —— 这才是「有没有被毒死」的真实判据。"""
    try:
        with client.conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchall()
    except InFailedSqlTransaction:
        return False
    return True


# --------------------------------------------------------------------------- #
# 助手本身
# --------------------------------------------------------------------------- #


def test_rollback_quietly_clears_aborted_state() -> None:
    client = _AbortableClient()
    client.conn.aborted = True

    mod._rollback_quietly(client, "unit")

    assert client.conn.aborted is False
    assert client.conn.rollback_count == 1


def test_rollback_quietly_swallows_its_own_failure() -> None:
    """回滚本身炸了也不能抛 —— 降级路径绝不允许制造新异常。"""
    client = _AbortableClient()
    client.conn.aborted = True
    client.conn.rollback_broken = True

    mod._rollback_quietly(client, "unit")  # 不抛即通过

    assert client.conn.aborted is True  # 回滚失败，状态没能解除（如实）


def test_rollback_quietly_tolerates_client_without_conn() -> None:
    mod._rollback_quietly(object(), "unit")  # 不抛即通过


# --------------------------------------------------------------------------- #
# 各 attach 路径：出错后必须能救回连接
# --------------------------------------------------------------------------- #


def test_signal_change_no_longer_touches_db() -> None:
    """``_attach_signal_change`` 不再查库 ⇒ 也不会再因查库失败而中毒连接。

    2026-10-06：原来它自己 ``latest_status(conn, signal_id)`` 查上一轮状态，
    但那时 ``_derive_first_buy_signal`` 已经写完并 commit，查到的必然是**本轮
    刚写的那条**，``signal_changed`` 恒 False。现在前值由 ``_derive_*`` 在写
    之前捕获并回传，本函数只做比较 —— 于是「查库失败 → 必须 rollback 救连接」
    这条纪律在这里**不再适用**，它归到了 ``_derive_first_buy_signal``（见
    ``test_load_previous_signal_failure_does_not_propagate``）。

    这里用同一个会毒化 ``cpt_signal_event`` 的连接反证：函数照常算出结果，
    连接**一次都没被碰**，所以没有 rollback 可做，也没有留下 aborted 态。
    """
    client = _AbortableClient(poison_on=["cpt_signal_event"])
    snapshot: dict[str, Any] = {
        "signal": {"status": "confirmed", "signal_id": "first_buy:1:abc"},
        "summary": {},
        "market": {"symbol": "000011"},
    }

    mod._attach_signal_change(snapshot, client, prev_status="structure_ready")

    # 前值是调用方给的，所以这里真的算出了「变了」—— 旧实现恒为 False
    assert snapshot["summary"]["signal_changed"] is True
    assert snapshot["summary"]["signal_change_type"] == "structure_ready→confirmed"
    # 没查库 ⇒ 没被毒、也没多余 rollback
    assert client.conn.rollback_count == 0
    assert _still_usable(client) is True


def test_close_countdown_failure_rolls_back() -> None:
    client = _AbortableClient(poison_on=["trade_calendar"])
    snapshot: dict[str, Any] = {"market": {"symbol": "000011"}}

    mod._attach_close_countdown(snapshot, client)

    assert snapshot["close_countdown"]["available"] is False
    assert snapshot["close_countdown"]["reason"] == "countdown_check_failed"
    assert _still_usable(client) is True


def test_t_plus_one_failure_rolls_back() -> None:
    client = _AbortableClient(poison_on=["trade_calendar"])
    snapshot: dict[str, Any] = {"market": {"symbol": "000011"}}

    mod._attach_t_plus_one(snapshot, client)

    assert snapshot["t_plus_one"]["available"] is False
    assert snapshot["t_plus_one"]["reason"] == "calendar_check_failed"
    assert _still_usable(client) is True


def test_daily_tags_failure_rolls_back() -> None:
    client = _AbortableClient(poison_on=["derived_bar"])

    tagged, audit = mod._apply_daily_tags(client, "000011", 0, 1, ())

    assert tagged == ()
    assert audit["reason"] == "tag_fetch_failed"
    assert _still_usable(client) is True


def test_security_name_failure_rolls_back() -> None:
    client = _AbortableClient(poison_on=["security_master"])

    assert mod._resolve_security_name(client, "000011") is None
    assert _still_usable(client) is True


def test_fetch_validated_klines_failure_rolls_back() -> None:
    """R52：日线读取失败**必须** rollback。

    原来这一支只把异常包成 ``AShareLocalError`` 就抛，没有 rollback。而
    ``AShareLocalClient._get_conn()`` 复用同一条连接（lazy，只在第一次建），
    所以语句失败后连接停在 aborted 态 ⇒ **同一个客户端后续每一次 SQL 都废**
    ⇒ 一次「某只票的查询失败」被放大成整条 A 股链路全废。这与同文件
    ``check_t_plus_one_calendar`` 的回滚纪律是同一条。
    """
    from cpt.adapters.a_share_local import AShareLocalClient, AShareLocalError

    conn = _AbortableConn(poison_on=["from public.daily_bar"])
    client = AShareLocalClient(conn_factory=lambda: conn)

    with pytest.raises(AShareLocalError):
        client.fetch_validated_klines("600519", 0, 1)

    assert conn.rollback_count == 1, "异常分支漏了 rollback"
    assert conn.aborted is False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchall()


def test_fetch_validated_klines_rollback_failure_does_not_mask_the_error() -> None:
    """rollback 自己失败也不能把原始错误换成别的异常。"""
    from cpt.adapters.a_share_local import AShareLocalClient, AShareLocalError

    conn = _AbortableConn(poison_on=["from public.daily_bar"])
    conn.rollback_broken = True
    client = AShareLocalClient(conn_factory=lambda: conn)

    with pytest.raises(AShareLocalError, match="DB 读取失败"):
        client.fetch_validated_klines("600519", 0, 1)


# --------------------------------------------------------------------------- #
# 读历史失败：不能冒泡成 500
# --------------------------------------------------------------------------- #


def _patch_first_buy_scaffolding(monkeypatch: pytest.MonkeyPatch) -> None:
    """把纯计算部分桩掉，只留「加载历史」这一段真实逻辑。"""
    facts = types.SimpleNamespace(
        structure_id="bi:1:1000",
        center_ids=("c1",),
        has_two_centers=True,
        has_divergence_leg=False,
        has_reversal_bi=False,
        divergence_status="none",
    )
    monkeypatch.setattr(mod, "derive_first_buy_facts", lambda **kw: facts)
    monkeypatch.setattr(
        mod,
        "assess_first_buy",
        lambda **kw: types.SimpleNamespace(status="structure_ready"),
    )
    monkeypatch.setattr(mod, "record_signal_event", lambda *a, **kw: None)


def test_load_previous_signal_failure_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读上一状态失败**不能**让整个快照 500 —— 缺历史只等于按首次评估。"""
    _patch_first_buy_scaffolding(monkeypatch)
    client = _AbortableClient()
    bi = types.SimpleNamespace(level=5, direction=-1)

    def _boom(*args: Any, **kwargs: Any) -> Any:
        client.conn.aborted = True
        raise RuntimeError("simulated history read failure")

    monkeypatch.setattr(mod, "load_previous_signal", _boom)

    signal, prev_status = mod._derive_first_buy_signal([bi], [], (), client=client, code="000011")

    assert signal is not None
    assert signal.status == "structure_ready"
    assert _still_usable(client) is True


def test_signal_event_commit_failure_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """append 或 commit 失败都要救回连接，且不能影响信号本身产出。"""
    _patch_first_buy_scaffolding(monkeypatch)
    client = _AbortableClient()

    def _boom(*args: Any, **kwargs: Any) -> Any:
        client.conn.aborted = True
        raise RuntimeError("simulated append failure")

    monkeypatch.setattr(mod, "load_previous_signal", lambda *a, **kw: None)
    monkeypatch.setattr(mod, "record_signal_event", _boom)

    signal, _prev = mod._derive_first_buy_signal(
        [types.SimpleNamespace(level=5, direction=-1)], [], (), client=client, code="000011"
    )

    assert signal is not None
    assert client.conn.rollback_count >= 1
    assert _still_usable(client) is True
