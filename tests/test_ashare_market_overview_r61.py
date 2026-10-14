"""R61：A 股「市场概览」口径修复的单元测试。

覆盖四组新增能力（每一组都对应线上面板上的一个具体视觉缺陷）：

1. :func:`board_label` —— ``board`` 裸值（SH/SZ/SZ_CHINEXT）分不出科创板；
2. :func:`_attach_price_basis` —— 后复权价旁边给出不复权价，因子缺失时**不伪造**；
3. :func:`_is_tradable_price` / :func:`_pct_from` —— 开盘前上游给 ``last=0.0``，
   原实现会算出 ``−100%``；
4. :func:`_attach_last_bar_stats` —— 末根 bar 的当日高/低/量/额（替代加密侧
   ``market_24h`` 与 118 根 bar 求和的错口径）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from cpt.application.a_share_snapshot import (
    _attach_last_bar_stats,
    _attach_price_basis,
    _attach_staleness,
    _is_tradable_price,
    _pct_from,
    _PriceBasis,
    board_label,
)

# ---------------------------------------------------------------------------
# 1. board_label：按 code 前缀判板块
# ---------------------------------------------------------------------------


def test_board_label_splits_star_market_out_of_sh() -> None:
    """``SH`` 里混着 617 只科创板，必须靠 688/689 前缀拆开。"""
    assert board_label("688981", "SH") == "科创板"
    assert board_label("689009", "SH") == "科创板"
    assert board_label("600519", "SH") == "沪市主板"


def test_board_label_shenzhen_prefixes() -> None:
    assert board_label("300750", "SZ") == "创业板"
    assert board_label("301001", "SZ") == "创业板"
    assert board_label("000001", "SZ") == "深市主板"
    assert board_label("002594", "SZ") == "深市主板"


def test_board_label_beijing_and_b_shares() -> None:
    assert board_label("430047", "SZ") == "北交所"
    assert board_label("832000", "SZ") == "北交所"
    assert board_label("870001", "SZ") == "北交所"
    assert board_label("920001", "SZ") == "北交所"
    assert board_label("900901", "SH") == "沪市B股"
    assert board_label("200011", "SZ") == "深市B股"


def test_board_label_falls_back_to_raw_board() -> None:
    """前缀认不出时才用 ``board``（这个库里 SZ_CHINEXT 恰好等于创业板）。"""
    assert board_label("123456", "SH") == "沪市主板"
    assert board_label("123456", "SZ") == "深市主板"
    assert board_label("123456", "SZ_CHINEXT") == "创业板"


def test_board_label_returns_none_instead_of_inventing_other() -> None:
    """认不出就返回 None（前端隐藏该行），**不编**一个"其它"。"""
    assert board_label("123456", None) is None
    assert board_label("", "") is None
    assert board_label("600519", None) is None  # board 为空时不猜沪市


# ---------------------------------------------------------------------------
# 2. _attach_price_basis：不复权价 + 倍率
# ---------------------------------------------------------------------------


def test_price_basis_gives_raw_price_beside_adjusted() -> None:
    """600519 / 2026-10-13：后复权 8151.84 ÷ 6.394 ≈ 1274.89。"""
    snapshot: dict = {"market": {}}
    _attach_price_basis(
        snapshot,
        _PriceBasis(last_close=8151.84, last_date=dt.date(2026, 10, 13), factor=6.394),
    )
    market = snapshot["market"]
    assert market["last_bar_date"] == "2026-10-13"
    assert market["price_ratio"] == 6.394
    # 8151.84 / 6.394 = 1274.9202…（倍率保留 6 位小数，这里只在分位上比对）
    assert market["last_price_raw"] == pytest.approx(1274.92, abs=1e-2)


def test_price_basis_without_factor_hides_ratio() -> None:
    """查不到因子 → 只给日期。显示 ``×1.0`` 等于把"不知道"说成"没除权"。"""
    snapshot: dict = {"market": {}}
    _attach_price_basis(
        snapshot,
        _PriceBasis(last_close=8151.84, last_date=dt.date(2026, 10, 13), factor=None),
    )
    market = snapshot["market"]
    assert market["last_bar_date"] == "2026-10-13"
    assert "price_ratio" not in market
    assert "last_price_raw" not in market


def test_price_basis_rejects_non_positive_factor() -> None:
    snapshot: dict = {"market": {}}
    _attach_price_basis(
        snapshot,
        _PriceBasis(last_close=100.0, last_date=dt.date(2026, 10, 13), factor=0.0),
    )
    assert "price_ratio" not in snapshot["market"]


def test_price_basis_no_date_still_gives_ratio() -> None:
    snapshot: dict = {"market": {}}
    _attach_price_basis(snapshot, _PriceBasis(last_close=639.4, last_date=None, factor=2.0))
    market = snapshot["market"]
    assert "last_bar_date" not in market
    assert market["price_ratio"] == 2.0
    assert market["last_price_raw"] == 319.7


def test_price_basis_non_positive_close_skips_raw_price() -> None:
    """close = 0 是"没数据"，别算出一个 0.0 的不复权价。"""
    snapshot: dict = {"market": {}}
    _attach_price_basis(
        snapshot, _PriceBasis(last_close=0.0, last_date=dt.date(2026, 10, 13), factor=2.0)
    )
    market = snapshot["market"]
    assert market["price_ratio"] == 2.0
    assert "last_price_raw" not in market


def test_price_basis_ignores_missing_market() -> None:
    _attach_price_basis(
        {}, _PriceBasis(last_close=1.0, last_date=dt.date(2026, 10, 13), factor=2.0)
    )


# ---------------------------------------------------------------------------
# 3. 开盘前 last=0.0 的 −100% 陷阱
# ---------------------------------------------------------------------------


def test_is_tradable_price_rejects_sentinels() -> None:
    assert _is_tradable_price(1274.89) is True
    assert _is_tradable_price(0.0) is False
    assert _is_tradable_price(0) is False
    assert _is_tradable_price(None) is False
    assert _is_tradable_price(-1.0) is False
    assert _is_tradable_price("abc") is False


def test_pct_from_returns_none_before_open_instead_of_minus_100() -> None:
    """开盘前新浪把 last 给成 0.0 —— 原实现会算出 −100%，看着像崩了。"""
    assert _pct_from(0.0, 1274.89) is None
    assert _pct_from(None, 1274.89) is None


def test_pct_from_computes_normal_change() -> None:
    assert _pct_from(110.0, 100.0) == 10.0
    assert _pct_from(90.0, 100.0) == -10.0


def test_pct_from_rejects_non_positive_prev_close() -> None:
    assert _pct_from(110.0, 0.0) is None
    assert _pct_from(110.0, None) is None


# ---------------------------------------------------------------------------
# 4. _attach_last_bar_stats：当日高/低/量/额
# ---------------------------------------------------------------------------


def _bar() -> dict:
    # 实测 600519 / 2026-10-13：高 8151.84 / 低 7950.0 / 量 26573 手 / 额 33.93 亿
    return {"high": 8151.84, "low": 7950.0, "volume": 26573.0, "quote_volume": 3393315572.0}


def test_last_bar_stats_with_factor_gives_two_price_bases() -> None:
    snapshot: dict = {"candles": [_bar()], "market": {}}
    _attach_last_bar_stats(
        snapshot, _PriceBasis(last_close=8151.84, last_date=dt.date(2026, 10, 13), factor=6.394)
    )
    market = snapshot["market"]
    assert market["last_high"] == 8151.84
    assert market["last_high_raw"] == pytest.approx(1274.92, abs=1e-2)
    assert market["last_low"] == 7950.0
    assert market["last_low_raw"] == pytest.approx(1243.35, abs=1e-2)
    # 量/额无所谓复权，直接给原始值（volume 的单位是**手**）
    assert market["last_volume_lots"] == 26573.0
    assert market["last_amount"] == 3393315572.0


def test_last_bar_stats_without_factor_skips_raw_prices() -> None:
    snapshot: dict = {"candles": [_bar()], "market": {}}
    _attach_last_bar_stats(snapshot, _PriceBasis(last_close=8151.84, last_date=None, factor=None))
    market = snapshot["market"]
    assert market["last_high"] == 8151.84
    assert "last_high_raw" not in market
    assert "last_low_raw" not in market


def test_last_bar_stats_no_candles_is_noop() -> None:
    snapshot: dict = {"candles": [], "market": {}}
    _attach_last_bar_stats(snapshot, _PriceBasis(last_close=1.0, last_date=None, factor=2.0))
    assert snapshot["market"] == {}


def test_last_bar_stats_tolerates_missing_fields() -> None:
    snapshot: dict = {"candles": [{"high": None, "low": None, "volume": None}], "market": {}}
    _attach_last_bar_stats(snapshot, _PriceBasis(last_close=1.0, last_date=None, factor=2.0))
    market = snapshot["market"]
    assert "last_high" not in market
    assert "last_low" not in market
    assert "last_volume_lots" not in market
    assert "last_amount" not in market


# ---------------------------------------------------------------------------
# 5. _attach_staleness：让「数据陈旧」这个态真的会亮（此前只读不写 = 死代码）
# ---------------------------------------------------------------------------

#: 2026-10-14 是周三；10-09 周五、10-12 周一、10-13 周二都是交易日。
_MS = 24 * 3600 * 1000


def _bar_ms(day: dt.date) -> int:
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC).timestamp() * 1000)


def _quality(candles: list[dict]) -> dict:
    return {"data_quality": {"stale": False}, "candles": candles}


@pytest.fixture()
def stale_env(monkeypatch: pytest.MonkeyPatch):
    """把「现在」和交易日历都换成可控的；返回 (设置 now, 记录查询, 设置 missing)。"""

    def setup(
        now: dt.datetime,
        *,
        conn: object | None = object(),
        missing: list[dt.date] | None = None,
        boom: bool = False,
    ):
        import cpt.adapters.a_share_local as local
        import cpt.application.a_share_snapshot as mod
        import cpt.domain.market_time as mt

        calls: list[tuple[dt.date, dt.date]] = []

        def fake_open_days_between(_conn: object, start: dt.date, end: dt.date) -> list[dt.date]:
            calls.append((start, end))
            if boom:
                raise RuntimeError("trade_calendar 不可用")
            return list(missing or [])

        monkeypatch.setattr(mt, "market_now", lambda: now)
        monkeypatch.setattr(local, "open_days_between", fake_open_days_between)
        monkeypatch.setattr(mod, "_active_client_conn", lambda _client: conn)
        return calls

    return setup


def test_staleness_quiet_intraday_when_last_bar_is_yesterday(stale_env) -> None:
    """盘中（北京 12:00）末根 bar = 昨天 → **不该**报陈旧，且一次库都不查。

    这是最要紧的反误报：A 股快照批 16:00 才跑，盘中库里本来就没有今天的 bar。
    """
    calls = stale_env(dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    snapshot = _quality([{"open_time": _bar_ms(dt.date(2026, 10, 13))}])
    _attach_staleness(snapshot, object())
    assert snapshot["data_quality"]["stale"] is False
    assert calls == []


def test_staleness_fires_intraday_when_last_bar_is_older(stale_env) -> None:
    """盘中末根 bar = 上周五 → 缺口是 10-12、10-13 两个交易日。"""
    calls = stale_env(
        dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        missing=[dt.date(2026, 10, 12), dt.date(2026, 10, 13)],
    )
    snapshot = _quality([{"open_time": _bar_ms(dt.date(2026, 10, 9))}])
    _attach_staleness(snapshot, object())
    quality = snapshot["data_quality"]
    assert quality["stale"] is True
    assert quality["stale_basis"] == "trade_calendar"
    assert quality["missing_trade_days"] == ["2026-10-12", "2026-10-13"]
    assert quality["stale_last_bar_date"] == "2026-10-09"
    # 盘中截止日取**昨天**，不是今天
    assert quality["stale_cutoff_date"] == "2026-10-13"
    assert calls == [(dt.date(2026, 10, 10), dt.date(2026, 10, 13))]


def test_staleness_after_close_counts_today(stale_env) -> None:
    """收盘后（北京 16:00）今天就该有 bar 了 → 截止日取今天。"""
    stale_env(
        dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        missing=[dt.date(2026, 10, 14)],
    )
    snapshot = _quality([{"open_time": _bar_ms(dt.date(2026, 10, 13))}])
    _attach_staleness(snapshot, object())
    assert snapshot["data_quality"]["stale"] is True
    assert snapshot["data_quality"]["stale_cutoff_date"] == "2026-10-14"


def test_staleness_after_close_with_todays_bar_is_quiet(stale_env) -> None:
    calls = stale_env(dt.datetime(2026, 10, 14, 16, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    snapshot = _quality([{"open_time": _bar_ms(dt.date(2026, 10, 14))}])
    _attach_staleness(snapshot, object())
    assert snapshot["data_quality"]["stale"] is False
    assert calls == []


def test_staleness_fails_open_when_calendar_query_raises(stale_env) -> None:
    """查库失败**绝不能**报陈旧 —— 判不了就不判。"""
    stale_env(
        dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        boom=True,
    )
    snapshot = _quality([{"open_time": _bar_ms(dt.date(2026, 10, 9))}])
    _attach_staleness(snapshot, object())
    assert snapshot["data_quality"]["stale"] is False
    assert "stale_basis" not in snapshot["data_quality"]


def test_staleness_fails_open_without_connection(stale_env) -> None:
    stale_env(
        dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
        conn=None,
    )
    snapshot = _quality([{"open_time": _bar_ms(dt.date(2026, 10, 9))}])
    _attach_staleness(snapshot, object())
    assert snapshot["data_quality"]["stale"] is False


@pytest.mark.parametrize(
    "snapshot",
    [
        {},
        {"data_quality": None, "candles": [{"open_time": _bar_ms(dt.date(2026, 10, 9))}]},
        {"data_quality": {"stale": False}, "candles": []},
        {"data_quality": {"stale": False}, "candles": [{"open_time": None}]},
        {"data_quality": {"stale": False}, "candles": [{}]},
    ],
)
def test_staleness_noop_on_incomplete_snapshot(snapshot: dict) -> None:
    _attach_staleness(snapshot, object())  # 不抛即可
    if isinstance(snapshot.get("data_quality"), dict):
        assert snapshot["data_quality"]["stale"] is False


def test_staleness_uses_beijing_time_not_host_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主是 UTC：北京 10-14 00:30（= UTC 10-13 16:30）算「盘中未收盘」还是「已收盘」？

    按北京时间 00:30 < 15:00 → 截止日取 10-13。若误用宿主 UTC 时间
    （16:30 >= 15:00）就会取 10-14，凭空多要一天 bar → **误报陈旧**。
    """
    import cpt.adapters.a_share_local as local
    import cpt.application.a_share_snapshot as mod
    import cpt.domain.market_time as mt

    calls: list[tuple[dt.date, dt.date]] = []
    monkeypatch.setattr(
        local,
        "open_days_between",
        lambda _c, s, e: (calls.append((s, e)), [])[1],
    )
    monkeypatch.setattr(mod, "_active_client_conn", lambda _c: object())
    # 关键：market_now() 给的是**北京时区**的 00:30
    monkeypatch.setattr(
        mt, "market_now", lambda: dt.datetime(2026, 10, 14, 0, 30, tzinfo=mt.MARKET_TZ)
    )
    snapshot = _quality([{"open_time": _bar_ms(dt.date(2026, 10, 13))}])
    _attach_staleness(snapshot, object())
    assert snapshot["data_quality"]["stale"] is False
    # 截止日 == 末根 bar 日 → 早退，一次库都不该查（查了说明 cutoff 被算成 10-14）
    assert calls == []
