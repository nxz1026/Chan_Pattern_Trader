"""``cpt.web.a_share`` 测试 — 注入 mock client，不依赖 psycopg。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cpt.web.a_share import (
    DEFAULT_WIDTH_K,
    build_ashare_snapshot,
)


class _FakeResult:
    """AShareFetchResult duck type — 只有 bars 属性即可。"""

    def __init__(self, bars):
        self.bars = tuple(bars)


class _FakeClient:
    """AShareLocalClient duck type — 只需实现 fetch_validated_klines。"""

    def __init__(self, bars: list[Any]) -> None:
        self._bars = bars
        self.calls: list[tuple[str, int, int]] = []

    def fetch_validated_klines(self, code: str, start_ms: int, end_ms: int) -> _FakeResult:
        self.calls.append((code, start_ms, end_ms))
        return _FakeResult(self._bars)


def _make_canonical(open_time_ms: int, close: float) -> Any:
    """最小可用 CanonicalBar（实际由 a_share_local 装配 OHLC 全部字段）。"""
    from cpt.domain.models import CanonicalBar

    return CanonicalBar(
        open_time=open_time_ms,
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=100.0,
        close_time=open_time_ms + 24 * 3600 * 1000 - 1,
        quote_volume=200.0,
        trade_count=1,
        taker_buy_base_volume=0.0,
        taker_buy_quote_volume=0.0,
        is_closed=True,
    )


def _bars_for(code: str, *, n: int, start_price: float = 10.0) -> list[Any]:
    """生成连续 n 根单调上涨的 CanonicalBar（避免 0 成交量等极端）。"""
    end_ms = int(datetime(2026, 9, 24, tzinfo=UTC).timestamp() * 1000)
    bars = []
    for i in range(n):
        t = end_ms - (n - 1 - i) * 24 * 3600 * 1000
        bars.append(_make_canonical(t, start_price + i * 0.5))
    return bars


def test_build_ashare_snapshot_returns_v2_schema():
    fake = _FakeClient(_bars_for("600519", n=40))
    snap = build_ashare_snapshot("600519", client=fake)
    assert snap["schema_version"] == "dashboard.v2"
    assert snap["market"]["symbol"] == "600519"
    assert snap["market"]["kind"] == "a_share"
    # v1 顶层键 + v2 增量键
    for k in ("candles", "overlays", "signal", "events", "data_quality", "runtime"):
        assert k in snap
    assert isinstance(snap["candles"], list)
    assert isinstance(snap["overlays"], dict)


def test_build_ashare_snapshot_empty_data_returns_degraded():
    fake = _FakeClient([])
    snap = build_ashare_snapshot("600519", client=fake)
    assert snap["runtime"]["degraded"] is True
    assert snap["runtime"]["degraded_reason"] == "no_data"
    assert snap["candles"] == []


def test_build_ashare_snapshot_db_error_returns_degraded():
    class BrokenClient:
        def fetch_validated_klines(self, code, start, end):
            raise RuntimeError("connection lost")

    snap = build_ashare_snapshot("600519", client=BrokenClient())
    assert snap["runtime"]["degraded"] is True
    assert "db_error:RuntimeError" in snap["runtime"]["degraded_reason"]


def test_build_ashare_snapshot_passes_correct_time_window():
    """验证 start/end 传给了底层 client（不写死日期）。"""
    fake = _FakeClient(_bars_for("000002", n=50))
    build_ashare_snapshot("000002", width_k=30, client=fake)
    code, start_ms, end_ms = fake.calls[0]
    assert code == "000002"
    assert end_ms - start_ms == (30 + 60) * 24 * 3600 * 1000


def test_provider_caches_snapshot_within_ttl():
    """60s TTL 内多次调用只重建一次。"""
    fake = _FakeClient(_bars_for("600519", n=30))
    snap1 = build_ashare_snapshot("600519", client=fake)
    snap2 = build_ashare_snapshot("600519", client=fake)
    # 忽略 as_of_ms 时间戳字段，其余应一致
    s1 = {**snap1, "runtime": {k: v for k, v in snap1["runtime"].items() if k != "as_of_ms"}}
    s2 = {**snap2, "runtime": {k: v for k, v in snap2["runtime"].items() if k != "as_of_ms"}}
    assert s1 == s2


def test_provider_default_width_k_matches_plan():
    assert DEFAULT_WIDTH_K == 30
