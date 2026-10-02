"""``cpt.domain.a_share_rules`` 测试。

不依赖 psycopg — mock conn 即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from cpt.adapters.a_share_local import (
    check_t_plus_one_calendar,
    fetch_daily_tags,
)
from cpt.application.a_share_snapshot import build_ashare_snapshot
from cpt.domain.a_share_rules import (
    AShareDailyTag,
    apply_ashare_tags_to_bis,
    t_plus_one_purchase_allowed,
)
from cpt.domain.models import Bi

from tests.test_web_a_share import _FakeClient, _zigzag_bars

#: ``build_ashare_snapshot`` 内部每次都会直连东财拿实时价（见 conftest 说明）。
#: 下面几条基线/对照用例要从生产构造函数进，所以必须钉死外网。
pytestmark = pytest.mark.usefixtures("stub_realtime_quote")


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


# --------------------------------------------------------------------------- #
# T+1 日历查询（R21 接线）
# --------------------------------------------------------------------------- #


@dataclass
class _CalendarCursor:
    """模拟 ``public.trade_calendar`` 查询的游标。"""

    today_open: bool | None = True
    next_date: str | None = None
    explode: bool = False
    executed: list = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql: str, params: tuple) -> None:
        self.executed.append((sql, params))
        if self.explode:
            raise RuntimeError("DB 不可达")

    def fetchone(self):
        # 第一查：SELECT is_open WHERE date = today
        if "is_open FROM" in self.executed[-1][0]:
            if self.today_open is None:
                return None
            return (self.today_open,)
        # 第二查：SELECT date::text ... WHERE is_open AND date > today
        return (self.next_date,) if self.next_date else None


@dataclass
class _CalendarConn:
    cursor_obj: _CalendarCursor

    def cursor(self):
        return self.cursor_obj


@dataclass
class _CalendarClient:
    """注入到 ``check_t_plus_one_calendar`` 的假客户端。"""

    conn: _CalendarConn

    def _get_conn(self):
        return self.conn


def _calendar_client(*, today_open=True, next_date=None, explode=False):
    return _CalendarClient(
        _CalendarConn(_CalendarCursor(today_open=today_open, next_date=next_date, explode=explode))
    )


def test_check_t_plus_one_calendar_trade_day():
    """今日开市 → available=True。"""
    client = _calendar_client(today_open=True)
    result = check_t_plus_one_calendar(client)
    assert result["available"] is True
    assert result["reason"] == "trade_day"
    assert result["today"] is not None
    assert result["next_trade_date"] is None


def test_check_t_plus_one_calendar_not_trade_day():
    """今日休市（周末/节假日）→ available=False，带下一开市日。"""
    client = _calendar_client(today_open=False, next_date="2026-10-08")
    result = check_t_plus_one_calendar(client)
    assert result["available"] is False
    assert result["reason"] == "not_a_trade_day"
    assert result["next_trade_date"] == "2026-10-08"


def test_check_t_plus_one_calendar_unknown_date():
    """日历表无今日记录 → available=False。"""
    client = _calendar_client(today_open=None)
    result = check_t_plus_one_calendar(client)
    assert result["available"] is False
    assert result["reason"] == "calendar_unknown"


def test_check_t_plus_one_calendar_db_error():
    """DB 报错 → 降级 available=False，不抛异常。"""
    client = _calendar_client(explode=True)
    result = check_t_plus_one_calendar(client)
    assert result["available"] is False
    assert result["reason"] == "calendar_check_failed"


# --------------------------------------------------------------------------- #
# 接线（R19）：本模块从「生产零导入」接进 A 股主看板
# --------------------------------------------------------------------------- #
#
# 上面那些用例是**自证式**的——它们直接调本模块，证明"函数本身没坏"，
# 证明不了"生产路径真的会走它"。R19 起补下面这组：入口是生产构造函数
# ``cpt.application.a_share_snapshot.build_ashare_snapshot``，断言的是
# **快照里真的带上了标签**。改接线代码让标签不落地，这组会红；只改
# 本模块的函数体而忘了接线，这组**不会**红（那正是原来漏检的形态）。

_CODE = "600519"


class _TagClient(_FakeClient):
    """在 ``_FakeClient`` 之上多实现 ``fetch_daily_tags``（鸭子类型探针要的那个方法）。"""

    def __init__(self, bars, tags, *, explode: bool = False) -> None:
        super().__init__(bars)
        self._tags = tags
        self._explode = explode
        self.tag_calls: list[tuple[str, int, int]] = []

    def fetch_daily_tags(self, code: str, start_ms: int, end_ms: int):
        self.tag_calls.append((code, start_ms, end_ms))
        if self._explode:
            raise RuntimeError("derived_bar 不可达")
        return self._tags


def _bi_end_dates(snap) -> list[str]:
    return [
        datetime.fromtimestamp(b["end_time"] / 1000, tz=UTC).date().isoformat()
        for b in snap["overlays"]["bis"]
    ]


def test_production_entry_tags_bis_with_extreme_days():
    """生产入口产出的快照里，涨停日的笔必须带 ``ashare:`` 合成 id。"""
    baseline = build_ashare_snapshot(_CODE, client=_FakeClient(_zigzag_bars(n=120)))
    target = _bi_end_dates(baseline)[-1]
    tag = AShareDailyTag(
        code=_CODE,
        trade_date=target,
        is_limit_up=True,
        is_limit_down=False,
        is_bomb=False,
        is_one_word=False,
    )
    snap = build_ashare_snapshot(_CODE, client=_TagClient(_zigzag_bars(n=120), {target: tag}))
    tagged = [
        b for b in snap["overlays"]["bis"] if any(i.startswith("ashare:") for i in b["source_ids"])
    ]
    assert len(tagged) == 1, "只有末日那根笔应被打标签"
    assert f"ashare:is_limit_up:{target}" in tagged[0]["source_ids"]


def _limit_up_tag(day: str) -> AShareDailyTag:
    return AShareDailyTag(
        code=_CODE,
        trade_date=day,
        is_limit_up=True,
        is_limit_down=False,
        is_bomb=False,
        is_one_word=False,
    )


def _quiet_tag(day: str) -> AShareDailyTag:
    return AShareDailyTag(
        code=_CODE,
        trade_date=day,
        is_limit_up=False,
        is_limit_down=False,
        is_bomb=False,
        is_one_word=False,
    )


def test_tags_do_not_change_structure():
    """标签只注入 ``source_ids``，**不碰**笔的数值与时点。

    与 M4 fixture 迁移用的是同一条判据：接线类改动必须先证明"结构不变"，
    否则一旦把标签写进 ``Bi`` 的数值字段，缠论结构会静默变形。
    """
    baseline = build_ashare_snapshot(_CODE, client=_FakeClient(_zigzag_bars(n=120)))
    dates = _bi_end_dates(baseline)
    tags = {d: _limit_up_tag(d) for d in dates}
    tagged = build_ashare_snapshot(_CODE, client=_TagClient(_zigzag_bars(n=120), tags))

    def shape(snap):
        return [
            {k: v for k, v in b.items() if k not in {"source_ids"}} for b in snap["overlays"]["bis"]
        ]

    assert shape(tagged) == shape(baseline), "打标签改变了笔的结构 → 标签被写进了数值字段"
    assert tagged["overlays"]["fractals"] == baseline["overlays"]["fractals"]
    assert tagged["overlays"]["zhongshus"] == baseline["overlays"]["zhongshus"]


def test_ashare_tags_audit_block_reports_coverage():
    """审计块要能区分「查了但没有极端日」和「压根没查」。"""
    baseline = build_ashare_snapshot(_CODE, client=_FakeClient(_zigzag_bars(n=120)))
    n_bis = len(baseline["overlays"]["bis"])
    assert n_bis > 0, "靶子没出笔，下面的断言就是同义反复"

    # ① 没实现 fetch_daily_tags 的假客户端 → available False
    assert baseline["data_quality"]["ashare_tags"] == {
        "available": False,
        "reason": "client_unsupported",
        "total_bis": n_bis,
    }

    # ② 实现了但区间内无极端日 → available True / tagged_bis 0
    dates = _bi_end_dates(baseline)
    quiet = build_ashare_snapshot(
        _CODE,
        client=_TagClient(_zigzag_bars(n=120), {d: _quiet_tag(d) for d in dates}),
    )
    assert quiet["data_quality"]["ashare_tags"] == {
        "available": True,
        "tagged_bis": 0,
        "total_bis": n_bis,
        "source": "public.derived_bar",
    }


def test_tag_fetch_failure_does_not_kill_snapshot():
    """标签是**纯展示增强**，DB 挂了只能少标签，绝不能把整个快照搞成 degraded。"""
    snap = build_ashare_snapshot(_CODE, client=_TagClient(_zigzag_bars(n=120), {}, explode=True))
    assert snap["market"]["bar_count"] == 120
    assert snap["data_quality"]["ashare_tags"] == {
        "available": False,
        "reason": "tag_fetch_failed",
        "total_bis": len(snap["overlays"]["bis"]),
    }
    assert all(
        not any(i.startswith("ashare:") for i in b["source_ids"]) for b in snap["overlays"]["bis"]
    )


def test_tag_query_window_matches_kline_window():
    """标签查询区间必须与 K 线查询区间一致 —— 区间错位会让末日笔查不到标签。"""
    client = _TagClient(_zigzag_bars(n=120), {})
    build_ashare_snapshot(_CODE, width_k=45, client=client)
    (_, tag_start, tag_end) = client.tag_calls[0]
    (_, bar_start, bar_end) = client.calls[0]
    assert (tag_start, tag_end) == (bar_start, bar_end)
