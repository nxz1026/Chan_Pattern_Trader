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
    SINA_BATCH_LIMIT,
    ASharePublicError,
    SinaQuoteClient,
    TencentKlineClient,
    normalize_code,
    parse_sina_quotes,
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


# ───────────────────────────── R82：批量 + 五档 ─────────────────────────────
#
# 真实响应形状取自 2026-10-21 实测（`hq.sinajs.cn/list=sh600519,sz000002,sz000001`）：
# 34 字段，`[10]=买一量 [11]=买一价 ... [20]=卖一量 [21]=卖一价`。
# **量与价的先后是反直觉的**，照 `[10]=价` 解析会得到自洽但错误的数。


def _sina_row(
    name: str,
    *,
    last: str = "10.00",
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
) -> str:
    """造一行新浪 34 字段 body。``bids``/``asks`` 是 ``[(价, 量), ...]``，从买一往买五排。"""
    bids = bids if bids is not None else [("9.99", "100")] * 5
    asks = asks if asks is not None else [("10.01", "200")] * 5
    # 下标对齐 2026-10-21 实测：0=名 1=开 2=昨收 3=现价 4=高 5=低 6=买一价 7=卖一价
    # 8=成交量 9=成交额 10..19=买五档(先量后价) 20..29=卖五档(先量后价)
    # 30=日期 31=时间 32=状态 33=附加字段
    head = [name, "9.90", "9.80", last, "10.20", "9.70", bids[0][0], asks[0][0]]
    body = [str(1000000), "12345678.0"]
    for price, vol in bids:  # 先量后价
        body.extend([vol, price])
    for price, vol in asks:  # 先量后价
        body.extend([vol, price])
    body.extend(["2026-10-21", "14:30:00", "00", "D|4300|5631667.00"])
    return ",".join(head + body)


def _sina_response(rows: dict[str, str]) -> bytes:
    body = "".join(f'var hq_str_{sym}="{row}";\n' for sym, row in rows.items())
    return body.encode("gbk")


def test_sina_batch_parses_five_levels_with_volume_before_price() -> None:
    """量与价的顺序：`[10]=买一量 [11]=买一价`。反了就会得到「买一价 200 元」。"""
    payload = _sina_response(
        {
            "sh600519": _sina_row(
                "贵州茅台",
                last="1309.69",
                bids=[("1309.59", "200"), ("1309.35", "100")] + [("1300.00", "50")] * 3,
                asks=[("1309.69", "4568"), ("1309.80", "1000")] + [("1310.00", "70")] * 3,
            )
        }
    )
    quotes = SinaQuoteClient(opener=lambda u, t, h: payload).fetch_quotes(["600519"])
    q = quotes["600519"]
    assert q["bid1_price"] == pytest.approx(1309.59)
    assert q["bid1_vol"] == pytest.approx(200.0)
    assert q["ask1_price"] == pytest.approx(1309.69)
    assert q["ask1_vol"] == pytest.approx(4568.0)
    # 反过来的读法会得到这两组数；钉死它们防止有人「顺手调换」
    assert q["bid1_price"] != pytest.approx(200.0)
    assert q["bid1_vol"] != pytest.approx(1309.59)
    assert q["bids"][0] == {"price": 1309.59, "volume": 200.0}
    assert q["asks"][0] == {"price": 1309.69, "volume": 4568.0}
    assert len(q["bids"]) == 5 and len(q["asks"]) == 5


def test_sina_batch_keys_are_bare_codes_not_prefixed_symbols() -> None:
    """返回值按 6 位 code 索引 —— 调用方手上就是 daily_bar.code。

    R82 实跑第一版返回 `sh600519` 这类前缀键，按 6 位 code 取全是 KeyError。
    """
    payload = _sina_response({"sz000002": _sina_row("万 科Ａ"), "sh600519": _sina_row("贵州茅台")})
    quotes = SinaQuoteClient(opener=lambda u, t, h: payload).fetch_quotes(["000002", "600519"])
    assert set(quotes) == {"000002", "600519"}
    # quote 里仍保留全限定符号（与 fetch_quote 一致）
    assert quotes["000002"]["code"] == "sz000002"
    assert quotes["600519"]["code"] == "sh600519"


def test_sina_batch_accepts_mixed_code_spellings_and_dedupes() -> None:
    payload = _sina_response({"sh600519": _sina_row("贵州茅台")})
    quotes = SinaQuoteClient(opener=lambda u, t, h: payload).fetch_quotes(
        ["600519", "600519.SH", "sh600519"]
    )
    assert list(quotes) == ["600519"]


