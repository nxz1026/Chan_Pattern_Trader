"""A 股公开源兜底适配器测试（R17）。

**不联网**：所有用例注入 ``opener``。真实响应形状取自 2026-09-24 实测（见模块
docstring），特别是腾讯 ``fqkline`` 的 ``[日期, 开, 收, 高, 低, 量]`` 字段顺序。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from cpt.adapters.a_share_public import (
    DAILY_INTERVAL_MS,
    ASharePublicError,
    SinaQuoteClient,
    TencentKlineClient,
    normalize_code,
    parse_tencent_kline,
)

# 2026-09-24 腾讯 hfq 实测响应（截断到 3 根）
TENCENT_HFQ_PAYLOAD = {
    "code": 0,
    "msg": "",
    "data": {
        "sh600519": {
            "hfqday": [
                ["2026-09-22", "8850.124", "8859.410", "8927.393", "8827.332", "24573.000"],
                ["2026-09-23", "8866.332", "8845.003", "8959.021", "8843.033", "30981.000"],
                ["2026-09-24", "8838.081", "8764.863", "8872.523", "8731.378", "31239.000"],
            ]
        }
    },
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "sh600519"),
        ("600519.SH", "sh600519"),
        ("sh600519", "sh600519"),
        ("SH600519", "sh600519"),
        ("000002", "sz000002"),
        ("000002.SZ", "sz000002"),
        ("300059", "sz300059"),
        ("920025", "bj920025"),
        (" 600519.sh ", "sh600519"),
    ],
)
def test_normalize_code(raw: str, expected: str) -> None:
    assert normalize_code(raw) == expected


@pytest.mark.parametrize("bad", ["", "60051", "abcdef", "12345678", "000001.XX"])
def test_normalize_code_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ASharePublicError):
        normalize_code(bad)


def test_tencent_field_order_is_date_open_close_high_low_volume() -> None:
    """坑 1 的回归测试：字段顺序不是 OHLC。

    按 OHLC 解析会把 8764.863 当成最高价、8872.523 当成最低价 —— 数值上"像"数据，
    但方向完全错。这里逐根断言 open/close/high/low 的对应关系。
    """
    bars = parse_tencent_kline(json.dumps(TENCENT_HFQ_PAYLOAD), symbol="sh600519")
    assert len(bars) == 3
    last = bars[-1]
    assert last.open == pytest.approx(8838.081)
    assert last.close == pytest.approx(8764.863)
    assert last.high == pytest.approx(8872.523)
    assert last.low == pytest.approx(8731.378)
    assert last.volume == pytest.approx(31239.0)
    # 跌日：开 > 收，且高 ≥ 开、低 ≤ 收 —— 按 OHLC 解析必然违反
    assert last.open > last.close
    assert last.high >= max(last.open, last.close)
    assert last.low <= min(last.open, last.close)


def test_tencent_open_time_matches_local_db_convention() -> None:
    """与 ``cpt/adapters/a_share_local.py`` 同口径：00:00 UTC + 一天减 1ms。"""
    bars = parse_tencent_kline(json.dumps(TENCENT_HFQ_PAYLOAD), symbol="sh600519")
    expected = int(datetime(2026, 9, 24, tzinfo=UTC).timestamp() * 1000)
    assert bars[-1].open_time == expected
    assert bars[-1].close_time == expected + DAILY_INTERVAL_MS - 1
    assert bars[-1].is_closed is True


def test_tencent_ohlc_inconsistency_raises_instead_of_silently_passing() -> None:
    payload = {
        "code": 0,
        "data": {"sh600519": {"hfqday": [["2026-09-24", "10", "20", "15", "5", "100"]]}},
    }
    with pytest.raises(ASharePublicError, match="OHLC 不自洽"):
        parse_tencent_kline(json.dumps(payload), symbol="sh600519")


def test_tencent_missing_adjust_key_raises() -> None:
    """标的没有后复权数据时，明确报错而不是拿不复权价冒充后复权。"""
    payload = {"code": 0, "data": {"sh600519": {"day": [["2026-09-24", "1", "1", "1", "1", "1"]]}}}
    with pytest.raises(ASharePublicError, match="hfqday"):
        parse_tencent_kline(json.dumps(payload), symbol="sh600519")


def test_tencent_error_code_and_missing_symbol() -> None:
    with pytest.raises(ASharePublicError, match="错误码"):
        parse_tencent_kline(json.dumps({"code": 1, "msg": "bad"}), symbol="sh600519")
    with pytest.raises(ASharePublicError, match="缺少 sh600519 节点"):
        parse_tencent_kline(json.dumps({"code": 0, "data": {}}), symbol="sh600519")


def test_tencent_non_json_response() -> None:
    with pytest.raises(ASharePublicError, match="非 JSON"):
        parse_tencent_kline("<html>502 Bad Gateway</html>", symbol="sh600519")


def test_tencent_client_builds_expected_request_and_validates_limit() -> None:
    seen: dict[str, object] = {}

    def opener(url: str, timeout: float, headers: Mapping[str, str] | None) -> bytes:
        seen["url"] = url
        seen["timeout"] = timeout
        return json.dumps(TENCENT_HFQ_PAYLOAD).encode("utf-8")

    client = TencentKlineClient(opener=opener, timeout=5.0)
    bars = client.fetch_daily_bars("600519.SH", limit=3)
    assert len(bars) == 3
    assert "param=sh600519,day,,,3,hfq" in str(seen["url"])
    assert seen["timeout"] == 5.0
    with pytest.raises(ValueError, match="limit"):
        client.fetch_daily_bars("600519", limit=0)
    with pytest.raises(ValueError, match="limit"):
        client.fetch_daily_bars("600519", limit=10_000)


def test_tencent_client_rejects_unknown_adjust() -> None:
    with pytest.raises(ASharePublicError, match="复权口径"):
        TencentKlineClient(adjust="hfq2")


def test_sina_quote_parses_gbk_snapshot() -> None:
    # 新浪返回 GBK 编码的 JS 赋值语句（实测 2026-09-24）
    body = (
        'var hq_str_sh600519="贵州茅台,1250.010,1251.240,1237.000,1256.130,1231.050,'
        "1237.000,1237.050,3123935,3867310920.000,"
        + ",".join(["0"] * 20)
        + ',2026-09-24,15:34:59,00";'
    )
    captured: dict[str, object] = {}

    def opener(url: str, timeout: float, headers: Mapping[str, str] | None) -> bytes:
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        return body.encode("gbk")

    quote = SinaQuoteClient(opener=opener).fetch_quote("600519")
    assert quote["name"] == "贵州茅台"
    assert quote["last"] == pytest.approx(1237.0)
    assert quote["high"] == pytest.approx(1256.13)
    assert quote["date"] == "2026-09-24"
    assert quote["time"] == "15:34:59"
    # 新浪要求 Referer，否则 403（实测）
    assert "Referer" in dict(captured["headers"])  # type: ignore[arg-type]


def test_sina_quote_empty_body_raises() -> None:
    def opener(url: str, timeout: float, headers: Mapping[str, str] | None) -> bytes:
        return 'var hq_str_sh600519="";'.encode("gbk")

    with pytest.raises(ASharePublicError, match="为空或格式不符"):
        SinaQuoteClient(opener=opener).fetch_quote("600519")
