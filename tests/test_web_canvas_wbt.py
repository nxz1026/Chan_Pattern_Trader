"""``/api/canvas/wbt`` 路由测试（R16-5 画布 D 的服务端入口）。

CI 没有 wbt（可选依赖 extra ``report``），所以这里只断言**响应形状与参数校验**；
真正"由 wbt 渲染且零 CDN 外链"的断言在 ``tests/test_canvas_wbt.py`` 里按需跳过。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from tests.conftest import served


def _snapshot() -> dict[str, Any]:
    candles = [
        {
            "open_time": 1_700_000_000_000 + index * 3_600_000,
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.5 + index,
            "volume": 5.0,
        }
        for index in range(6)
    ]
    times = [bar["open_time"] for bar in candles]
    return {
        "schema_version": "dashboard.v2",
        "market": {"symbol": "TESTUSDT", "kind": "crypto"},
        "runtime": {"mode": "fixture"},
        "candles": candles,
        "overlays": {
            "fractals": [
                {
                    "kind": "top",
                    "start_time": times[0],
                    "end_time": times[0],
                    "high": 101.0,
                    "low": 99.0,
                }
            ],
            "bis": [
                {
                    "direction": 1,
                    "start_time": times[0],
                    "end_time": times[-1],
                    "high": 106.0,
                    "low": 99.0,
                }
            ],
            "zhongshus": [
                {
                    "start_time": times[0],
                    "end_time": times[-1],
                    "high": 104.0,
                    "low": 101.0,
                    "bi_ids": ["b0"],
                }
            ],
            "trend_types": [{"kind": "up", "start_time": times[0], "end_time": times[-1]}],
        },
    }


def _ashare_like_snapshot() -> dict[str, Any]:
    """3 根日线的 A 股快照 —— 与 ``_snapshot()`` 的 6 根**故意不同**，
    这样"画布 D 到底用了哪一份"从计数上就能看出来。"""
    base = _snapshot()
    candles = base["candles"][:3]
    times = [bar["open_time"] for bar in candles]
    base["market"] = {"symbol": "002614", "kind": "a_share", "interval": "1d"}
    base["candles"] = candles
    base["overlays"] = {
        "fractals": [
            {
                "kind": "top",
                "start_time": times[0],
                "end_time": times[0],
                "high": 101.0,
                "low": 99.0,
            }
        ],
        "bis": [],
        "zhongshus": [],
        "trend_types": [],
    }
    return base


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def test_canvas_wbt_route_returns_stable_shape() -> None:
    with served(_snapshot) as base:
        payload = _get(f"{base}/api/canvas/wbt?start_ms=1700000000000&end_ms=1700018000000")
    assert set(payload) == {
        "available",
        "source",
        "reason",
        "css",
        "body_html",
        "scripts",
        "counts",
    }
    assert payload["counts"]["canvas"] == "D"
    assert payload["counts"]["candles"] == 6
    assert payload["counts"]["bis"] == 1
    assert payload["counts"]["zhongshus"] == 1
    assert payload["source"].startswith("wbt.report.HtmlReportBuilder")


def test_canvas_wbt_route_honours_window() -> None:
    with served(_snapshot) as base:
        full = _get(f"{base}/api/canvas/wbt?start_ms=1700000000000&end_ms=1700018000000")
        narrow = _get(f"{base}/api/canvas/wbt?start_ms=1700000000000&end_ms=1700003600000")
    assert full["counts"]["candles"] == 6
    assert narrow["counts"]["candles"] == 2
    # 窗口变窄 ⇒ 画出来的结构不可能变多
    assert narrow["counts"]["fractals"] <= full["counts"]["fractals"]


def test_canvas_wbt_route_without_window_does_not_filter() -> None:
    with served(_snapshot) as base:
        payload = _get(f"{base}/api/canvas/wbt")
    assert payload["counts"]["candles"] == 6


def test_canvas_wbt_route_rejects_non_integer_window() -> None:
    with served(_snapshot) as base:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(f"{base}/api/canvas/wbt?start_ms=abc&end_ms=def")
    assert excinfo.value.code == 400


def test_canvas_wbt_route_requires_both_bounds() -> None:
    # 只给一端 ⇒ 不过滤（不报错），避免客户端半截参数把画布打成空白
    with served(_snapshot) as base:
        payload = _get(f"{base}/api/canvas/wbt?start_ms=1700000000000")
    assert payload["counts"]["candles"] == 6


def test_canvas_wbt_route_with_code_uses_ashare_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """画布 D 是**服务端**取数的，带 code 必须换成 A 股快照。

    回归（R17-3 审计实测）：不带 code 时服务端拿的是 provider 的**加密**快照，
    于是 A/B/C 画 123 根 A 股 K 线、D 画 579 根 BTCUSDT K 线 —— 一屏两个市场。
    """
    from cpt.web import a_share_routes

    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_snapshot(code: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((code, kwargs))
        return _ashare_like_snapshot()

    monkeypatch.setattr(a_share_routes, "snapshot_payload", _fake_snapshot)
    with served(_snapshot) as base:
        payload = _get(
            f"{base}/api/canvas/wbt?start_ms=1700000000000&end_ms=1700018000000&code=002614"
        )
    # 路由**不传** width_k：客户端与画布 D 都吃 a_share_routes.DEFAULT_WIDTH_K，
    # 两边必须用同一个默认值，否则窗口根数不同、计数又不相等。
    assert calls == [("002614", {})]
    assert a_share_routes.DEFAULT_WIDTH_K == 120
    # 用的是 A 股那 3 根，而不是 provider 的 6 根
    assert payload["counts"]["candles"] == 3
    assert payload["counts"]["canvas"] == "D"


def test_canvas_wbt_route_without_code_keeps_crypto_snapshot() -> None:
    """不带 code 必须保持原行为（加密侧零影响）。"""
    with served(_snapshot) as base:
        payload = _get(f"{base}/api/canvas/wbt?start_ms=1700000000000&end_ms=1700018000000")
    assert payload["counts"]["candles"] == 6


def test_canvas_wbt_route_rejects_bad_code() -> None:
    with served(_snapshot) as base:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(f"{base}/api/canvas/wbt?code=abc")
    assert excinfo.value.code == 400
