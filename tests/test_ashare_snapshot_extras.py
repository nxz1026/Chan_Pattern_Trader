"""Tests for R21 Phase 3–6 gap fillers: close countdown, signal change, dual compare."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cpt.adapters.a_share_public import ASharePublicError
from cpt.application.a_share_snapshot import (
    _attach_close_countdown,
    _attach_dual_compare,
    _attach_signal_change,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, conn: Any):
        self._conn = conn

    def _get_conn(self):
        return self._conn


class _MockCursor:
    def __init__(self, fetchone_rows: list[tuple]):
        self.fetchone_rows = fetchone_rows
        self._idx = 0

    def fetchone(self):
        if self._idx < len(self.fetchone_rows):
            row = self.fetchone_rows[self._idx]
            self._idx += 1
            return row
        # 行不存在 → 返回 None
        return None

    def execute(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _conn_mock(fetchone_rows: list[tuple] | None = None, *, raise_exc: bool = False) -> Any:
    """Create a mock connection with cursor returning fetchone_rows."""
    conn = MagicMock()
    if raise_exc:
        conn.cursor.side_effect = RuntimeError("db down")
        return conn

    cursor = _MockCursor(fetchone_rows or [])
    conn.cursor.return_value = cursor
    return conn


# ---------------------------------------------------------------------------
# _attach_close_countdown
# ---------------------------------------------------------------------------


def test_close_countdown_trade_day() -> None:
    snapshot: dict[str, Any] = {"market": {}}
    conn = _conn_mock([(True,)])
    client = _FakeClient(conn)
    _attach_close_countdown(snapshot, client)
    cc = snapshot["close_countdown"]
    assert cc["available"] is True
    assert "seconds_to_close" in cc
    assert cc["close_time"] == "15:00:00"
    assert cc["seconds_to_close"] >= 0


def test_close_countdown_not_trade_day() -> None:
    snapshot: dict[str, Any] = {"market": {}}
    conn = _conn_mock([(False,)])
    client = _FakeClient(conn)
    _attach_close_countdown(snapshot, client)
    cc = snapshot["close_countdown"]
    assert cc["available"] is False
    assert cc["reason"] == "not_a_trade_day"


def test_close_countdown_calendar_unknown() -> None:
    """fetchone 返回 None（行不存在）→ calendar_unknown。"""
    snapshot: dict[str, Any] = {"market": {}}
    conn = _conn_mock([None])  # fetchone 返回 None = 行不存在
    client = _FakeClient(conn)
    _attach_close_countdown(snapshot, client)
    cc = snapshot["close_countdown"]
    assert cc["available"] is False
    assert cc["reason"] == "calendar_unknown"


def test_close_countdown_db_failure() -> None:
    snapshot: dict[str, Any] = {"market": {}}
    conn = _conn_mock(raise_exc=True)
    client = _FakeClient(conn)
    _attach_close_countdown(snapshot, client)
    cc = snapshot["close_countdown"]
    assert cc["available"] is False
    assert cc["reason"] == "countdown_check_failed"


# ---------------------------------------------------------------------------
# _attach_signal_change
# ---------------------------------------------------------------------------


def test_signal_changed_true() -> None:
    snapshot = {"signal": {"signal_id": "fb:1:abc", "status": "confirmed"}, "summary": {}}
    conn = _conn_mock([("structure_ready",)])
    client = _FakeClient(conn)
    _attach_signal_change(snapshot, client)
    assert snapshot["summary"]["signal_changed"] is True
    assert snapshot["summary"]["signal_change_type"] == "structure_ready→confirmed"


def test_signal_changed_false_same_status() -> None:
    snapshot = {"signal": {"signal_id": "fb:1:abc", "status": "confirmed"}, "summary": {}}
    conn = _conn_mock([("confirmed",)])
    client = _FakeClient(conn)
    _attach_signal_change(snapshot, client)
    assert snapshot["summary"]["signal_changed"] is False
    assert snapshot["summary"]["signal_change_type"] is None


def test_signal_changed_new() -> None:
    snapshot = {"signal": {"signal_id": "fb:1:abc", "status": "structure_ready"}, "summary": {}}
    conn = _conn_mock([(None,)])
    client = _FakeClient(conn)
    _attach_signal_change(snapshot, client)
    assert snapshot["summary"]["signal_changed"] is False
    assert snapshot["summary"]["signal_change_type"] == "new"


def test_signal_changed_no_signal() -> None:
    snapshot = {"signal": None, "summary": {}}
    conn = _conn_mock([])
    client = _FakeClient(conn)
    _attach_signal_change(snapshot, client)
    assert snapshot["summary"]["signal_changed"] is False
    assert snapshot["summary"]["signal_change_type"] is None


def test_signal_changed_db_failure() -> None:
    snapshot = {"signal": {"signal_id": "fb:1:abc", "status": "confirmed"}, "summary": {}}
    conn = _conn_mock(raise_exc=True)
    client = _FakeClient(conn)
    _attach_signal_change(snapshot, client)
    assert snapshot["summary"]["signal_changed"] is False


# ---------------------------------------------------------------------------
# _attach_dual_compare
# ---------------------------------------------------------------------------
#
# R35 重写：源从**东财 push2**（本机实测 502，生产上永远 unavailable）换成
# **新浪快照**（同机 200），并修掉了被 502 掩盖的**口径错配** —— 原实现拿
# **不复权**的现价去比 CPT 的**后复权**收盘价，600519 / 2026-09-30 实测会算出
# **−85.84%**（1258.62 vs 8886.536）。现在先把现价乘同一根 bar 的因子再比。
# ---------------------------------------------------------------------------


def _quote(
    last: float = 100.0,
    prev_close: float = 99.0,
    high: float = 105.0,
    low: float = 95.0,
    open_: float = 98.0,
) -> dict:
    """新浪快照 ``fetch_quote`` 的返回形状（**不复权**）。"""
    return {
        "code": "sh600519",
        "name": "贵州茅台",
        "open": open_,
        "prev_close": prev_close,
        "last": last,
        "high": high,
        "low": low,
        "volume": 3833098.0,
        "date": "2026-10-02",
        "time": "16:14:58",
    }


def _factor_conn(factor: float | None) -> Any:
    """一个只会回答「某日 hfq_factor 是多少」的假连接（``hfq_factor_on`` 唯一的依赖）。"""

    class _Cur:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_: object) -> bool:
            return False

        def execute(self, sql: str, params: Any = None) -> None:
            return None

        def fetchone(self) -> Any:
            return None if factor is None else (factor,)

    class _Conn:
        def cursor(self) -> Any:
            return _Cur()

    return _Conn()


class _FakeClient:
    """``hfq_factor_on`` 只经 ``client._get_conn()`` 拿连接。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def _get_conn(self) -> Any:
        return self._conn


