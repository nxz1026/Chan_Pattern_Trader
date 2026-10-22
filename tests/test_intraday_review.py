"""R90：封板存活回顾（``cpt/application/intraday_review.py``）。

守的是三条口径：

1. 「开过板」= 某个 True 之后出现过 False，**不是**「没封着」。
2. 「最后封住」看当天最后一轮，不是「出现过 True」。
3. 没有样本时 ``hold_rate`` 是 ``None`` 不是 0 —— 0 会被读成「一只都没封住」。
"""

from __future__ import annotations

import datetime as dt

import pytest
from cpt.application.intraday_review import (
    by_code,
    session_of,
    session_summary,
    track_summary,
)

TZ8 = dt.timezone(dt.timedelta(hours=8))


def _row(code: str, minute: int, sealed: bool, lots: float | None = None) -> dict:
    return {
        "code": code,
        "at": dt.datetime(2026, 10, 22, 9, 30, tzinfo=TZ8) + dt.timedelta(minutes=minute),
        "sealed": sealed,
        "bid1_lots": lots,
        "limit_price": 5.23,
    }


# ── 分组与交易日 ──────────────────────────────────────────────────


def test_by_code_sorts_each_track_by_time() -> None:
    rows = [_row("600293", 4, True), _row("600293", 0, True), _row("000001", 2, False)]
    groups = by_code(rows)
    assert [r["at"].minute for r in groups["600293"]] == [30, 34]
    assert list(groups) == ["600293", "000001"]


def test_session_of_uses_beijing_date_not_utc() -> None:
    """UTC 16:10 = 北京次日 00:10；按 UTC 切会让跨日轮次归错交易日。"""
    late_utc = dt.datetime(2026, 10, 22, 16, 10, tzinfo=dt.UTC)
    assert session_of(late_utc) == dt.date(2026, 10, 23)


def test_session_of_keeps_afternoon_session_on_the_same_day() -> None:
    """北京 14:58（UTC 06:58）必须与北京 09:31 同一天。"""
    assert session_of(dt.datetime(2026, 10, 22, 6, 58, tzinfo=dt.UTC)) == dt.date(2026, 10, 22)
    assert session_of(dt.datetime(2026, 10, 22, 1, 31, tzinfo=dt.UTC)) == dt.date(2026, 10, 22)


# ── 单票轨迹 ──────────────────────────────────────────────────────


def test_never_sealed_track_is_not_an_open_board() -> None:
    """从没封过的票不算「开过板」—— 那是冲板失败，不是开板。"""
    out = track_summary([_row("000001", 0, False), _row("000001", 2, False)])
    assert out["first_sealed_at"] is None
    assert out["sealed_now"] is False
    assert out["opened_board"] is False
    assert out["open_board_count"] == 0


def test_sealed_throughout_has_no_open_board() -> None:
    out = track_summary([_row("600293", 0, True, 100.0), _row("600293", 2, True, 90.0)])
    assert out["sealed_now"] is True
    assert out["opened_board"] is False
    assert out["max_bid1_lots"] == 100.0


def test_open_board_is_counted_after_the_first_seal() -> None:
    track = [
        _row("600293", 0, True, 100.0),
        _row("600293", 2, False),
        _row("600293", 4, False),
        _row("600293", 6, True, 50.0),
    ]
    out = track_summary(track)
    assert out["opened_board"] is True
    assert out["open_board_count"] == 2
    assert out["sealed_now"] is True
    assert out["first_sealed_at"].minute == 30


def test_open_board_ignores_flaps_before_the_first_seal() -> None:
    """先 False 再 True —— 前面那些 False 不该被算成开板。"""
    track = [_row("600293", 0, False), _row("600293", 2, False), _row("600293", 4, True)]
    out = track_summary(track)
    assert out["opened_board"] is False
    assert out["open_board_count"] == 0


def test_sealed_now_reads_the_last_round_not_any_true() -> None:
    """中间封过、最后一轮开板 —— sealed_now 必须是 False。"""
    track = [_row("600293", 0, True), _row("600293", 2, False)]
    assert track_summary(track)["sealed_now"] is False


def test_empty_track_raises() -> None:
    with pytest.raises(ValueError):
        track_summary([])


# ── 全天汇总 ──────────────────────────────────────────────────────


def test_empty_session_reports_none_rate_not_zero() -> None:
    """没有样本时 ``hold_rate`` 必须是 None —— 0 会被读成「一只都没封住」。"""
    out = session_summary([])
    assert out["hold_rate"] is None
    assert out["codes"] == 0
    assert "还没有" in out["headline"]


def test_hold_rate_counts_only_codes_that_reached_the_board() -> None:
    rows = [
        # 封到底
        _row("600293", 0, True, 100.0),
        _row("600293", 2, True, 90.0),
        # 封过又开板
        _row("002249", 0, True, 10.0),
        _row("002249", 2, False),
        # 从没封过（不算分母）
        _row("000001", 0, False),
        _row("000001", 2, False),
    ]
    out = session_summary(rows)
    assert out["codes"] == 3
    assert out["sealed"] == 2
    assert out["sealed_all_the_way"] == 1
    assert out["opened"] == 1
    assert out["hold_rate"] == 0.5


def test_headline_says_it_in_plain_words() -> None:
    rows = [_row("600293", 0, True, 100.0), _row("600293", 2, True, 90.0)]
    out = session_summary(rows)
    assert "封过板" in out["headline"]
    assert "100%" in out["headline"]


def test_no_code_reached_the_board_has_its_own_headline() -> None:
    rows = [_row("000001", 0, False), _row("000001", 2, False)]
    out = session_summary(rows)
    assert out["sealed"] == 0
    assert out["hold_rate"] is None
    assert "一只都没封上" in out["headline"]


def test_rounds_reported_so_thin_samples_are_visible() -> None:
    """只出现 1 轮的票照样计入，但 rounds 要如实报 —— 别拿少样本的比例当结论。"""
    rows = [_row("600293", 0, True)]
    out = session_summary(rows)
    assert out["rounds"] == 1
    assert out["rounds_min"] == 1
    assert out["rounds_max"] == 1
