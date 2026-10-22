"""今日采集状态：把 ``intraday_collectd`` 的日志读成一句人话（R89）。

## 为什么读日志而不是查库

盘中采集**刻意不写数据库**（ADR 0003 §E2：``intraday_quote`` 那张表归
emotion-core）。所以「采集今天跑没跑、跑得怎么样」的唯一事实来源就是
``~/logs/intraday-collect.log``（一行一次运行，JSON）。

读日志有一个代价必须承认：**日志不轮转就会无限长**。本模块只读文件尾部
（默认 8 KiB），不读全文 —— 一天 240 行约 30 KB，尾部足以覆盖当天全部记录。

## 四种状态（不许合并）

| ``state`` | 含义 | 界面怎么说 |
|---|---|---|
| ``running`` | 最后一轮在 10 分钟内且不是 error | 「正在采，最后一轮 X 分钟前」 |
| ``stale`` | 现在不在盘中，或最后一轮太旧 | 「最后一轮是 X 小时前，现在不在采集时段」 |
| ``failed`` | 最近连续 ``max_failures`` 轮都是 error | 「采集挂了，连续 N 轮失败」 |
| ``never`` | 日志里一条都没有 | 「还没跑过（今天 09:30 之后才会开始）」 |

**「空结果」不算失败**：午休、竞价前都会 ``empty``/``skipped``，把它们当失败
会让状态条天天红。

## 今天与「最新交易日」是两个口径

- ``as_of`` = 日志里最后一条的北京时间日期，用来判断「这份日志是不是今天的」。
- ``market_day`` = 库里的最新交易日（由调用方传入，不在本模块查库）。

周末与节假日库里最新交易日会落后于今天，这时状态条应当说「今天休市」而不是
「采集挂了」。本模块只负责把两个日期都摆出来，判断由调用方/前端做。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

__all__ = [
    "STALE_AFTER_SECONDS",
    "collect_status",
    "parse_log_tail",
]

#: 超过这么久没新记录就算「不新鲜」。timer 每 2 分钟一轮，10 分钟 = 允许挂 5 轮。
STALE_AFTER_SECONDS: Final[float] = 600.0

#: 只读文件尾部这么多字节。一天 240 行 × ~130 B ≈ 30 KiB，8 KiB 足够覆盖当天。
_TAIL_BYTES: Final[int] = 8192


def parse_log_tail(path: Path, *, max_bytes: int = _TAIL_BYTES) -> list[dict[str, Any]]:
    """读日志尾部并解析成 dict 列表（按时间升序）。

    坏行**跳过不抛** —— 日志写一半断电是常态，不能因为它让整个状态条消失。
    """
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        raw = fh.read()
    if size > max_bytes:
        # 截断必然切掉第一行的一半，丢掉它。
        raw = raw.split(b"\n", 1)[-1]
    out: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def collect_status(
    records: Sequence[dict[str, Any]],
    *,
    now: datetime,
    market_day: date | None = None,
    max_failures: int = 3,
    stale_after: float = STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """把日志记录压成一屏状态。

    Args:
        records: :func:`parse_log_tail` 的结果（旧→新）。
        now: 当前时刻（**带时区**，北京时间）。
        market_day: 库里最新交易日；``None`` 表示查不到（降级成只说日志）。
        max_failures: 连续这么多轮 error 才算「挂了」。
        stale_after: 超过这么多秒没新记录算「不新鲜」。
    """
    base: dict[str, Any] = {
        "state": "never",
        "as_of": None,
        "age_seconds": None,
        "last_outcome": None,
        "today_rounds": 0,
        "today_ok": 0,
        "today_skipped": 0,
        "today_errors": 0,
        "consecutive_errors": 0,
        "market_day": market_day.isoformat() if market_day else None,
        "is_market_day_today": (market_day == now.date() if market_day is not None else None),
        "headline": "",
        "action": "",
    }
    if not records:
        base["headline"] = "还没跑过 — 今天 09:30 之后才会开始"
        base["action"] = "不用做任何事。盘中每 2 分钟会自动采一轮。"
        return base

    last_at = _parse_at(records[-1].get("at"))
    if last_at is None:
        base["headline"] = "日志格式看不懂 — 最后一条没有可解析的时间"
        base["action"] = "检查 /home/ubuntu/logs/intraday-collect.log"
        return base
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=now.tzinfo)

    age = (now - last_at).total_seconds()
    today = now.date()
    today_records = [
        r for r in records if (at := _parse_at(r.get("at"))) is not None and at.date() == today
    ]

    # 连续 error：从尾部往前数，遇到第一个非 error 就停。
    consecutive = 0
    for r in reversed(records):
        if r.get("outcome") == "error":
            consecutive += 1
        else:
            break

    ok = sum(1 for r in today_records if r.get("outcome") == "ok")
    skipped = sum(1 for r in today_records if r.get("outcome") in ("skipped", "empty"))
    errors = sum(1 for r in today_records if r.get("outcome") == "error")
    last_outcome = records[-1].get("outcome")

    if consecutive >= max_failures:
        state = "failed"
    elif age > stale_after:
        state = "stale"
    else:
        state = "running"

    minutes = int(age // 60)
    age_text = f"{minutes} 分钟前" if minutes < 60 else f"{age / 3600:.1f} 小时前"

    if state == "failed":
        headline = f"采集挂了 — 连续 {consecutive} 轮报错"
        action = "看日志 /home/ubuntu/logs/intraday-collect.log 里 first_error 那行。"
    elif state == "stale":
        if market_day is not None and market_day != today:
            headline = f"今天休市（库里最新交易日是 {market_day.isoformat()}）"
            action = "不用做任何事，下一个交易日 09:30 自动恢复。"
        else:
            headline = f"最后一轮是 {age_text}"
            action = "不在采集时段就正常；若现在是盘中，检查 cpt-intraday-collect.timer。"
    else:
        # running
        if last_outcome == "skipped":
            reason = records[-1].get("reason")
            headline = f"刚跑过一轮，被跳过：{reason}" if reason else "刚跑过一轮，被跳过"
            action = "休市或非交易时段，属正常。"
        elif last_outcome == "empty":
            headline = f"刚跑过一轮，涨幅榜上没票接近涨停（{age_text}）"
            action = "正常 —— 不是每条记录都能采到东西。"
        else:
            cand = records[-1].get("candidates")
            sealed = records[-1].get("sealed")
            headline = f"正在采 — 今天 {len(today_records)} 轮，最后一轮 {age_text}"
            if isinstance(cand, int) and isinstance(sealed, int):
                action = f"最后这轮：候选 {cand} 只，其中已封 {sealed} 只。"
            else:
                action = "采集正常。"

    base.update(
        {
            "state": state,
            "as_of": last_at.isoformat(),
            "age_seconds": round(age, 1),
            "last_outcome": last_outcome,
            "today_rounds": len(today_records),
            "today_ok": ok,
            "today_skipped": skipped,
            "today_errors": errors,
            "consecutive_errors": consecutive,
            "headline": headline,
            "action": action,
        }
    )
    return base
