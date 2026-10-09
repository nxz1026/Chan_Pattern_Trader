"""``cpt.adapters.a_share_local`` 测试。

用 mock DB 测全部分支，不依赖 psycopg — 保持 ``dependencies = []`` 干净。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from cpt.adapters.a_share_local import (
    AShareFetchResult,
    AShareLocalClient,
    AShareLocalError,
    AShareNoDataError,
    AShareNoFactorError,
    ASharePlaceholderRowsError,
)
from cpt.domain.models import CanonicalBar

# --------------------------------------------------------------------------- #
# mock DB
# --------------------------------------------------------------------------- #


@dataclass
class FakeCursor:
    rows_bars: list[tuple]
    rows_factors: list[tuple]
    filter: dict = field(default_factory=dict)
    executed: list = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append((sql, params))
        # 简化路由：根据 SQL 关键字
        s = sql.strip().lower()
        if s.startswith("select date, open"):
            code, start, end = params
            self.filter["bars"] = [
                r[1:] for r in self.rows_bars if r[0] == code and start <= r[1] <= end
            ]
        elif s.startswith("select trade_date, hfq_factor"):
            code, start, end = params
            self.filter["factors"] = [r for r in self.rows_factors if start <= r[0] <= end]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        s = self.executed[-1][0].strip().lower()
        if s.startswith("select date, open"):
            return self.filter["bars"]
        if s.startswith("select trade_date, hfq_factor"):
            return self.filter["factors"]
        raise AssertionError(f"Unexpected fetchall on: {s}")


@dataclass
class FakeConn:
    bars: list[tuple]
    factors: list[tuple]

    def cursor(self):
        return FakeCursor(rows_bars=self.bars, rows_factors=self.factors)

    def close(self):
        pass


def date(y, m, d):
    from datetime import date as _d

    return _d(y, m, d)


def ms(d) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def full_db():
    """000002 三天完整 OHLC + 因子。"""
    bars = [
        ("000002", date(2026, 9, 22), 3.0, 3.1, 2.9, 3.05, 100.0, 200.0),
        ("000002", date(2026, 9, 23), 3.1, 3.2, 3.0, 3.15, 150.0, 300.0),
        ("000002", date(2026, 9, 24), 3.2, 3.3, 3.1, 3.25, 200.0, 400.0),
    ]
    factors = [
        (date(2026, 9, 22), 10.0),
        (date(2026, 9, 23), 10.5),
        (date(2026, 9, 24), 11.0),
    ]
    conn = FakeConn(bars=list(bars), factors=list(factors))
    client = AShareLocalClient(conn_factory=lambda: conn)
    return client, conn


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #


def test_returns_hfq_applied_canonical_bars(full_db):
    client, _ = full_db
    result = client.fetch_validated_klines("000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))
    assert isinstance(result, AShareFetchResult)
    assert len(result.bars) == 3
    assert result.skipped_no_factor == ()

    bar = result.bars[0]
    # 原始 close=3.05 × factor=10.0 = 30.5
    assert isinstance(bar, CanonicalBar)
    assert bar.close == pytest.approx(30.5)
    assert bar.open == pytest.approx(30.0)
    assert bar.high == pytest.approx(31.0)
    assert bar.low == pytest.approx(29.0)
    # 成交量保持不复权
    assert bar.volume == pytest.approx(100.0)
    assert bar.quote_volume == pytest.approx(200.0)
    # 时间字段：open_time=当日 00:00:00 UTC；close_time=当日 23:59:59.999 UTC
    assert bar.open_time == ms(date(2026, 9, 22))
    assert bar.close_time == bar.open_time + 24 * 3600 * 1000 - 1
    assert bar.is_closed is True


def test_factor_missing_day_is_skipped_not_failed(full_db):
    client, _ = full_db
    # 删掉 9-23 的因子，模拟缺口
    client._get_conn().factors[:] = [
        f for f in client._get_conn().factors if f[0] != date(2026, 9, 23)
    ]

    result = client.fetch_validated_klines("000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))
    assert len(result.bars) == 2
    assert result.skipped_no_factor == ("2026-09-23",)
    # 留下的两根仍是 9-22 和 9-24
    assert [b.open_time for b in result.bars] == [ms(date(2026, 9, 22)), ms(date(2026, 9, 24))]


def test_all_dates_missing_factor_raises_error(full_db):
    client, _ = full_db
    client._get_conn().factors.clear()
    with pytest.raises(AShareLocalError, match="所有日期都缺因子"):
        client.fetch_validated_klines("000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))


def test_no_bars_in_range_raises_error(full_db):
    client, _ = full_db
    with pytest.raises(AShareLocalError, match="public.daily_bar 无数据"):
        client.fetch_validated_klines("000002", ms(date(2024, 1, 1)), ms(date(2024, 1, 5)))


def test_code_with_exchange_suffix_is_normalized(full_db):
    client, _ = full_db
    result = client.fetch_validated_klines(
        "000002.SZ", ms(date(2026, 9, 22)), ms(date(2026, 9, 22))
    )
    assert len(result.bars) == 1
    assert result.bars[0].close == pytest.approx(30.5)


def test_db_error_is_wrapped_in_ashare_error():
    class BrokenConn:
        def cursor(self):
            raise RuntimeError("connection lost")

    client = AShareLocalClient(conn_factory=lambda: BrokenConn())
    with pytest.raises(AShareLocalError, match="DB 读取失败"):
        client.fetch_validated_klines("000002", 0, 1)


def test_close_is_idempotent_and_safe(full_db):
    client, _ = full_db
    client.close()
    client.close()  # 第二次不能抛错
    assert client._conn is None


def test_fetch_validated_bars_returns_barlike_list(full_db):
    client, _ = full_db
    bars = client.fetch_validated_bars("000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))
    assert len(bars) == 3
    # 验证确实是 BarLike（duck typing：has open_time/open/high/low/close）
    for b in bars:
        for attr in ("open_time", "open", "high", "low", "close"):
            assert hasattr(b, attr)


# --------------------------------------------------------------------------- #
# 占位行守卫（R52）
#
# 背景：上游在 2026-09-28~09-30 一次批量写出 35 行 O/H/L=0、vol=amt=0 的废行
# （18 只票），其中 **18 行连 close 都是 0**。危害分两档：
#   close≠0 → validate_ashare_bars 抛 DataValidationError → 整只票降级；
#   close=0 → 校验器 0<=0<=0 放行 → 零价 K 线进结构计算 → 假分型/假笔/假中枢，
#             **全程零报错**。第二档更毒，所以下面的用例必须同时钉住两档。
# --------------------------------------------------------------------------- #


@pytest.fixture
def db_with_placeholders():
    """三天行情：9-22 正常、9-23 占位（close 填前收盘价）、9-24 完全空占位（close=0）。

    第三行是**校验器本来会放行**的那一档 —— 它是这次修复真正的判据所在。
    """
    bars = [
        ("000002", date(2026, 9, 22), 3.0, 3.1, 2.9, 3.05, 100.0, 200.0),
        # 占位行 A：O/H/L 全 0，close 填了前收盘价（上游惯例）
        ("000002", date(2026, 9, 23), 0.0, 0.0, 0.0, 3.05, 0.0, 0.0),
        # 占位行 B：O/H/L/close 全 0 —— validate_ashare_bars 会放行
        ("000002", date(2026, 9, 24), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    ]
    factors = [
        (date(2026, 9, 22), 10.0),
        (date(2026, 9, 23), 10.5),
        (date(2026, 9, 24), 11.0),
    ]
    conn = FakeConn(bars=list(bars), factors=list(factors))
    return AShareLocalClient(conn_factory=lambda: conn)


def test_placeholder_rows_are_dropped_from_bars(db_with_placeholders):
    """两档占位行都不得进入 ``bars`` —— 少了 9-24 那根就是关键。"""
    result = db_with_placeholders.fetch_validated_klines(
        "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24))
    )
    assert len(result.bars) == 1
    assert [b.open_time for b in result.bars] == [ms(date(2026, 9, 22))]
    assert result.skipped_placeholder == ("2026-09-23", "2026-09-24")
    # 不能与「缺因子」混记：两者对上游的指控完全不同
    assert result.skipped_no_factor == ()


def test_zero_price_bar_would_have_passed_the_validator(db_with_placeholders):
    """钉住「为什么必须在 adapters 拦」—— 这是本轮最容易被后人「优化掉」的一处。

    断言的是**校验器的真实行为**，不是我们的实现：如果哪天有人放宽了
    ``validate_ashare_bars``，这个用例会先红，提醒他当初为什么加这道守卫。
    """
    from cpt.adapters.validators import validate_ashare_bars
    from cpt.domain.models import CanonicalBar

    zero_bar = CanonicalBar(
        open_time=ms(date(2026, 9, 24)),
        open=0.0,
        high=0.0,
        low=0.0,
        close=0.0,
        volume=0.0,
        close_time=ms(date(2026, 9, 24)) + 24 * 3600 * 1000 - 1,
        quote_volume=0.0,
        trade_count=0,
        taker_buy_base_volume=0.0,
        taker_buy_quote_volume=0.0,
        is_closed=True,
    )
    # 校验器**放行**了 —— 0 <= 0 <= 0 成立。这就是必须在 adapters 层拦的原因。
    validated = validate_ashare_bars([zero_bar], interval_ms=24 * 3600 * 1000)
    assert len(validated) == 1
    assert validated[0].close == 0.0


def test_placeholder_rows_do_not_break_partial_series(db_with_placeholders):
    """丢弃占位行后，剩下的序列必须仍能通过校验（不能因为少一天就整票降级）。"""
    from cpt.adapters.validators import validate_ashare_bars

    result = db_with_placeholders.fetch_validated_klines(
        "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24))
    )
    validated = validate_ashare_bars(list(result.bars), interval_ms=24 * 3600 * 1000)
    assert len(validated) == 1


def test_all_placeholder_rows_raise_distinct_error(db_with_placeholders):
    """整段都是占位行 ⇒ 必须报**专属**错误，不能混进 no_factor / no_data。

    混掉的后果是排查被指到错误的方向：查因子表是白查，真凶是采集。
    """
    conn = db_with_placeholders._get_conn()
    conn.bars[:] = [
        ("000002", date(2026, 9, 22), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        ("000002", date(2026, 9, 23), 0.0, 0.0, 0.0, 3.05, 0.0, 0.0),
    ]
    with pytest.raises(ASharePlaceholderRowsError, match="全是占位行"):
        db_with_placeholders.fetch_validated_klines(
            "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24))
        )


def test_placeholder_error_is_not_a_missing_factor_error(db_with_placeholders):
    """异常类型必须与 no_factor / no_data 分开 —— 靠类型，不靠消息串。"""
    conn = db_with_placeholders._get_conn()
    conn.bars[:] = [("000002", date(2026, 9, 22), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)]
    with pytest.raises(ASharePlaceholderRowsError) as exc_info:
        db_with_placeholders.fetch_validated_klines(
            "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24))
        )
    assert not isinstance(exc_info.value, AShareNoFactorError)
    assert not isinstance(exc_info.value, AShareNoDataError)


def test_all_rows_missing_factor_still_raises_no_factor_error(full_db):
    """守卫不能误伤原有的缺因子路径（占位行为空、但因子也缺 ⇒ 报缺因子）。"""
    client, _ = full_db
    client._get_conn().factors.clear()
    with pytest.raises(AShareNoFactorError):
        client.fetch_validated_klines("000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))


def test_zero_open_alone_is_not_a_placeholder(db_with_placeholders):
    """判据是 O/H/L **同时**为 0，不是「open=0」。

    一字跌停的真实 bar 就是 open=high=low=close=涨停价（都非 0），
    但仍存在 open=0 而 high>0 的极端情况（如集合竞价 0 元开盘）——
    那类行是**合法数据**，误丢会静默造成序列缺口。
    """
    conn = db_with_placeholders._get_conn()
    conn.bars[:] = [
        ("000002", date(2026, 9, 22), 0.0, 3.1, 2.9, 3.05, 100.0, 200.0),
    ]
    result = db_with_placeholders.fetch_validated_klines(
        "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24))
    )
    assert len(result.bars) == 1
    assert result.skipped_placeholder == ()


def test_fetch_result_defaults_placeholder_to_empty(full_db):
    """默认必须是空元组 —— 测试里大量 duck-type 假结果只给两个字段。"""
    client, _ = full_db
    result = client.fetch_validated_klines("000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))
    assert result.skipped_placeholder == ()
    # 手工构造时省略该字段也不该炸
    assert AShareFetchResult(bars=(), skipped_no_factor=()).skipped_placeholder == ()


# --------------------------------------------------------------------------- #
# Wind 代码转换（接线前全仓 0 代码引用，只在两处注释里被点名）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("600519", "600519.SH"),
        ("601398", "601398.SH"),
        ("900901", "900901.SH"),  # 沪 B
        ("510300", "510300.SH"),  # 沪 ETF
        ("688981", "688981.SH"),  # 科创板（`6` 开头，仍走沪市）
        ("000002", "000002.SZ"),
        ("300750", "300750.SZ"),  # 创业板
        ("200002", "200002.SZ"),  # 深 B
        ("159915", "159915.SZ"),  # 深 ETF
        # 北交所各段：`92` 必须先于 `9` 判，43/83/87/88 也必须认
        ("920025", "920025.BJ"),
        ("430047", "430047.BJ"),
        ("830799", "830799.BJ"),
        ("870204", "870204.BJ"),
        ("889999", "889999.BJ"),
        # 已带后缀：规范化大小写后原样返回
        ("600519.sh", "600519.SH"),
        ("000002.BJ", "000002.BJ"),
    ],
)
def test_to_wind_code_maps_both_exchanges(raw: str, expected: str) -> None:
    assert AShareLocalClient._to_wind_code(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "abc",
        "60051",  # 5 位
        "6005190",  # 7 位
        "600519.XX",  # 未知后缀
        "700000",  # 首位 7 在 A 股不存在
        "６００５１９",  # 全角数字（str.isdigit() 会放过）
        "600 519",
    ],
)
def test_to_wind_code_rejects_invalid_instead_of_guessing(bad: str) -> None:
    """非法输入必须抛 ``ValueError``。

    接线前这里一律兜底成 ``"<原样>.SZ"``，等于拿一个不存在的 Wind 代码去查库，
    报回来的是"查无此码"—— 把"输入不合法"伪装成"Wind 没有这只票"。
    """
    with pytest.raises(ValueError):
        AShareLocalClient._to_wind_code(bad)


# --------------------------------------------------------------------------- #
# NULL 脏行守卫（R59 审计 M8）
#
# 改前只有 volume/quote_volume 做 ``is not None`` 兜底；O/H/L/close 任一为 NULL
# 就 ``float(None)`` 抛 TypeError —— 不在 ``AShareLocalError`` 体系内，整只票的
# K 线全拿不到。R52 的上游占位行是「全 0」，NULL 是同一类「采集写出废行」，
# 按同一条降级纪律处理：跳过该行 + 可观测痕迹，绝不牵连整票。
# --------------------------------------------------------------------------- #


def test_null_ohlc_rows_are_skipped_instead_of_type_error(db_with_placeholders):
    """O/H/L/close 遇 NULL ⇒ 跳过这一天，其余照常返回（改前整票 TypeError）。"""
    conn = db_with_placeholders._get_conn()
    conn.bars[:] = [
        ("000002", date(2026, 9, 22), 3.0, 3.1, 2.9, 3.05, 100.0, 200.0),
        ("000002", date(2026, 9, 23), None, 3.1, 2.9, 3.05, 100.0, 200.0),  # open NULL
        ("000002", date(2026, 9, 24), 3.0, 3.1, 2.9, None, 100.0, 200.0),  # close NULL
    ]
    result = db_with_placeholders.fetch_validated_klines(
        "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24))
    )
    assert len(result.bars) == 1
    assert [b.open_time for b in result.bars] == [ms(date(2026, 9, 22))]
    # 与占位行同列：两者都是「采集写出废行」，对上游的指控一致
    assert result.skipped_placeholder == ("2026-09-23", "2026-09-24")
    assert result.skipped_no_factor == ()


def test_null_ohlc_rows_leave_a_warning_trace(db_with_placeholders, caplog):
    """跳过 NULL 脏行必须留痕迹 —— 否则序列凭空缺一天，排查时无据可查。"""
    conn = db_with_placeholders._get_conn()
    conn.bars[:] = [
        ("000002", date(2026, 9, 22), 3.0, 3.1, 2.9, 3.05, 100.0, 200.0),
        ("000002", date(2026, 9, 23), None, None, None, None, 0.0, 0.0),
    ]
    with caplog.at_level(logging.WARNING, logger="cpt.adapters.a_share_local"):
        db_with_placeholders.fetch_validated_klines(
            "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24))
        )
    assert any("NULL" in record.getMessage() for record in caplog.records)


def test_all_null_rows_raise_placeholder_error_with_null_reason(db_with_placeholders):
    """整段都是 NULL ⇒ 仍是 ``ASharePlaceholderRowsError``（不是 TypeError），消息点明 NULL。"""
    conn = db_with_placeholders._get_conn()
    conn.bars[:] = [
        ("000002", date(2026, 9, 22), None, None, None, None, 0.0, 0.0),
        ("000002", date(2026, 9, 23), None, None, None, None, 0.0, 0.0),
    ]
    with pytest.raises(ASharePlaceholderRowsError, match="NULL"):
        db_with_placeholders.fetch_validated_klines(
            "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24))
        )


def test_null_volume_and_amount_keep_existing_zero_fallback(db_with_placeholders):
    """量/额为 NULL 的既有兜底不能被新守卫误伤：照旧填 0，bar 保留。"""
    conn = db_with_placeholders._get_conn()
    conn.bars[:] = [("000002", date(2026, 9, 22), 3.0, 3.1, 2.9, 3.05, None, None)]
    result = db_with_placeholders.fetch_validated_klines(
        "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24))
    )
    assert len(result.bars) == 1
    assert result.bars[0].volume == 0.0
    assert result.bars[0].quote_volume == 0.0
