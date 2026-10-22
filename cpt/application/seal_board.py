"""首屏「今天该不该看盘」的封板强度那一段（R91）。

## 这个模块解决什么

R89 起首屏已经有「全市场走到哪一步」（三步进度）。那说的是**收盘后的日线结构**，
盘中不动。用户在盘中最想知道的是另一件事：**今天的封板情绪怎么样**。

R90 落库之后这件事有数据了（``public.intraday_seal_snapshot``），但那只表是
「每只票每轮一行」，人读不懂。本模块把它压成一句话 + 三个数：

| 输出 | 来自 |
|---|---|
| 几只封着 | 最新一轮 ``sealed=True`` 的行数 |
| 封单中位数 / 强度分档 | :func:`cpt.application.intraday_analysis.board_health` |
| 有几只在往板上打 | 最新一轮 ``sealed=False`` 的行数 |

## 三条纪律

1. **没数据不许说成「封板少」**。表里没有轮次 → ``available=False`` + 说清原因，
   不是「今天 0 只封板」。这两种情况在盘中的应对完全相反。
2. **不给买卖指令**（R76 红线）。只说「几只封着、厚不厚」，不说「该不该买」。
3. **数字必须指回字段**。每个数都能追到「最新一轮的哪一列」，见 ``evidence``。

## 和「盘中采集活没活」的分工

``cpt/application/intraday_status.py`` 回答的是**采集器本身**在不在跑（读日志）；
本模块回答的是**盘面**长什么样（读表）。两个都坏了要能分开说 —— 采集器在跑但
今天是空盘，与采集器挂了，是两件事。
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any

from cpt.application.intraday_analysis import board_health

__all__ = [
    "SEAL_BOARD_BASIS",
    "seal_board_summary",
]

#: 自报口径：这张表是**每 2 分钟一轮**的盘中快照，不是收盘后的全市场统计。
SEAL_BOARD_BASIS: str = "intraday_seal_snapshot_latest_round"

#: 加在文案末尾的实测提醒。**必须留着** —— R70/R87 已经证明结构信号没有预测力，
#: 盘中封板强度同样没有经过验收，不能让人误读成「封得厚就能买」。
_CAVEAT = "封板强度是位置描述，不是买入信号：它只说明现在封得厚不厚，不说明明天涨不涨。"


def _hms(at: dt.datetime) -> str:
    """北京时间的时分 —— 用户看的是钟点，不是 UTC。"""
    return at.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%H:%M")


def seal_board_summary(latest: dict[str, Any]) -> dict[str, Any]:
    """把 :func:`cpt.storage.intraday_seal_store.load_latest_round` 的结果翻译成人话。

    Args:
        latest: ``load_latest_round()`` 的返回值，键为 ``at`` / ``rows`` / ``counts``。

    Returns:
        ``{
            "available": bool,
            "as_of": ISO 字符串 | None,
            "as_of_hms": "HH:MM"（北京） | None,
            "sealed": int, "open_board": int, "total": int,
            "median_bid_lots": float | None,
            "board_strength": float | None,
            "label": "厚"/"正常"/"偏薄"/"薄"/"没样本",
            "headline": str, "action": str,
            "evidence": {...},   # 每个数追到哪一列
            "basis": str,
        }``

        ``available=False`` 时 ``headline`` 说清「没有最新一轮」，**不说成 0 只封板**。
    """
    at = latest.get("at")
    rows = latest.get("rows") or []
    counts = latest.get("counts") or {}
    base: dict[str, Any] = {
        "available": False,
        "as_of": None,
        "as_of_hms": None,
        "sealed": 0,
        "open_board": 0,
        "total": 0,
        "median_bid_lots": None,
        "board_strength": None,
        "label": "没样本",
        "headline": "",
        "action": _CAVEAT,
        "evidence": {
            "table": "public.intraday_seal_snapshot",
            "columns": ("at", "code", "sealed", "bid1_lots", "limit_price"),
            "rounds_considered": 1,
        },
        "basis": SEAL_BOARD_BASIS,
    }

    if at is None or not rows:
        base["headline"] = (
            "今天还没有盘中封板快照 —— 采集器没跑过，或今天休市。"
            "这不是「今天没有票封板」，两者要从「盘中采集」那一行分开看。"
        )
        return base

    sealed_rows = [r for r in rows if r.get("sealed")]
    open_rows = [r for r in rows if not r.get("sealed")]
    lots = sorted(r["bid1_lots"] for r in sealed_rows if r.get("bid1_lots") is not None)
    # 中位数只在这里算一次，交给 board_health —— 它不自己从 rows 重算，
    # 避免「读方算一遍、被调方再算一遍」两份口径（R76 的老教训）。
    median = float(statistics.median(lots)) if lots else None
    health = board_health(
        {
            "sealed": sealed_rows,
            "approaching": open_rows,
            "counts": counts,
            "median_bid_lots": median,
        }
    )

    n_sealed = len(sealed_rows)
    n_open = len(open_rows)
    stamp = _hms(at)
    base.update(
        {
            "available": True,
            "as_of": at.isoformat(),
            "as_of_hms": stamp,
            "sealed": n_sealed,
            "open_board": n_open,
            "total": int(counts.get("total") or len(rows)),
            "median_bid_lots": median,
            "board_strength": health["board_strength"],
            "label": health["label"],
            "headline": (
                f"{stamp} 这一轮：{n_sealed} 只封在板上"
                + (f"、{n_open} 只冲到板附近没封住" if n_open else "")
                + (
                    f"；封单中位 {median:,.0f} 手，封板{health['label']}"
                    if median is not None
                    else ""
                )
                + "。"
            ),
        }
    )
    if n_sealed == 0:
        base["headline"] = (
            f"{stamp} 这一轮：板附近 {len(rows)} 只，没有一只封住。"
            "封板情绪按「没样本」处理 —— 不是「封得薄」，是压根没有样本可评。"
        )
    return base
