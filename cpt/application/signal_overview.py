"""把信号**历史事件表**折成「每只票现在走到哪一步」的全景视图（R73 方案 A1）。

## 这一层能回答什么、不能回答什么

数据源是 ``public.cpt_signal_event`` —— **状态跃迁事件**，只在状态变化时追加一行。
于是「最新状态」是**事实**，但「这一步卡了几天」只是**下界**：

* 一只票 6 个月前卡在第 2 步、之后每月都写一行同样的 ``invalidated``，
  「距最后一次跃迁」会是 1 天，而它实际卡了 180 天；
* 一只票今天才首次评估，也是 0 天。

两者在表里长得一模一样。所以本模块把 ``age_days`` 命名为
「距最后一次跃迁 N 天」并**永不**把它说成「卡了 N 天」。要回答后者，
需要每轮落一份步骤快照（方案 A2，需新建表，用户 2026-10-21 选择先做 A1）。

## 兜底 structure_id 是一个盲区，必须如实标出

``structure_id`` 形如 ``level5:empty:000014`` 的是
:func:`cpt.application.a_share_snapshot._empty_structure_id` 的兜底值 ——
它表示**结构不足两个中枢**，连第一笔都没走到。这类行的 ``status`` 恒为
``invalidated``（``assess_first_buy`` 结构准备条件不成立），所以**不在**
「已走到位置」的行里出现，也不会污染步骤分布。

反过来，``structure_id`` 里带真实中枢 id 的行（``level5:bi:5:000006``）
才**有能力**区分「结构已就绪」与「结构不成立」—— 它们是全景视图该看的样本。

## 纪律：只做归类，不发明结论

本模块不读行情、不算收益、不预测方向。每一行输出都能指回
``cpt_signal_event`` 的一个字段，或
:func:`cpt.application.signal_invalidation.classify_invalidation` 的一个原因码。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

from cpt.application.signal_invalidation import REASON_CODES

__all__ = [
    "BLIND_SPOT_EMPTY_STRUCTURE",
    "STALE_AFTER_DAYS",
    "signal_overview",
]

#: 兜底 structure_id 的特征：``level5:empty:<code>``（见模块 docstring）。
_EMPTY_STRUCTURE_RE = re.compile(r"^level\d+:empty:\d{6}$")

#: 距最后一次跃迁超过这么多个自然日，判为「陈旧」：样本在本窗口内没再动过，
#: 它的「当前状态」只是最后已知状态，不能当今天的状态读。
STALE_AFTER_DAYS = 7

#: 盲区计数键：结构不足两个中枢、连第一步都没走到。
BLIND_SPOT_EMPTY_STRUCTURE = "empty_structure"

_MS_PER_DAY = 86_400_000

#: 步骤序号 → 一步的说法（与 :mod:`cpt.application.signal_progress` 的三步对齐）。
_STEP_NAMES: dict[str, str] = {
    "structure_ready": "位置已就绪、等反向笔",
    "confirmed": "三步全过（反抽已确认）",
}


def _is_empty_structure(structure_id: Any) -> bool:
    """``structure_id`` 是否为「结构不足」的兜底值。"""
    return isinstance(structure_id, str) and bool(_EMPTY_STRUCTURE_RE.match(structure_id))


def _latest_per_key(events: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """每只票取**最新**一条事件；按 ``transition_time`` 再按 ``id`` 定序。

    ``load_signal_events`` 已经按 ``transition_time DESC, id DESC`` 返回，
    这里仍显式比较一次：调用方可能传别的顺序进来，而「最新」必须是确定的。
    """
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        code = event.get("code")
        if not isinstance(code, str) or not code:
            continue
        current = latest.get(code)
        if current is None or _is_newer(event, current):
            latest[code] = event
    return latest


def _is_newer(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """``candidate`` 是否比 ``current`` 更新（时间相同看 id）。"""
    c_time = candidate.get("transition_time") or 0
    o_time = current.get("transition_time") or 0
    if c_time != o_time:
        return bool(c_time > o_time)
    return bool((candidate.get("id") or 0) > (current.get("id") or 0))


def _reason_of(event: dict[str, Any]) -> str | None:
    """失效行的原因码；没记原因返回 ``None``（**不猜**）。

    只对 ``invalidated`` 取值：其它状态就算 ``center_ids`` 尾项碰巧带记号
    （理论上不会有），也不能当成失效原因展示。
    """
    if event.get("status") != "invalidated":
        return None
    reason = event.get("invalidated_reason")
    return str(reason) if reason else None


def _age_days(event: dict[str, Any], now_ms: int) -> int | None:
    """距最后一次状态跃迁的自然日数；时间不可用返回 ``None``。"""
    transition = event.get("transition_time")
    if not isinstance(transition, int) or transition <= 0:
        return None
    return max(0, (now_ms - transition) // _MS_PER_DAY)


def _bucket_rows(
    latest: dict[str, dict[str, Any]], now_ms: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按票归类。返回 (明细, 盲区计数)。"""
    rows: list[dict[str, Any]] = []
    blind_spots: Counter[str] = Counter()
    for code in sorted(latest):
        event = latest[code]
        structure_id = event.get("structure_id")
        status = str(event.get("status") or "unknown")
        blind = _is_empty_structure(structure_id)
        if blind:
            blind_spots[BLIND_SPOT_EMPTY_STRUCTURE] += 1
        age = _age_days(event, now_ms)
        stale = age is not None and age >= STALE_AFTER_DAYS
        reason = _reason_of(event)
        rows.append(
            {
                "code": code,
                "status": status,
                "step": _STEP_NAMES.get(status, ""),
                "divergence_status": event.get("divergence_status"),
                "structure_id": structure_id,
                "transition_time": event.get("transition_time"),
                # ⚠️ 这是**下界**，不是「卡了几天」，见模块 docstring。
                "age_days": age,
                "stale": stale,
                "invalidated_reason": reason,
                # 兜底结构的失效原因只有「结构不足」一种，标记出来避免误读。
                "is_blind_spot": blind,
            }
        )
    rows.sort(key=lambda row: (row["age_days"] is None, row["age_days"] or 0, row["code"]))
    return rows, dict(blind_spots)


