"""Tests for R21 Phase 3–6 gap fillers: close countdown, signal change, dual compare."""

from __future__ import annotations

import json as _json
import urllib.request as _url
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
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


def _eastmoney_response(
    price: float = 100.0,
    high: float = 105.0,
    low: float = 95.0,
    open_: float = 98.0,
    volume: float = 1000000,
    turnover: float = 100000000,
    prev_close: float = 99.0,
    change_pct: float = 1.0,
) -> bytes:
    data = {
        "data": {
            "f43": int(price * 100),
            "f44": int(high * 100),
            "f45": int(low * 100),
            "f46": int(open_ * 100),
            "f47": str(int(volume)),
            "f48": str(int(turnover)),
            "f60": int(prev_close * 100),
            "f170": int(change_pct * 100),
            "f57": "600519",
            "f58": "贵州茅台",
        }
    }
    return _json.dumps(data).encode()


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_dual_compare_success() -> None:
    snapshot = {"candles": [{"close": 99.5, "open_time": 1, "close_time": 2}], "market": {}}
    payload = _eastmoney_response(price=100.0, prev_close=99.0, change_pct=1.0)
    with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
        _attach_dual_compare(snapshot, "600519", None)
    dc = snapshot["dual_compare"]
    assert dc["available"] is True
    assert dc["cpt_close"] == 99.5
    assert dc["realtime_price"] == 100.0
    assert dc["divergence_pct"] == pytest.approx(0.5025, abs=0.01)
    assert dc["realtime"]["change_pct"] == pytest.approx(1.0, abs=0.01)


def test_dual_compare_shenzhen_prefix() -> None:
    snapshot = {"candles": [{"close": 10.0}], "market": {}}
    payload = _eastmoney_response(price=10.1, prev_close=10.0)
    with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)) as mocked:
        _attach_dual_compare(snapshot, "000001", None)
    # 验证 secid 前缀为 0（深市）
    call_args = mocked.call_args
    req_arg = call_args[0][0]
    assert "secid=0.000001" in req_arg.full_url


def test_dual_compare_network_failure() -> None:
    snapshot = {"candles": [{"close": 99.5}], "market": {}}
    with patch("urllib.request.urlopen", side_effect=_url.URLError("timeout")):
        _attach_dual_compare(snapshot, "600519", None)
    dc = snapshot["dual_compare"]
    assert dc["available"] is False
    assert dc["reason"] == "realtime_unavailable"


def test_dual_compare_empty_response() -> None:
    snapshot = {"candles": [{"close": 99.5}], "market": {}}
    payload = _json.dumps({"data": None}).encode()
    with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
        _attach_dual_compare(snapshot, "600519", None)
    dc = snapshot["dual_compare"]
    assert dc["available"] is False


def test_dual_compare_no_candles() -> None:
    snapshot = {"candles": [], "market": {}}
    payload = _eastmoney_response(price=100.0, prev_close=99.0)
    with patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
        _attach_dual_compare(snapshot, "600519", None)
    dc = snapshot["dual_compare"]
    assert dc["available"] is True
    assert dc["cpt_close"] == 0.0
    assert dc["divergence_pct"] is None
