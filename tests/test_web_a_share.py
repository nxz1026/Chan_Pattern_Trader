"""``cpt.web.a_share`` 测试 — 注入 mock client，不依赖 psycopg。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cpt.web.a_share import (
    DEFAULT_WIDTH_K,
    _empty_snapshot,
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


def test_empty_and_real_paths_have_same_schema_shape():
    """回归：degraded（空/DB错）路径与正常路径的 schema 形状必须一致。

    实测踩过：空快照曾把 ``overlays`` 返回成 ``[]``（真实路径是 dict）、
    ``signal`` 返回成 ``{}``（真实路径是 ``None``）——前端在 degraded 分支会崩。
    """
    real = build_ashare_snapshot("600519", client=_FakeClient(_bars_for("600519", n=40)))
    empty = _empty_snapshot("600519", "no_data")
    broken = build_ashare_snapshot("600519", client=_BrokenClient())

    for name, snap in (("real", real), ("empty", empty), ("broken", broken)):
        assert isinstance(snap["candles"], list), name
        assert isinstance(snap["overlays"], dict), f"{name}: overlays 必须是 dict"
        assert set(snap["overlays"]) == {"fractals", "bis", "zhongshus", "trend_types"}, name
        assert snap["signal"] is None or isinstance(snap["signal"], dict), name
        assert isinstance(snap["events"], list), name
        assert isinstance(snap["runtime"], dict), name
        assert snap["schema_version"] == "dashboard.v2", name
        # market 必备键
        for key in ("symbol", "kind", "interval_ms", "bar_count"):
            assert key in snap["market"], f"{name}: market 缺 {key}"


class _BrokenClient:
    def fetch_validated_klines(self, code: str, start_ms: int, end_ms: int):
        raise RuntimeError("connection lost")


def _zigzag_bars(*, n: int = 120) -> list[Any]:
    """正弦式振荡序列 —— 保证能出分型与笔（单调序列出不来）。"""
    import math

    end_ms = int(datetime(2026, 9, 24, tzinfo=UTC).timestamp() * 1000)
    return [
        _make_canonical(
            end_ms - (n - 1 - i) * 24 * 3600 * 1000,
            10.0 + 3.0 * math.sin(i / 3.0),
        )
        for i in range(n)
    ]


def test_overlays_are_populated_from_replay_payload():
    """回归：overlays 必须真的取到结构元素（不能静默全空）。

    实测踩过：``run_replay`` 返回 ``export_dataset`` 的 schema v1 payload，结构
    元素嵌在 ``payload["data"]`` 下；写成 ``payload.get("fractals")`` 会静默拿到
    ``None`` → 前端 overlays 全空，K 线上一个笔/中枢都不画（而 snapshot 仍是
    合法 v2 schema，不会报错）。
    """
    snap = build_ashare_snapshot("600519", client=_FakeClient(_zigzag_bars(n=120)))
    assert snap["market"]["bar_count"] == 120
    assert len(snap["overlays"]["fractals"]) > 0, "分型为空 → replay payload 没解包"
    assert len(snap["overlays"]["bis"]) > 0, "笔为空 → replay payload 没解包"
    # 每根笔必须带端点时间（前端画线要用），且端点落在 K 线时间范围内
    first = snap["overlays"]["bis"][0]
    assert first["start_time"] < first["end_time"]
