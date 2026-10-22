"""封板存活回顾（R90）：把一天的 ``intraday_seal_snapshot`` 翻成人话。

## 为什么需要这一层

ADR 0003 §C3 的结论是「封住率 ≥78.6% 才盈亏平衡、实测 70.1%」，那个 70.1%
来自一次性回测。真实每日样本只能从盘中快照读出来：某只票今天冲到板、封了多久、
开了几次、最后还封着没有。

本模块**只读快照算结论，不读网络、不判涨停** —— 判据（``seal_strength``）在
采集端只有一个实现，这里只做时间序列上的统计。

## 三条口径

1. **「开过板」= 某一轮 ``sealed=True`` 之后出现过 ``sealed=False``**，
   不是「没封着」—— 冲板失败从没封过的不算开板。
2. **「最后封住」看当天最后一轮**，不是看「有没有出现过 True」。
3. 轮次不满（票今天只出现 1 轮）照样计入，但 ``rounds`` 会如实报出来 ——
   样本少的时候别拿比例当结论。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any, Final

_MARKET_TZ: Final[dt.tzinfo] = dt.timezone(dt.timedelta(hours=8))


def session_of(at: dt.datetime) -> dt.date:
    """把快照时间归到「哪个交易日」（北京时间）。

    快照时间存的是 timestamptz；北京 15:00 之后的轮次（理论上不该有）仍然
    归当天 —— 按北京日期切，比按 UTC 切少一天错位。
    """
    local = at.astimezone(_MARKET_TZ)
    return local.date()


def by_code(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按票分组，每组按时间升序。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row["code"]), []).append(row)
    for track in out.values():
        track.sort(key=lambda r: r["at"])
    return out


def track_summary(track: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """一只票一天的轨迹小结。

    Returns:
        ``{"code","rounds","first_sealed_at","last_at","sealed_now",
           "opened_board","open_board_count","max_bid1_lots"}``
    """
    if not track:
        raise ValueError("track_summary 需要至少一轮")
    sealed_flags = [bool(r["sealed"]) for r in track]
    first_true = next((i for i, s in enumerate(sealed_flags) if s), None)
    opens = 0
    if first_true is not None:
        for flag in sealed_flags[first_true + 1 :]:
            if not flag:
                opens += 1
    lots = [r["bid1_lots"] for r in track if r.get("bid1_lots") is not None]
    return {
        "code": str(track[0]["code"]),
        "rounds": len(track),
        "first_sealed_at": track[first_true]["at"] if first_true is not None else None,
        "last_at": track[-1]["at"],
        "sealed_now": bool(track[-1]["sealed"]),
        "opened_board": opens > 0,
        "open_board_count": opens,
        "max_bid1_lots": max(lots) if lots else None,
    }


def session_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """一天的封板存活汇总 + 一句人话。

    Returns:
        ``{"codes","rounds","sealed","opened","sealed_all_the_way","hold_rate",
           "rounds_min","rounds_max","headline"}``

    ``hold_rate`` = 「最后一轮还封着」÷ 「今天到过板的票」。**没有样本时是
    ``None`` 不是 0** —— 0 会被读成「一只都没封住」。
    """
    if not rows:
        return {
            "codes": 0,
            "rounds": 0,
            "sealed": 0,
            "opened": 0,
            "sealed_all_the_way": 0,
            "hold_rate": None,
            "rounds_min": 0,
            "rounds_max": 0,
            "headline": "今天还没有封板快照。",
        }
    tracks = [track_summary(t) for t in by_code(rows).values()]
    reached = [t for t in tracks if t["first_sealed_at"] is not None]
    sealed = [t for t in reached if t["sealed_now"]]
    opened = [t for t in reached if t["opened_board"]]
    rounds = [t["rounds"] for t in tracks]
    rate = (len(sealed) / len(reached)) if reached else None
    if not reached:
        headline = f"今天 {len(tracks)} 只冲到板附近，但一只都没封上。"
    else:
        # ``rate`` 在 reached 非空时必不为 None（上面算出来的），但 mypy 不认这种
        # 跨行推断 —— 这里显式取一次，免得为类型检查把逻辑拆散。
        pct = (rate or 0.0) * 100
        headline = (
            f"今天 {len(reached)} 只封过板，最后一轮还封着 {len(sealed)} 只"
            f"（封住率 {pct:.0f}%），其中 {len(opened)} 只中途开过板。"
        )
    return {
        "codes": len(tracks),
        "rounds": sum(rounds),
        "sealed": len(reached),
        "opened": len(opened),
        "sealed_all_the_way": len(sealed),
        "hold_rate": rate,
        "rounds_min": min(rounds),
        "rounds_max": max(rounds),
        "headline": headline,
    }


__all__ = ["by_code", "session_of", "session_summary", "track_summary"]
