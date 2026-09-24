"""``cpt.adapters.a_share_local`` 测试。

用 mock DB 测全部分支，不依赖 psycopg — 保持 ``dependencies = []`` 干净。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from cpt.adapters.a_share_local import (
    AShareFetchResult,
    AShareLocalClient,
    AShareLocalError,
)
from cpt.domain.models import CanonicalBar

# --------------------------------------------------------------------------- #
# mock DB
# --------------------------------------------------------------------------- #


@dataclass
class FakeCursor:
    rows_bars: list[tuple]
    rows_factors: list[tuple]
    filter: dict = field(default_factory=dict)
    executed: list = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append((sql, params))
        # 简化路由：根据 SQL 关键字
        s = sql.strip().lower()
        if s.startswith("select date, open"):
            code, start, end = params
            self.filter["bars"] = [
                r[1:] for r in self.rows_bars if r[0] == code and start <= r[1] <= end
            ]
        elif s.startswith("select trade_date, hfq_factor"):
            code, start, end = params
            self.filter["factors"] = [r for r in self.rows_factors if start <= r[0] <= end]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        s = self.executed[-1][0].strip().lower()
        if s.startswith("select date, open"):
            return self.filter["bars"]
        if s.startswith("select trade_date, hfq_factor"):
            return self.filter["factors"]
        raise AssertionError(f"Unexpected fetchall on: {s}")


@dataclass
class FakeConn:
    bars: list[tuple]
    factors: list[tuple]

    def cursor(self):
        return FakeCursor(rows_bars=self.bars, rows_factors=self.factors)

    def close(self):
        pass


def date(y, m, d):
    from datetime import date as _d

    return _d(y, m, d)


def ms(d) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def full_db():
    """000002 三天完整 OHLC + 因子。"""
    bars = [
        ("000002", date(2026, 9, 22), 3.0, 3.1, 2.9, 3.05, 100.0, 200.0),
        ("000002", date(2026, 9, 23), 3.1, 3.2, 3.0, 3.15, 150.0, 300.0),
        ("000002", date(2026, 9, 24), 3.2, 3.3, 3.1, 3.25, 200.0, 400.0),
    ]
    factors = [
        (date(2026, 9, 22), 10.0),
        (date(2026, 9, 23), 10.5),
        (date(2026, 9, 24), 11.0),
    ]
    conn = FakeConn(bars=list(bars), factors=list(factors))
    client = AShareLocalClient(conn_factory=lambda: conn)
    return client, conn


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


def test_returns_hfq_applied_canonical_bars(full_db):
    client, _ = full_db
    result = client.fetch_validated_klines("000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))
    assert isinstance(result, AShareFetchResult)
    assert len(result.bars) == 3
    assert result.skipped_no_factor == ()

    bar = result.bars[0]
    # 原始 close=3.05 × factor=10.0 = 30.5
    assert isinstance(bar, CanonicalBar)
    assert bar.close == pytest.approx(30.5)
    assert bar.open == pytest.approx(30.0)
    assert bar.high == pytest.approx(31.0)
    assert bar.low == pytest.approx(29.0)
    # 成交量保持不复权
    assert bar.volume == pytest.approx(100.0)
    assert bar.quote_volume == pytest.approx(200.0)
    # 时间字段：open_time=当日 00:00:00 UTC；close_time=当日 23:59:59.999 UTC
    assert bar.open_time == ms(date(2026, 9, 22))
    assert bar.close_time == bar.open_time + 24 * 3600 * 1000 - 1
    assert bar.is_closed is True


def test_factor_missing_day_is_skipped_not_failed(full_db):
    client, _ = full_db
    # 删掉 9-23 的因子，模拟缺口
    client._get_conn().factors[:] = [
        f for f in client._get_conn().factors if f[0] != date(2026, 9, 23)
    ]

    result = client.fetch_validated_klines("000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))
    assert len(result.bars) == 2
    assert result.skipped_no_factor == ("2026-09-23",)
    # 留下的两根仍是 9-22 和 9-24
    assert [b.open_time for b in result.bars] == [ms(date(2026, 9, 22)), ms(date(2026, 9, 24))]


def test_all_dates_missing_factor_raises_error(full_db):
    client, _ = full_db
    client._get_conn().factors.clear()
    with pytest.raises(AShareLocalError, match="所有日期都缺因子"):
        client.fetch_validated_klines("000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))


def test_no_bars_in_range_raises_error(full_db):
    client, _ = full_db
    with pytest.raises(AShareLocalError, match="public.daily_bar 无数据"):
        client.fetch_validated_klines("000002", ms(date(2024, 1, 1)), ms(date(2024, 1, 5)))


def test_code_with_exchange_suffix_is_normalized(full_db):
    client, _ = full_db
    result = client.fetch_validated_klines(
        "000002.SZ", ms(date(2026, 9, 22)), ms(date(2026, 9, 22))
    )
    assert len(result.bars) == 1
    assert result.bars[0].close == pytest.approx(30.5)


def test_db_error_is_wrapped_in_ashare_error():
    class BrokenConn:
        def cursor(self):
            raise RuntimeError("connection lost")

    client = AShareLocalClient(conn_factory=lambda: BrokenConn())
    with pytest.raises(AShareLocalError, match="DB 读取失败"):
        client.fetch_validated_klines("000002", 0, 1)


def test_close_is_idempotent_and_safe(full_db):
    client, _ = full_db
    client.close()
    client.close()  # 第二次不能抛错
    assert client._conn is None


def test_fetch_validated_bars_returns_barlike_list(full_db):
    client, _ = full_db
    bars = client.fetch_validated_bars("000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))
    assert len(bars) == 3
    # 验证确实是 BarLike（duck typing：has open_time/open/high/low/close）
    for b in bars:
        for attr in ("open_time", "open", "high", "low", "close"):
            assert hasattr(b, attr)
