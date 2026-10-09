"""R59（审计 M28）：``data_quality.gaps`` 只能有一种 schema。

``dashboard_quality.quality_report`` 产出的缺口是
``{"from", "to", "delta"}``（相邻两根 bar 的 ``open_time`` 与毫秒差），
而 ``_attach_calendar_gaps`` 原来覆写为 ``{"date": ...}`` —— 同一个键两种形状，
下游按 ``from``/``to`` 读只会拿到 ``None``，且 ``from/to/delta`` 在日历路径上
永久丢失。这里钉住统一后的形状。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from cpt.adapters import a_share_local
from cpt.application import a_share_snapshot as snap


def _ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


class _FakeConn:
    pass


class _FakeClient:
    def _get_conn(self) -> _FakeConn:
        return _FakeConn()


def test_calendar_gap_shape_matches_quality_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """改前：``gaps == [{"date": "2026-01-06"}]``（无 from/to/delta）。"""
    monkeypatch.setattr(
        a_share_local,
        "open_days_between",
        lambda conn, start, end: [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)],
    )
    snapshot: dict[str, Any] = {
        "data_quality": {"gaps": [], "gap_count": 0},
        "candles": [
            {"open_time": _ms(2026, 1, 5), "close": 10.0},
            {"open_time": _ms(2026, 1, 7), "close": 11.0},
        ],
    }

    snap._attach_calendar_gaps(snapshot, _FakeClient())

    quality = snapshot["data_quality"]
    assert quality["gap_count"] == 1
    assert quality["gap_basis"] == "trade_calendar"
    gap = quality["gaps"][0]
    assert set(gap) == {"date", "from", "to", "delta"}
    assert gap["date"] == "2026-01-06"
    assert gap["from"] == _ms(2026, 1, 5)
    assert gap["to"] == _ms(2026, 1, 7)
    assert gap["delta"] == _ms(2026, 1, 7) - _ms(2026, 1, 5)


def test_no_gap_keeps_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        a_share_local,
        "open_days_between",
        lambda conn, start, end: [date(2026, 1, 5), date(2026, 1, 6)],
    )
    snapshot: dict[str, Any] = {
        "data_quality": {"gaps": [], "gap_count": 0},
        "candles": [
            {"open_time": _ms(2026, 1, 5), "close": 10.0},
            {"open_time": _ms(2026, 1, 6), "close": 10.5},
        ],
    }

    snap._attach_calendar_gaps(snapshot, _FakeClient())

    assert snapshot["data_quality"]["gaps"] == []
    assert snapshot["data_quality"]["gap_count"] == 0
