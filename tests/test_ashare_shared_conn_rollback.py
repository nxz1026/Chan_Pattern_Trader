"""A 股快照：**共享连接**上失败必须回滚（R67 / 审计 P2-2）。

## 这条纪律的由来

`AShareLocalClient` 全程复用**同一条** psycopg 连接（`_get_conn` 只在首次建连）。
psycopg 的事务语义是：一条语句在事务内报错 ⇒ 整条事务立刻进 aborted 态，此后
**任何**语句都抛 `InFailedSqlTransaction`，直到 rollback 解除。所以「失败后不回滚」
不是丢一条记录，而是**连锁毒掉后面所有查询**（陷阱 #31 的实测症状：
「一只查失败，后面几十只全部降级/skip」）。

本文件 `_rollback_quietly` 早就在 6 处调了（`:230/:248/:266/:631/:678/:784`），
但有 **3 处漏了**（审计 P2-2）—— 它们全在「只记 debug / warning 就返回降级值」的
分支里，因为它们看起来**无害**（都有一句「不该让面板消失」的注释）：

1. `_resolve_price_basis` —— 取后复权因子失败；
2. `_stale_reason` —— 判「滞后原因」时查因子失败；
3. `build_ashare_snapshot` 的 R38 水位记录（`MetricRecorder(conn).record(...)`）。

后果各不相同但同源：①后面 `_attach_staleness` 接着失败 ⇒ `stale_reason` 为 None
（面板不显示原因）；②`gap_count` 保持 crypto 口径（≈25）⇒ 假 `health=failing`；
③水位行随 `commit()` 一起消失。

## 为什么只在 except 里回滚

无条件回滚会销毁**调用方**尚未提交的事务（陷阱 #31）。「失败时无条件」是唯一
正确的时机：那时事务已经 aborted，回滚销毁不了任何还有价值的东西。

全程离线：假连接 + 假客户端，不连 DB。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpt.adapters import a_share_local  # noqa: E402
from cpt.application import a_share_snapshot as snap  # noqa: E402

SNAPSHOT_PY = ROOT / "cpt" / "application" / "a_share_snapshot.py"


class _RecConn:
    """够用的假连接：只关心 `rollback` 有没有被调用。"""

    def __init__(self, *, failing: bool = True) -> None:
        self.failing = failing
        self.rollbacks = 0
        self.aborted = False

    def cursor(self) -> Any:  # noqa: ANN401
        return self

    def __enter__(self) -> _RecConn:
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def execute(self, *a: Any, **k: Any) -> None:
        self.aborted = True
        raise a_share_local.AShareLocalError("simulated DB failure")

    @property
    def rowcount(self) -> int:
        return 0

    def fetchone(self) -> Any:  # noqa: ANN401
        return None

    def fetchall(self) -> tuple[()]:
        return ()

    def rollback(self) -> None:
        self.rollbacks += 1
        self.aborted = False


class _RecClient:
    """只暴露 `_get_conn`（`_rollback_quietly` 判据就是它）。"""

    def __init__(self, conn: _RecConn) -> None:
        self.conn = conn

    def _get_conn(self) -> _RecConn:
        return self.conn


def _boom(*a: Any, **k: Any) -> Any:  # noqa: ANN401
    raise a_share_local.AShareLocalError("simulated factor query failure")


# ── 站 1：_resolve_price_basis ───────────────────────────────────────────────


def test_price_basis_rolls_back_after_factor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★回归：取因子失败 ⇒ 回滚共享连接（否则后面 `_attach_staleness` 全废）。"""
    conn = _RecConn()
    client = _RecClient(conn)
    monkeypatch.setattr(a_share_local, "hfq_factor_on", _boom)
    snapshot = {"candles": [{"close": 1500.0, "open_time": 1_760_000_000_000}]}

    basis = snap._resolve_price_basis(snapshot, "600519", client)

    assert basis.factor is None, "查不到因子就该是 None（不填 1.0）"
    assert conn.rollbacks == 1, "取因子失败后没有回滚共享连接"
    assert conn.aborted is False


