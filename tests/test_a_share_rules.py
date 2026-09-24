"""``cpt.domain.a_share_rules`` 测试。

不依赖 psycopg — mock conn 即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from cpt.domain.a_share_rules import (
    AShareDailyTag,
    apply_ashare_tags_to_bis,
    fetch_daily_tags,
    t_plus_one_purchase_allowed,
)
from cpt.domain.models import Bi


def date(y, m, d):
    from datetime import date as _d

    return _d(y, m, d)


def ms(d) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def bi(end_time_ms: int, *, source_ids=(), level: int = 0, direction: int = -1) -> Bi:
    start_time_ms = end_time_ms - 24 * 3600 * 1000
    return Bi(
        level=level,
        direction=direction,
        start_time=start_time_ms,
        end_time=end_time_ms,
        high=11.0,
        low=9.0,
        source_ids=source_ids,
    )


# --------------------------------------------------------------------------- #
# Mock DB
# --------------------------------------------------------------------------- #


@dataclass
class FakeCursor:
    rows: list[tuple]
    filter: dict = field(default_factory=dict)
    executed: list = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append((sql, params))
        code, start, end = params
        # rows 元素是 (code, date, ...extreme)；SQL 投影后去掉 code 列
        self.filter["out"] = [r[1:] for r in self.rows if r[0] == code and start <= r[1] <= end]

    def fetchall(self):
        return self.filter["out"]


@dataclass
class FakeConn:
    rows: list[tuple]

    def cursor(self):
        return FakeCursor(rows=self.rows)


# --------------------------------------------------------------------------- #
# fetch_daily_tags
# --------------------------------------------------------------------------- #


def test_fetch_daily_tags_returns_tags_in_range():
    rows = [
        ("000002", date(2026, 9, 22), True, False, False, False),
        ("000002", date(2026, 9, 23), False, False, False, False),
        ("000002", date(2026, 9, 24), True, True, True, False),  # 多标签同日
    ]
    conn = FakeConn(rows=rows)
    out = fetch_daily_tags(conn, "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))
    assert set(out.keys()) == {"2026-09-22", "2026-09-23", "2026-09-24"}
    assert out["2026-09-22"].is_limit_up is True
    assert out["2026-09-24"].is_bomb is True


def test_fetch_daily_tags_excludes_out_of_range():
    rows = [
        ("000002", date(2026, 9, 21), True, False, False, False),
        ("000002", date(2026, 9, 22), True, False, False, False),
        ("000002", date(2026, 9, 25), True, False, False, False),
    ]
    conn = FakeConn(rows=rows)
    out = fetch_daily_tags(conn, "000002", ms(date(2026, 9, 22)), ms(date(2026, 9, 24)))
    assert "2026-09-22" in out
    assert "2026-09-21" not in out
    assert "2026-09-25" not in out


def test_fetch_daily_tags_with_exchange_suffix():
    rows = [("000002", date(2026, 9, 22), True, False, False, False)]
    conn = FakeConn(rows=rows)
    out = fetch_daily_tags(conn, "000002.SZ", ms(date(2026, 9, 22)), ms(date(2026, 9, 22)))
    assert "2026-09-22" in out


# --------------------------------------------------------------------------- #
# apply_ashare_tags_to_bis
# --------------------------------------------------------------------------- #


def test_apply_ashare_tags_to_bis_adds_limit_up_id_to_bar():
    tag = AShareDailyTag(
        code="000002",
        trade_date="2026-09-22",
        is_limit_up=True,
        is_limit_down=False,
        is_bomb=False,
        is_one_word=False,
    )
    b = bi(ms(date(2026, 9, 22)) + 24 * 3600 * 1000 - 1)
    out = apply_ashare_tags_to_bis([b], {"2026-09-22": tag})
    assert len(out) == 1
    assert "ashare:is_limit_up:2026-09-22" in out[0].source_ids


def test_apply_ashare_tags_to_bis_adds_all_extreme_labels():
    tag = AShareDailyTag(
        code="000002",
        trade_date="2026-09-22",
        is_limit_up=True,
        is_limit_down=True,
        is_bomb=True,
        is_one_word=True,
    )
    b = bi(ms(date(2026, 9, 22)) + 24 * 3600 * 1000 - 1)
    out = apply_ashare_tags_to_bis([b], {"2026-09-22": tag})
    ids = set(out[0].source_ids)
    assert "ashare:is_limit_up:2026-09-22" in ids
    assert "ashare:is_limit_down:2026-09-22" in ids
    assert "ashare:is_bomb:2026-09-22" in ids
    assert "ashare:is_one_word:2026-09-22" in ids


def test_apply_ashare_tags_to_bis_does_not_mutate_input_bar():
    tag = AShareDailyTag(
        code="000002",
        trade_date="2026-09-22",
        is_limit_up=True,
        is_limit_down=False,
        is_bomb=False,
        is_one_word=False,
    )
    b = bi(ms(date(2026, 9, 22)) + 24 * 3600 * 1000 - 1, source_ids=("orig:1",))
    out = apply_ashare_tags_to_bis([b], {"2026-09-22": tag})
    # 源对象 source_ids 不变
    assert b.source_ids == ("orig:1",)
    # 新对象保留原 id + 新增
    assert "orig:1" in out[0].source_ids
    assert "ashare:is_limit_up:2026-09-22" in out[0].source_ids


def test_apply_ashare_tags_to_bis_normal_day_unchanged():
    """非极端日（has_any_extreme == False）— 不加 id。"""
    tag = AShareDailyTag(
        code="000002",
        trade_date="2026-09-22",
        is_limit_up=False,
        is_limit_down=False,
        is_bomb=False,
        is_one_word=False,
    )
    b = bi(ms(date(2026, 9, 22)) + 24 * 3600 * 1000 - 1)
    out = apply_ashare_tags_to_bis([b], {"2026-09-22": tag})
    assert out[0].source_ids == ()


def test_apply_ashare_tags_to_bis_deduplicates_existing_ids():
    """如果原有 source_ids 已含同标签，新对象不会重复。"""
    tag = AShareDailyTag(
        code="000002",
        trade_date="2026-09-22",
        is_limit_up=True,
        is_limit_down=False,
        is_bomb=False,
        is_one_word=False,
    )
    b = bi(
        ms(date(2026, 9, 22)) + 24 * 3600 * 1000 - 1, source_ids=("ashare:is_limit_up:2026-09-22",)
    )
    out = apply_ashare_tags_to_bis([b], {"2026-09-22": tag})
    ids = list(out[0].source_ids)
    assert ids.count("ashare:is_limit_up:2026-09-22") == 1


def test_apply_ashare_tags_to_bis_skips_bars_without_tag():
    """日期在 tags dict 中找不到 → 不加 id（DB 缺失，视为非极端日）。"""
    b = bi(ms(date(2026, 9, 22)) + 24 * 3600 * 1000 - 1)
    out = apply_ashare_tags_to_bis([b], {})
    assert out[0].source_ids == ()


# --------------------------------------------------------------------------- #
# t_plus_one
# --------------------------------------------------------------------------- #


def test_t_plus_one_returns_true_when_no_previous_close():
    """无前置日 → 视为"首次建仓"，允许。"""
    assert t_plus_one_purchase_allowed(None) is True


def test_t_plus_one_returns_true_after_close():
    """前一日已收盘 → 日历允许（持仓层面由仓位模块处理）。"""
    assert t_plus_one_purchase_allowed("2026-09-23") is True