def test_sina_suspended_stock_yields_no_levels_not_zeros() -> None:
    """停牌：整行 body 全 0。价格 <= 0 的档位整对置 None，不记成「挂 0 手」。"""
    row = _sina_row("停牌股", last="0.00", bids=[("0.00", "0")] * 5, asks=[("0.00", "0")] * 5)
    payload = _sina_response({"sz000016": row})
    q = SinaQuoteClient(opener=lambda u, t, h: payload).fetch_quotes(["000016"])["000016"]
    assert q["bids"] == [None] * 5
    assert q["asks"] == [None] * 5
    assert q["bid1_price"] is None
    assert q["bid1_vol"] == pytest.approx(0.0)  # 量本身是 0，保留


def test_sina_limit_up_has_bids_but_no_asks() -> None:
    """封涨停的真实形态（000518 四环生物 2026-10-21 实测）：买盘有量、卖盘为 0。"""
    row = _sina_row(
        "四环生物",
        last="3.99",
        bids=[("3.99", "5000000")] + [("3.98", "1000")] * 4,
        asks=[("0.00", "0")] * 5,
    )
    payload = _sina_response({"sz000518": row})
    q = SinaQuoteClient(opener=lambda u, t, h: payload).fetch_quotes(["000518"])["000518"]
    assert q["bids"][0] == {"price": 3.99, "volume": 5000000.0}
    assert q["asks"] == [None] * 5


def test_sina_empty_body_row_is_skipped_not_zeroed() -> None:
    """`var hq_str_sz000000="";` —— 空 body 整只不出现，不返回 0 元报价。"""
    text = 'var hq_str_sz000002=""";\n' + f'var hq_str_sh600519="{_sina_row("贵州茅台")}";\n'
    quotes = parse_sina_quotes(text)
    assert set(quotes) == {"sh600519"}


def test_sina_batch_chunks_beyond_the_limit() -> None:
    """超过 SINA_BATCH_LIMIT 自动切块，每块一次请求，结果合并。"""
    codes = [f"{600000 + i}" for i in range(SINA_BATCH_LIMIT + 20)]
    urls: list[str] = []

    def opener(url: str, timeout: float, headers: Mapping[str, str] | None) -> bytes:
        urls.append(url)
        syms = url.split("list=")[1].split(",")
        return _sina_response({s: _sina_row(f"票{s[-3:]}") for s in syms})

    quotes = SinaQuoteClient(opener=opener).fetch_quotes(codes)
    assert len(quotes) == len(codes)
    assert len(urls) == 2
    assert urls[0].split("list=")[1].count(",") + 1 == SINA_BATCH_LIMIT
    assert urls[1].split("list=")[1].count(",") + 1 == 20


def test_sina_batch_empty_input_makes_no_request() -> None:
    def opener(url: str, timeout: float, headers: Mapping[str, str] | None) -> bytes:
        raise AssertionError("不该发请求")

    assert SinaQuoteClient(opener=opener).fetch_quotes([]) == {}


def test_sina_batch_raises_when_nothing_matches() -> None:
    """响应里一只都没匹配上 = 上游格式变了，必须报，不能返回空字典当成功。"""
    with pytest.raises(ASharePublicError, match="一只都没匹配上"):
        SinaQuoteClient(opener=lambda u, t, h: b"<html>502</html>").fetch_quotes(["600519"])


def test_sina_batch_raises_on_short_row() -> None:
    """字段不足说明响应格式变了，静默降级会让下游拿到半截数据。"""
    text = 'var hq_str_sh600519="贵州茅台,1,2,3";\n'
    with pytest.raises(ASharePublicError, match="字段不足"):
        parse_sina_quotes(text)


def test_sina_batch_http_error_is_not_swallowed() -> None:
    import urllib.error

    def opener(url: str, timeout: float, headers: Mapping[str, str] | None) -> bytes:
        raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)  # type: ignore[arg-type]

    with pytest.raises(ASharePublicError, match="HTTP 403"):
        SinaQuoteClient(opener=opener).fetch_quotes(["600519"])


def test_sina_fetch_quote_return_contract_is_unchanged() -> None:
    """`fetch_quote` 的键集合一个字都不能变 —— 生产路径在读它。"""
    payload = _sina_response({"sh600519": _sina_row("贵州茅台")})
    quote = SinaQuoteClient(opener=lambda u, t, h: payload).fetch_quote("600519")
    assert set(quote) == {
        "code",
        "name",
        "open",
        "prev_close",
        "last",
        "high",
        "low",
        "volume",
        "date",
        "time",
    }
