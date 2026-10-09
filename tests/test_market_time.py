"""``cpt.domain.market_time`` 测试：A 股时区口径的唯一来源。

R59（审计 H10 / L11）：这些是钉住「市场时间不随宿主机时区漂移」的用例。
"""

from __future__ import annotations

import datetime as dt

import pytest
from cpt.domain import market_time
from cpt.domain.market_time import (
    MARKET_CLOSE,
    MARKET_TZ,
    market_now,
    market_today,
    seconds_to_close,
)


def test_market_anchor_constants() -> None:
    assert MARKET_TZ.key == "Asia/Shanghai"
    assert MARKET_CLOSE == dt.time(15, 0)


def test_market_now_is_aware_and_pinned_to_market_tz() -> None:
    now = market_now()
    assert now.tzinfo is not None
    assert now.tzinfo is MARKET_TZ
    assert now.utcoffset() == dt.timedelta(hours=8)


def test_market_today_follows_market_now() -> None:
    assert market_today() == market_now().date()


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # 北京时间当天的固定时刻
        (dt.datetime(2026, 10, 8, 9, 35, 30, tzinfo=MARKET_TZ), 5 * 3600 + 24 * 60 + 30),
        (dt.datetime(2026, 10, 8, 14, 30, tzinfo=MARKET_TZ), 1800),
        (dt.datetime(2026, 10, 8, 15, 0, tzinfo=MARKET_TZ), 0),
        (dt.datetime(2026, 10, 8, 16, 0, tzinfo=MARKET_TZ), 0),
        (dt.datetime(2026, 10, 8, 23, 59, tzinfo=MARKET_TZ), 0),
        # 同一瞬间换个时区表示，结果必须一样（14:30 BJT == 06:30 UTC）
        (dt.datetime(2026, 10, 8, 6, 30, tzinfo=dt.UTC), 1800),
    ],
)
def test_seconds_to_close(moment: dt.datetime, expected: int) -> None:
    assert seconds_to_close(moment) == expected


def test_seconds_to_close_defaults_to_market_now(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed = dt.datetime(2026, 10, 8, 14, 59, 0, tzinfo=MARKET_TZ)
    monkeypatch.setattr(market_time, "market_now", lambda: fixed)
    assert seconds_to_close() == 60