def _status_summary(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """按状态计数；只数**能区分结构**的行（兜底结构单列，见模块 docstring）。"""
    counts: Counter[str] = Counter()
    for row in rows:
        if row["is_blind_spot"]:
            continue
        counts[str(row["status"])] += 1
    return dict(counts)


def _reason_summary(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """失效原因计数；没记原因的单列 ``unknown``，不并进任何具体原因。"""
    counts: Counter[str] = Counter()
    for row in rows:
        if row["status"] != "invalidated" or row["is_blind_spot"]:
            continue
        counts[row["invalidated_reason"] or "unknown"] += 1
    return dict(counts)


def signal_overview(events: Sequence[dict[str, Any]], *, now_ms: int) -> dict[str, Any]:
    """把信号事件流折成全景视图。

    :param events: :func:`cpt.storage.signal_event_store.load_signal_events`
        的返回值（时间倒序、每行含 ``code`` / ``status`` / ``structure_id`` /
        ``transition_time`` 毫秒 / ``invalidated_reason``）。
    :param now_ms: 当前时刻（Unix 毫秒）。**必须由调用方传入**：本模块不读
        系统时钟，否则同一份数据在不同时间跑出的「距今天数」不可复现。
    :returns: 形如::

            {
              "total": 203,                       # 覆盖的票数
              "status_counts": {"invalidated": 137, "structure_ready": 56, "confirmed": 10},
              "invalidation_reasons": {"unknown": 135, "no_divergence_leg": 1, ...},
              "blind_spots": {"empty_structure": 27},
              "stale_count": 141,                 # 距最后跃迁 >= STALE_AFTER_DAYS
              "stale_after_days": 7,
              "age_is_lower_bound": True,         # 恒 True，钉死口径
              "reason_codes": [...],              # 合法原因码，供前端校验
              "rows": [{"code", "status", "step", "age_days", "stale", ...}, ...],
            }

        的只读字典。``rows`` 按 (age_days, code) 升序 —— 最新的在最前。
    """
    latest = _latest_per_key(events)
    rows, blind_spots = _bucket_rows(latest, now_ms)
    stale_count = sum(1 for row in rows if row["stale"])
    return {
        "total": len(rows),
        "status_counts": _status_summary(rows),
        "invalidation_reasons": _reason_summary(rows),
        "blind_spots": blind_spots,
        "stale_count": stale_count,
        "stale_after_days": STALE_AFTER_DAYS,
        # 口径钉死：age_days 永远只是下界。前端不得改写成「卡了 N 天」。
        "age_is_lower_bound": True,
        "reason_codes": list(REASON_CODES),
        "rows": rows,
    }