def test_dual_compare_success() -> None:
    """因子 = 1.0 时退化成「现价 vs 收盘」，与旧行为一致。"""
    snapshot = {"candles": [{"close": 99.5, "open_time": 1, "close_time": 2}], "market": {}}
    with patch(
        "cpt.adapters.a_share_public.SinaQuoteClient.fetch_quote",
        lambda self, code, **kw: _quote(),
    ):
        _attach_dual_compare(snapshot, "600519", _FakeClient(_factor_conn(1.0)))
    dc = snapshot["dual_compare"]
    assert dc["available"] is True
    assert dc["cpt_close"] == 99.5
    assert dc["realtime_price"] == pytest.approx(100.0)
    assert dc["divergence_pct"] == pytest.approx(0.5025, abs=0.01)
    assert dc["realtime"]["change_pct"] == pytest.approx(1.01, abs=0.02)
    assert dc["realtime"]["source"] == "sina"


def test_dual_compare_adjusts_realtime_to_hfq_basis() -> None:
    """**回归钉子**：R35 那个 −85.84% 的口径错配不许回来。

    真实数字（2026-10-02 实测）：库里不复权 1258.62、因子 7.06053932、
    快照收盘 8886.536。同一天上游现价就是 1258.62 ⇒ 同口径下偏离应为 0。
    """
    factor = 7.06053932
    last_hfq_close = 1258.62 * factor
    snapshot = {
        "candles": [{"close": last_hfq_close, "open_time": 1790726400000, "close_time": 0}],
        "market": {},
    }
    with patch(
        "cpt.adapters.a_share_public.SinaQuoteClient.fetch_quote",
        lambda self, code, **kw: _quote(last=1258.62, prev_close=1258.62),
    ):
        _attach_dual_compare(snapshot, "600519", _FakeClient(_factor_conn(factor)))
    dc = snapshot["dual_compare"]
    assert dc["available"] is True
    assert dc["realtime"]["raw_price"] == 1258.62
    assert dc["realtime_price"] == pytest.approx(8886.536, rel=1e-4)
    assert dc["divergence_pct"] == pytest.approx(0.0, abs=0.01)
    # 拿不复权价直接比就是这个数 —— 明确记下来，免得有人"简化"回去
    assert (1258.62 - last_hfq_close) / last_hfq_close * 100 == pytest.approx(-85.84, abs=0.05)