# ── 站 2：_stale_reason ─────────────────────────────────────────────────────


def test_stale_reason_rolls_back_after_factor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★回归：判因失败 ⇒ 回滚 + 返回 None（不编原因）。"""
    conn = _RecConn()
    monkeypatch.setattr(a_share_local, "raw_bar_exists_on", lambda _c, _code, _day: True)
    monkeypatch.setattr(a_share_local, "hfq_factor_on", _boom)

    assert snap._stale_reason(conn, "600519", {date(2026, 10, 16)}) is None
    assert conn.rollbacks == 1, "判因失败后没有回滚共享连接"
    assert conn.aborted is False


def test_stale_reason_keeps_the_three_way_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回滚是**加**在异常分支上的，三态分类不许被改动（这是 R62 的判据来源）。

    R67（P2-1）：第一态的探针从 ``fetch_raw_bar_on``（自建连、吞异常、拿不到返回
    ``None``）换成 ``raw_bar_exists_on``（走共享连接、判不了就抛、返回 ``bool``）。
    """
    conn = _RecConn()

    monkeypatch.setattr(a_share_local, "raw_bar_exists_on", lambda _c, _code, _day: False)
    assert snap._stale_reason(conn, "600519", {date(2026, 10, 16)}) == (
        "upstream_lag",
        "2026-10-16",
    )

    monkeypatch.setattr(a_share_local, "raw_bar_exists_on", lambda _c, _code, _day: True)
    monkeypatch.setattr(a_share_local, "hfq_factor_on", lambda _conn, _code, _day: None)
    assert snap._stale_reason(conn, "600519", {date(2026, 10, 16)}) == (
        "factor_missing",
        "2026-10-16",
    )

    monkeypatch.setattr(a_share_local, "hfq_factor_on", lambda _conn, _code, _day: 6.39)
    assert snap._stale_reason(conn, "600519", {date(2026, 10, 16)}) == (
        "unusable",
        "2026-10-16",
    )
    assert conn.rollbacks == 0, "正常分类路径不该回滚（那是调用方的事务）"


# ── 站 3 + 全局契约 ─────────────────────────────────────────────────────────


def test_metric_recorder_failure_rolls_back() -> None:
    """站 3 在 `build_ashare_snapshot` 深处，行为测试成本过高 ⇒ 钉住源码契约。"""
    src = SNAPSHOT_PY.read_text(encoding="utf-8")
    assert '_rollback_quietly(conn, f"metric:{code}")' in src
    # 它必须在「水位记录失败」的 except 分支里，而不是别处
    idx = src.index('_rollback_quietly(conn, f"metric:{code}")')
    tail = src[idx:]
    assert "运行水位记录失败" in src[max(0, idx - 700) : idx], "回滚不在水位失败的 except 里"
    assert "return snapshot" in tail[:200]


def test_all_three_new_sites_are_inside_except_blocks() -> None:
    """三处都必须是「失败时」回滚 —— 无条件回滚会销毁调用方未提交的事务（陷阱 #31）。"""
    src = SNAPSHOT_PY.read_text(encoding="utf-8")
    sites = (
        '_rollback_quietly(client, f"price_basis:{code}")',
        '_rollback_quietly(conn, f"stale_reason:{code}")',
        '_rollback_quietly(conn, f"metric:{code}")',
    )
    for site in sites:
        assert src.count(site) == 1, site
        idx = src.index(site)
        assert "except Exception" in src[max(0, idx - 1500) : idx], f"{site} 不在 except 分支里"


def test_rollback_helper_accepts_a_raw_connection() -> None:
    """`_rollback_quietly` 现在同时吃「客户端」与「裸连接」，判据是 `rollback` 可调用。"""
    conn = _RecConn()
    snap._rollback_quietly(conn, "unit")
    assert conn.rollbacks == 1

    client = _RecClient(_RecConn())
    snap._rollback_quietly(client, "unit")
    assert client.conn.rollbacks == 1

    # 既不是连接也不是客户端的对象：静默无操作，绝不抛
    snap._rollback_quietly(None, "unit")
    snap._rollback_quietly(object(), "unit")
