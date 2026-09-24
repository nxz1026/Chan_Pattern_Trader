"""画布 D（wbt 报告）的单元测试。

CI 不装 wbt（``requirements-dev.txt`` 只有 czsc 之外的轻量依赖），所以涉及
真实渲染的用例一律 ``importorskip("wbt")``；不依赖 wbt 的纯函数（窗口过滤、
body 抽取、CDN 剥离）在 CI 里照跑。
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from cpt.application.canvas_wbt import (
    WBT_PINNED_VERSION,
    WbtUnavailableError,
    build_canvas_d_payload,
    extract_body_fragment,
)

CDN_RE = re.compile(r"""(?:src|href)\s*=\s*["']https?://""", re.IGNORECASE)


def _snapshot(bars: int = 12) -> dict[str, Any]:
    candles = [
        {
            "open_time": 1_700_000_000_000 + index * 3_600_000,
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.5 + index,
            "volume": 10.0 + index,
        }
        for index in range(bars)
    ]
    times = [bar["open_time"] for bar in candles]
    return {
        "market": {"symbol": "TESTUSDT", "kind": "crypto"},
        "candles": candles,
        "overlays": {
            "fractals": [
                {
                    "kind": "top" if index % 2 == 0 else "bottom",
                    "start_time": times[index],
                    "end_time": times[index],
                    "high": 101.0 + index,
                    "low": 99.0 + index,
                    "level": 1,
                    "source_ids": [f"f{index}"],
                }
                for index in range(bars)
            ],
            "bis": [
                {
                    "direction": 1 if index % 2 == 0 else -1,
                    "start_time": times[index],
                    "end_time": times[index + 1],
                    "high": 101.0 + index,
                    "low": 99.0 + index,
                    "level": 1,
                    "source_ids": [f"b{index}"],
                }
                for index in range(bars - 1)
            ],
            "zhongshus": [
                {
                    "start_time": times[0],
                    "end_time": times[bars - 1],
                    "high": 105.0,
                    "low": 102.0,
                    "level": 1,
                    "bi_ids": ["b0", "b1", "b2"],
                }
            ],
            "trend_types": [
                {
                    "kind": "up",
                    "start_time": times[0],
                    "end_time": times[bars - 1],
                    "level": 1,
                }
            ],
        },
    }


def test_extract_body_fragment_splits_scripts() -> None:
    document = (
        "<html><head><link href='https://cdn.example/x.css'></head>"
        "<body><p>正文</p><script>var a=1;</script><script src='https://cdn.example/a.js'></script>"
        "</body></html>"
    )
    body, scripts = extract_body_fragment(document)
    assert "<p>正文</p>" in body
    assert "<script" not in body
    assert len(scripts) == 2
    # head 里的 CDN 链接随 head 一起被丢弃 —— 这是离线验收的关键一步。
    assert "cdn.example/x.css" not in body


def test_extract_body_fragment_requires_body() -> None:
    with pytest.raises(ValueError, match="body"):
        extract_body_fragment("<html><head></head></html>")


def test_window_filter_keeps_only_intersecting_structures() -> None:
    snapshot = _snapshot(12)
    times = [bar["open_time"] for bar in snapshot["candles"]]
    window = (times[4], times[7])
    payload = build_canvas_d_payload(snapshot, window=window)
    counts = payload["counts"]
    assert counts["canvas"] == "D"
    assert counts["candles"] == 4
    # 分型：时间点落在窗口内的 4 个
    assert counts["fractals"] == 4
    # 笔：与窗口**相交**的算，不是"两端都在窗口内"的算 ——
    # bi3(t3→t4) 尾端压在窗口起点、bi7(t7→t8) 首端压在窗口终点，都算。
    assert counts["bis"] == 5
    # 中枢/走势类型横跨全部 K 线，必然与窗口相交
    assert counts["zhongshus"] == 1
    assert counts["trendTypes"] == 1