def test_dual_compare_missing_factor_refuses_instead_of_faking() -> None:
    """查不到因子 → 说不知道，**不给** −85% 那种数字。"""
    snapshot = {"candles": [{"close": 8886.536, "open_time": 1790726400000}], "market": {}}
    with patch(
        "cpt.adapters.a_share_public.SinaQuoteClient.fetch_quote",
        lambda self, code, **kw: _quote(last=1258.62),
    ):
        _attach_dual_compare(snapshot, "600519", _FakeClient(_factor_conn(None)))
    dc = snapshot["dual_compare"]
    assert dc["available"] is False
    assert dc["reason"] == "factor_unavailable"
    assert dc["realtime_raw_price"] == 1258.62


def test_dual_compare_network_failure() -> None:
    snapshot = {"candles": [{"close": 99.5, "open_time": 1}], "market": {}}

    def boom(self, code, **kw):
        raise ASharePublicError("新浪快照不可达：sh600519（timeout）")

    with patch("cpt.adapters.a_share_public.SinaQuoteClient.fetch_quote", boom):
        _attach_dual_compare(snapshot, "600519", _FakeClient(_factor_conn(1.0)))
    dc = snapshot["dual_compare"]
    assert dc["available"] is False
    assert dc["reason"] == "realtime_unavailable"


def test_dual_compare_empty_last_price() -> None:
    snapshot = {"candles": [{"close": 99.5, "open_time": 1}], "market": {}}
    with patch(
        "cpt.adapters.a_share_public.SinaQuoteClient.fetch_quote",
        lambda self, code, **kw: _quote(last=None),
    ):
        _attach_dual_compare(snapshot, "600519", _FakeClient(_factor_conn(1.0)))
    dc = snapshot["dual_compare"]
    assert dc["available"] is False
    assert dc["reason"] == "realtime_empty"


def test_dual_compare_no_candles() -> None:
    """没有 candles → 没有"最后一根 bar 的因子"，只能如实说不知道。"""
    snapshot = {"candles": [], "market": {}}
    with patch(
        "cpt.adapters.a_share_public.SinaQuoteClient.fetch_quote",
        lambda self, code, **kw: _quote(),
    ):
        _attach_dual_compare(snapshot, "600519", _FakeClient(_factor_conn(1.0)))
    dc = snapshot["dual_compare"]
    assert dc["available"] is False
    assert dc["reason"] == "factor_unavailable"
