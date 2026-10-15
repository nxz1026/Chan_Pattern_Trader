"""R61 端到端渲染回归：A 股「市场概览」必须**真在 DOM 里**画出这些数。

## 为什么必须起浏览器

R61 实际踩到的两个坑，**纯字符串契约测试一个都拦不住**：

1. ``SOURCE_LABELS`` 只加进了 ``dash-chrome.js`` 的 ``<<<HEAD`` 段，而
   ``build_dashboard_bundle.py`` 只拼 ``dash-core.js`` 的权威头 —— 声明被静默丢弃。
   ``dashboard_js()``（读的是**源文件**拼接）照样能看到这个名字，测试全绿；
   但线上 ``renderChrome`` 跑到用它的那一行抛 ``ReferenceError``，被上层
   ``try/catch`` 吞掉，**其后所有 DOM 写入都不执行**：页面无报错，只是
   「当日最高/最低/成交」那几行永远停在初始值。
2. ``formatNumber`` 内部是 ``Math.abs`` —— ``formatPrice(-3.12)`` 返回 ``"3.12"``，
   跌 3 块显示成涨 3 块。只有读渲染结果才看得见。

所以这里用真实 chromium 跑一遍真实 bundle + 一份合成快照（不碰生产库、不联网），
断言渲染出来的**文本**。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import CHROME_FLAGS, CHROME_TIMEOUT_SECONDS, chromium_path, chromium_runnable

ROOT = Path(__file__).parents[1]
DASH = ROOT / "dashboard"

#: 与 R61 线上实测同形（600519 / 2026-10-13）的合成快照。
_SNAPSHOT: dict = {
    "schema_version": 2,
    "market": {
        "kind": "a_share",
        "name": "贵州茅台",
        "symbol": "600519",
        "board": "SH",
        "board_label": "沪市主板",
        "last_price": 8151.84,
        "last_price_raw": 1274.89,
        "price_ratio": 6.394152,
        "last_bar_date": "2026-10-13",
        "last_high": 8238.7365,
        "last_high_raw": 1288.48,
        "last_low": 8108.4876,
        "last_low_raw": 1268.11,
        "last_volume_lots": 26573.0,
        "last_amount": 3393315572.0,
        "bar_count": 2,
        "first_open_time": 1776384000000,
        "last_open_time": 1791820800000,
        "interval_ms": 86400000,
    },
    "candles": [
        {
            "open_time": 1776384000000,
            "close": 8000.0,
            "open": 7900.0,
            "high": 8100.0,
            "low": 7800.0,
            "volume": 100.0,
        },
        {
            "open_time": 1791820800000,
            "close": 8151.84,
            "open": 8100.0,
            "high": 8238.7365,
            "low": 8108.4876,
            "volume": 26573.0,
        },
    ],
    "data_quality": {
        "stale": True,
        "stale_reason": "factor_missing",
        "stale_reason_day": "2026-10-14",
        "gap": False,
        "closed_bar_count": 2,
        "unclosed_bar_count": 0,
        "reason": None,
    },
    "dual_compare": {
        "available": True,
        "cpt_close": 8151.84,
        "realtime_price": 8131.89,
        "divergence_pct": -0.2447,
        "realtime": {
            "source": "sina",
            "quote_date": "2026-10-14",
            "quote_time": "10:11:47",
            "raw_price": 1271.77,
            "prev_close": 8151.84,
            "hfq_factor": 6.39415165,
            "change_pct": -0.2447,
        },
    },
}


def _harness(tmp_path: Path, snapshot: dict | None = None) -> Path:
    """把真实 dashboard 目录**软链**进临时目录，只替换 index.html 的取数地址。"""
    for entry in DASH.iterdir():
        if entry.name == "index.html":
            continue
        (tmp_path / entry.name).symlink_to(entry)
    html = (DASH / "index.html").read_text(encoding="utf-8")
    html = html.replace(
        'data-snapshot-url="/cpt/api/dashboard/snapshot"',
        'data-snapshot-url="/api/dashboard/a-share"',
    )
    stub = (
        "<script>\n(function () {\n"
        "  window.__ERRS = [];\n"
        '  window.addEventListener("error", function (e) {\n'
        "    window.__ERRS.push(String(e.message));\n"
        "  });\n"
        "  var SNAP = " + json.dumps(snapshot if snapshot is not None else _SNAPSHOT) + ";\n"
        "  window.fetch = function (input) {\n"
        '    var url = typeof input === "string" ? input : (input && input.url) || "";\n'
        '    var body = url.indexOf("/llm/") >= 0 ? { available: false } : SNAP;\n'
        "    return Promise.resolve(new Response(JSON.stringify(body),"
        ' { status: 200, headers: { "Content-Type": "application/json" } }));\n'
        "  };\n})();\n</script>\n"
    )
    marker = '    <script src="./url_safety.js" defer></script>'
    assert marker in html
    html = html.replace(marker, stub + marker)
    html = html.replace(
        "</body>",
        "<script>setTimeout(function(){var d=document.createElement('pre');"
        "d.id='__errs_out';d.textContent='ERRS='+JSON.stringify(window.__ERRS);"
        "document.body.appendChild(d);},1200);</script></body>",
    )
    target = tmp_path / "index_stub.html"
    target.write_text(html, encoding="utf-8")
    return target


def _text(dom: str, testid: str) -> str:
    pattern = rf'<[^>]*data-testid="{re.escape(testid)}"[^>]*>(.*?)</'
    m = re.search(pattern, dom, re.S)
    assert m, f"DOM 里没有 {testid}"
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()


@pytest.mark.skipif(
    not chromium_runnable(),
    reason="Chromium 本机不可运行（aarch64 环境 SIGTRAP/缺 GUI·沙箱依赖，非代码缺陷）",
)
def test_ashare_overview_renders_both_price_bases(tmp_path: Path) -> None:
    browser = chromium_path()
    assert browser is not None
    harness = _harness(tmp_path)
    result = subprocess.run(
        [
            browser,
            *CHROME_FLAGS,
            "--virtual-time-budget=4000",
            "--dump-dom",
            f"file://{harness}?market=a_share&code=600519",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "HOME": str(Path.home())},
        timeout=CHROME_TIMEOUT_SECONDS,
    )
    dom = result.stdout

    # 页面里不该有任何未捕获异常（静默丢声明就是这个测试要抓的东西）
    errs = re.search(r"<pre id=\"__errs_out\">(.*?)</pre>", dom, re.S)
    assert errs and errs.group(1).strip() == "ERRS=[]", (
        f"渲染期有 JS 错误: {errs and errs.group(1)}"
    )

    # 后复权价旁边必须有不复权价（用户提的原始诉求）
    assert _text(dom, "market-last-price") == "8,151.84"
    assert _text(dom, "market-last-price-raw") == "（1,274.89）"
    assert _text(dom, "market-price-basis") == "后复权 ×6.394152（括号内为不复权）"
    assert _text(dom, "market-data-asof") == "2026-10-13"

    # 涨跌幅与涨跌额：符号必须对（formatNumber 内部是 Math.abs）
    assert _text(dom, "market-change") == "-0.24%"
    assert _text(dom, "market-change-amount") == "-3.12"

    # 当日高/低/量/额：A 股口径，不再是「不可用」「24h」
    assert _text(dom, "market-high-label") == "当日最高"
    assert _text(dom, "market-high-24h") == "8,238.74（1,288.48）"
    assert _text(dom, "market-low-label") == "当日最低"
    assert _text(dom, "market-low-24h") == "8,108.49（1,268.11）"
    assert _text(dom, "market-volume-label") == "当日成交"
    assert _text(dom, "market-volume") == "2.66 万手 / 33.93 亿元"

    # 板块不能是裸码
    assert _text(dom, "market-board") == "沪市主板"

    # 实时价标签跟上游走（R35 之后是新浪），不是写死的东财
    assert _text(dom, "dual-realtime-label") == "实时价（新浪）"

    # stale 只说「K 线没跟上」，原因单独一行（上游没入库 vs 缺复权因子，排查路径不同）
    assert _text(dom, "data-quality-stale") == "true"
    assert _text(dom, "data-quality-stale-reason-label") == "K 线滞后原因"
    assert _text(dom, "data-quality-stale-reason") == "当日 bar 已入库，缺复权因子（2026-10-14）"


@pytest.mark.skipif(
    not chromium_runnable(),
    reason="Chromium 本机不可运行（aarch64 环境 SIGTRAP/缺 GUI·沙箱依赖，非代码缺陷）",
)
def test_ashare_overview_renders_raw_fallback_basis(tmp_path: Path) -> None:
    """R64：整段不复权兜底时面板不能把原始价说成后复权价。

    这是 2026-10-15 的真实形态：600519 的因子只到 10-13，K 线只认带因子的 bar ⇒
    旧行为下快照停在 10-13（「今天多少钱」都看不到）。兜底后末根是**今天**、
    面板上的价**就是**不复权价 —— 口径行必须改口，且不能再并排一个同样的数。
    """
    browser = chromium_path()
    assert browser is not None
    snapshot = json.loads(json.dumps(_SNAPSHOT))
    market = snapshot["market"]
    market["price_basis"] = "raw"
    market["factor_last_date"] = "2026-10-13"
    for key in ("price_ratio", "last_price_raw", "last_high_raw", "last_low_raw"):
        market.pop(key, None)
    market["last_price"] = 1274.89
    market["last_high"] = 1288.48
    market["last_low"] = 1268.11
    market["last_bar_date"] = "2026-10-15"
    harness = _harness(tmp_path, snapshot)

    result = subprocess.run(
        [
            browser,
            *CHROME_FLAGS,
            "--virtual-time-budget=4000",
            "--dump-dom",
            f"file://{harness}?market=a_share&code=600519",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "HOME": str(Path.home())},
        timeout=CHROME_TIMEOUT_SECONDS,
    )
    dom = result.stdout

    errs = re.search(r"<pre id=\"__errs_out\">(.*?)</pre>", dom, re.S)
    assert errs and errs.group(1).strip() == "ERRS=[]", (
        f"渲染期有 JS 错误: {errs and errs.group(1)}"
    )

    assert _text(dom, "market-last-price") == "1,274.89"
    # 不再并排第二个同样的数（后端也不发这个键）
    assert _text(dom, "market-last-price-raw") == ""
    assert _text(dom, "market-price-basis") == "不复权（当日缺复权因子，整段未复权）"
    assert _text(dom, "market-data-asof") == "2026-10-15"
    # 高低只有一套数，不带括号里的第二口径
    assert _text(dom, "market-high-24h") == "1,288.48"
    assert _text(dom, "market-low-24h") == "1,268.11"
    # 涨跌额在不复权兜底时也要有：此时没有 last_price_raw，反推基准必须用 lastPrice
    amount = _text(dom, "market-change-amount")
    assert amount.startswith("+") and amount != "+", f"涨跌额丢了: {amount!r}"
