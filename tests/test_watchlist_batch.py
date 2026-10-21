"""R80：``--only watchlist`` 批次的行为测试。

用户 2026-10-21 的需求是「新加的关注要自动跑、跑不到要提醒」。这条链路上
最容易错的是**把热池也一起高频跑**（一天近万次缠论计算），所以下面钉住
「only 模式一次 DB 热池查询都不发」。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BATCH_PATH = ROOT / "scripts/snapshot_a_share_batch.py"


def _load_batch() -> Any:
    """按路径加载脚本模块（``scripts/`` 不是包，没有 ``__init__``）。"""
    name = "_cpt_snapshot_batch_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, BATCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_only_watchlist_never_queries_the_hot_pool(monkeypatch: Any) -> None:
    """``only="watchlist"`` 时**一次** ``fetch_hot_pool`` 都不调。

    热池 97 只 × 每 15 分钟 = 一天近万次缠论计算 —— 这条测试守的就是别把它加回来。
    """
    batch = _load_batch()
    calls: list[int] = []

    def boom(*args: Any, **kwargs: Any) -> list[Any]:
        calls.append(1)
        raise AssertionError("only=watchlist 不该查热池")

    monkeypatch.setattr(batch, "fetch_hot_pool", boom)
    monkeypatch.setattr(batch, "WatchlistStore", lambda *a, **k: _FakeStore(["000001", "600519"]))
    codes = batch.collect_codes(hot_limit=50, only="watchlist")
    assert calls == []
    assert codes == ["000001", "600519"]


def test_default_mode_still_merges_hot_pool_and_watchlist(monkeypatch: Any) -> None:
    """默认（``only=None``）行为不变：热池在前、关注补缺、去重保序。"""

    class _Entry:
        def __init__(self, code: str) -> None:
            self.code = code

    class _FakeClient:
        def _get_conn(self) -> object:
            return object()

        def close(self) -> None:
            return None

    batch = _load_batch()
    monkeypatch.setattr(batch, "AShareLocalClient", _FakeClient)
    monkeypatch.setattr(
        batch, "fetch_hot_pool", lambda conn, limit: [_Entry("600519"), _Entry("000001")]
    )
    monkeypatch.setattr(batch, "WatchlistStore", lambda *a, **k: _FakeStore(["000001", "000002"]))
    codes = batch.collect_codes(hot_limit=2)
    assert codes == ["600519", "000001", "000002"]


def test_watchlist_entries_from_other_markets_are_skipped(monkeypatch: Any) -> None:
    """只跑 A 股 —— 关注列表里可能有加密交易对。"""

    class _Entry:
        def __init__(self, code: str, market: str) -> None:
            self.code = code
            self.market = market

    batch = _load_batch()
    monkeypatch.setattr(
        batch,
        "WatchlistStore",
        lambda *a, **k: _FakeStoreRaw([_Entry("000001", "A"), _Entry("BTCUSDT", "crypto")]),
    )
    assert batch.collect_codes(hot_limit=1, only="watchlist") == ["000001"]


class _FakeStore:
    def __init__(self, codes: list[str]) -> None:
        self._codes = codes

    def list(self) -> list[Any]:
        return _FakeStoreRaw([_E(c) for c in self._codes]).list()


class _E:
    def __init__(self, code: str) -> None:
        self.code = code
        self.market = "A"


class _FakeStoreRaw:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def list(self) -> list[Any]:
        return self._entries
