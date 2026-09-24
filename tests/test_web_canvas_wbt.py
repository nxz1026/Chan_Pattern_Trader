"""``/api/canvas/wbt`` 路由测试（R16-5 画布 D 的服务端入口）。

CI 没有 wbt（可选依赖 extra ``report``），所以这里只断言**响应形状与参数校验**；
真正"由 wbt 渲染且零 CDN 外链"的断言在 ``tests/test_canvas_wbt.py`` 里按需跳过。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from cpt.web.app import serve_snapshot


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


@contextmanager
def _served() -> Iterator[str]:
    server = serve_snapshot(_snapshot)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def test_canvas_wbt_route_returns_stable_shape() -> None:
    with _served() as base:
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
    with _served() as base:
        full = _get(f"{base}/api/canvas/wbt?start_ms=1700000000000&end_ms=1700018000000")
        narrow = _get(f"{base}/api/canvas/wbt?start_ms=1700000000000&end_ms=1700003600000")
    assert full["counts"]["candles"] == 6
    assert narrow["counts"]["candles"] == 2
    # 窗口变窄 ⇒ 画出来的结构不可能变多
    assert narrow["counts"]["fractals"] <= full["counts"]["fractals"]


def test_canvas_wbt_route_without_window_does_not_filter() -> None:
    with _served() as base:
        payload = _get(f"{base}/api/canvas/wbt")
    assert payload["counts"]["candles"] == 6


def test_canvas_wbt_route_rejects_non_integer_window() -> None:
    with _served() as base:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(f"{base}/api/canvas/wbt?start_ms=abc&end_ms=def")
    assert excinfo.value.code == 400


def test_canvas_wbt_route_requires_both_bounds() -> None:
    # 只给一端 ⇒ 不过滤（不报错），避免客户端半截参数把画布打成空白
    with _served() as base:
        payload = _get(f"{base}/api/canvas/wbt?start_ms=1700000000000")
    assert payload["counts"]["candles"] == 6