def test_window_filter_excludes_structures_entirely_outside() -> None:
    snapshot = _snapshot(12)
    times = [bar["open_time"] for bar in snapshot["candles"]]
    # 把中枢/走势类型挪到最前面两根 K 线上，再把窗口放到最后四根 ——
    # 二者完全不相交，必须被过滤掉（这正是旧版画布 A"钳到图边缘"的反例）。
    snapshot["overlays"]["zhongshus"] = [
        {
            "start_time": times[0],
            "end_time": times[1],
            "high": 105.0,
            "low": 102.0,
            "level": 1,
            "bi_ids": ["b0"],
        }
    ]
    snapshot["overlays"]["trend_types"] = [
        {"kind": "up", "start_time": times[0], "end_time": times[1], "level": 1}
    ]
    payload = build_canvas_d_payload(snapshot, window=(times[8], times[11]))
    counts = payload["counts"]
    assert counts["candles"] == 4
    assert counts["fractals"] == 4
    assert counts["bis"] == 4
    assert counts["zhongshus"] == 0
    assert counts["trendTypes"] == 0


def test_no_candles_in_window_is_unavailable_not_an_error() -> None:
    snapshot = _snapshot(4)
    times = [bar["open_time"] for bar in snapshot["candles"]]
    payload = build_canvas_d_payload(snapshot, window=(times[-1] + 10**9, times[-1] + 2 * 10**9))
    assert payload["available"] is False
    assert payload["reason"] == "no_candles_in_window"
    assert payload["body_html"] == ""
    assert payload["counts"]["candles"] == 0


def test_unavailable_payload_shape_is_stable() -> None:
    payload = build_canvas_d_payload({"candles": [], "overlays": {}})
    assert set(payload) == {
        "available",
        "source",
        "reason",
        "css",
        "body_html",
        "scripts",
        "counts",
    }
    assert payload["available"] is False


def test_wbt_unavailable_error_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Any:
        raise WbtUnavailableError("画布 D 需要可选依赖 wbt")

    monkeypatch.setattr("cpt.application.canvas_wbt._import_wbt", _boom)
    payload = build_canvas_d_payload(_snapshot(4))
    assert payload["available"] is False
    assert "wbt_unavailable" in str(payload["reason"])
    # 计数仍然可用（客户端据此显示"结构有 N 个但报告不可用"）
    assert payload["counts"]["candles"] == 4


def test_pinned_version_constant() -> None:
    assert WBT_PINNED_VERSION == "0.9.1"


@pytest.mark.parametrize("bars", [12, 40])
def test_wbt_render_has_no_cdn_and_keeps_report_structure(bars: int) -> None:
    pytest.importorskip("wbt")
    snapshot = _snapshot(bars)
    times = [bar["open_time"] for bar in snapshot["candles"]]
    payload = build_canvas_d_payload(snapshot, window=(times[0], times[-1]))
    assert payload["available"] is True, payload["reason"]
    assert payload["source"] == f"wbt.report.HtmlReportBuilder@{WBT_PINNED_VERSION}"

    blob = payload["body_html"] + payload["css"] + "".join(payload["scripts"])
    assert not CDN_RE.search(blob), "报告片段里仍有 CDN 外链，离线验收会失败"

    # 真实复用 wbt 的渲染器：指标卡 / 数据表 / tab 导航 / 页脚都是 wbt 的类名
    assert payload["body_html"].count("stat-tile") >= 6
    assert "table table-striped" in payload["body_html"]
    assert "nav-tabs" in payload["body_html"]
    # plotly 图由 CPT 补上（wbt 自己画不了 K 线）。
    # trace 数据在 plotly 的内联 <script> 里，不在 body_html 里 —— body 只有空 div。
    assert "cpt-canvas-d-chart" in payload["body_html"]
    scripts_blob = "".join(payload["scripts"])
    assert "candlestick" in scripts_blob
    assert "newPlot" in scripts_blob
    # 外链脚本一律被剥离（wbt 模板的 bootstrap CDN script 就在 body 里）
    assert all("src=" not in script.split(">", 1)[0].lower() for script in payload["scripts"])
    assert len(payload["css"]) > 1000
