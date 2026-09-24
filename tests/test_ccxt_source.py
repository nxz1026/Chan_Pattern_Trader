"""ccxt 加密通道测试（R17）。**不联网**：注入 ``factory`` 假交易所。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from cpt.adapters.ccxt_source import (
    EXCHANGE_IDS,
    CcxtKlineClient,
    CcxtNotInstalledError,
    CcxtSourceError,
    parse_ohlcv_rows,
)

HOUR_MS = 3_600_000
BASE_MS = 1_790_000_000_000


class FakeExchange:
    """最小 ccxt 交易所替身。"""

    def __init__(
        self, rows: Sequence[Sequence[Any]] | None = None, *, error: Exception | None = None
    ) -> None:
        self.rows = list(rows or [])
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def fetch_ohlcv(self, symbol: str, *, timeframe: str, limit: int) -> list[list[Any]]:
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "limit": limit})
        if self.error is not None:
            raise self.error
        return [list(row) for row in self.rows]

    def fetch_time(self) -> int:
        if self.error is not None:
            raise self.error
        return BASE_MS


def _rows(count: int = 3) -> list[list[Any]]:
    return [
        [BASE_MS + index * HOUR_MS, 100.0 + index, 101.0 + index, 99.0 + index, 100.5 + index, 10.0]
        for index in range(count)
    ]


def test_parse_ohlcv_rows_maps_fields_and_is_closed() -> None:
    bars = parse_ohlcv_rows(_rows(3), interval_ms=HOUR_MS, now_ms=BASE_MS + 2 * HOUR_MS + 1)
    assert len(bars) == 3
    assert bars[0].open_time == BASE_MS
    assert bars[0].open == pytest.approx(100.0)
    assert bars[0].high == pytest.approx(101.0)
    assert bars[0].low == pytest.approx(99.0)
    assert bars[0].close == pytest.approx(100.5)
    assert bars[0].close_time == BASE_MS + HOUR_MS - 1
    # 前两根已收盘，最后一根未收盘
    assert [bar.is_closed for bar in bars] == [True, True, False]


def test_parse_ohlcv_rows_rejects_short_rows() -> None:
    with pytest.raises(CcxtSourceError, match="字段不足"):
        parse_ohlcv_rows([[BASE_MS, 1, 2, 3]], interval_ms=HOUR_MS, now_ms=BASE_MS)


def test_parse_ohlcv_rows_rejects_non_numeric() -> None:
    bad = [[BASE_MS, "x", 1, 1, 1, 1]]
    with pytest.raises(CcxtSourceError, match="不是数字"):
        parse_ohlcv_rows(bad, interval_ms=HOUR_MS, now_ms=BASE_MS)


@pytest.mark.parametrize("alias", ["binanceusdm", "币安期货"])
def test_exchange_aliases(alias: str) -> None:
    client = CcxtKlineClient(exchange=alias, factory=lambda *_: FakeExchange(_rows()))
    assert client.exchange_id == EXCHANGE_IDS["binanceusdm"]


def test_unknown_exchange_raises() -> None:
    with pytest.raises(CcxtSourceError, match="不支持的交易所"):
        CcxtKlineClient(exchange="ftx")


def test_fetch_klines_passes_symbol_timeframe_limit() -> None:
    fake = FakeExchange(_rows(2))
    client = CcxtKlineClient(factory=lambda *_: fake, now_ms=lambda: BASE_MS + 10 * HOUR_MS)
    bars = client.fetch_klines("BTC/USDT:USDT", "1h", limit=2, interval_ms=HOUR_MS)
    assert len(bars) == 2
    assert fake.calls == [{"symbol": "BTC/USDT:USDT", "timeframe": "1h", "limit": 2}]
    assert all(bar.is_closed for bar in bars)


def test_fetch_klines_validates_arguments() -> None:
    client = CcxtKlineClient(factory=lambda *_: FakeExchange(_rows()))
    with pytest.raises(ValueError, match="symbol"):
        client.fetch_klines("", "1h")
    with pytest.raises(ValueError, match="周期"):
        client.fetch_klines("BTC/USDT:USDT", "7h")
    with pytest.raises(ValueError, match="limit"):
        client.fetch_klines("BTC/USDT:USDT", "1h", limit=0)


def test_fetch_klines_wraps_exchange_errors() -> None:
    fake = FakeExchange(error=RuntimeError("boom"))
    client = CcxtKlineClient(factory=lambda *_: fake)
    with pytest.raises(CcxtSourceError, match="fetch_ohlcv 失败"):
        client.fetch_klines("BTC/USDT:USDT", "1h")


def test_fetch_klines_empty_response_raises() -> None:
    client = CcxtKlineClient(factory=lambda *_: FakeExchange([]))
    with pytest.raises(CcxtSourceError, match="返回空 K 线"):
        client.fetch_klines("BTC/USDT:USDT", "1h")


def test_probe_returns_server_time() -> None:
    client = CcxtKlineClient(factory=lambda *_: FakeExchange())
    assert client.probe() == {"exchange": "binanceusdm", "server_time_ms": BASE_MS}


def test_missing_ccxt_raises_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """未装 ccxt 时给的是**可执行的安装提示**，不是裸 ModuleNotFoundError。"""

    def _boom() -> Any:
        raise CcxtNotInstalledError(
            '加密 ccxt 通道需要可选依赖 ccxt，请安装：pip install -e ".[crypto]"'
        )

    monkeypatch.setattr("cpt.adapters.ccxt_source._import_ccxt", _boom)
    client = CcxtKlineClient()
    with pytest.raises(CcxtNotInstalledError, match=r"\[crypto\]"):
        client.fetch_klines("BTC/USDT:USDT", "1h")
    with pytest.raises(CcxtNotInstalledError, match=r"\[crypto\]"):
        client.probe()


def test_real_ccxt_factory_is_used_when_installed() -> None:
    """装了 ccxt 时真的构造交易所对象（不联网，只构造）。"""
    pytest.importorskip("ccxt")
    client = CcxtKlineClient(exchange="binanceusdm")
    exchange = client._exchange()  # noqa: SLF001 — 断言的是构造而非请求
    assert exchange.id == "binanceusdm"
    assert isinstance(exchange.proxies, Mapping) or exchange.proxies is None
