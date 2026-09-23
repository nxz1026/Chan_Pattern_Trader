from __future__ import annotations

import json

import pytest
from cpt.adapters.binance_futures import BinanceDataError, BinanceFuturesClient

PAYLOAD = [
    [
        0,
        "10.0",
        "12.0",
        "8.0",
        "11.0",
        "3.0",
        299999,
        "33.0",
        4,
        "1.5",
        "16.5",
        "0",
    ]
]


def opener(_url: str, _timeout: float):
    return json.dumps(PAYLOAD).encode()


def test_fetch_klines_maps_binance_array_to_canonical_bar() -> None:
    client = BinanceFuturesClient(opener=opener, now_ms=lambda: 400000)
    result = client.fetch_klines("BTCUSDT", "5m")
    assert len(result) == 1
    assert (result[0].open_time, result[0].high, result[0].close) == (0, 12.0, 11.0)
    assert result[0].is_closed is True


def test_query_parameters_and_limit_are_validated() -> None:
    seen: list[str] = []

    def recording(url: str, timeout: float):
        seen.append(url)
        return json.dumps(PAYLOAD).encode()

    BinanceFuturesClient(opener=recording).fetch_klines(
        "BTCUSDT", "5m", start_time=1, end_time=2, limit=10
    )
    assert "symbol=BTCUSDT" in seen[0]
    assert "interval=5m" in seen[0]
    assert "startTime=1" in seen[0]
    assert "endTime=2" in seen[0]
    assert "limit=10" in seen[0]
    with pytest.raises(ValueError, match="limit"):
        BinanceFuturesClient(opener=opener).fetch_klines("BTCUSDT", "5m", limit=0)


def test_bad_payload_raises_binance_data_error() -> None:
    def bad(_url: str, _timeout: float):
        return b'{"code": -1121, "msg": "bad symbol"}'

    with pytest.raises(BinanceDataError):
        BinanceFuturesClient(opener=bad).fetch_klines("BAD", "5m")
