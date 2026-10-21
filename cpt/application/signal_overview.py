"""把信号**历史事件表**折成「每只票现在走到哪一步」的全景视图（R73 方案 A1）。

## R78：从「走到哪一步」细化到「**卡在第几步、为什么**」

R73 只给站住的票一个步骤名（``structure_ready`` / ``confirmed``），失效票的
``step`` 是空串 —— 而失效票才是绝大多数（实测 174/228）。于是「全局视图」
实际上看不见「市场普遍卡在哪一步」。

R78 补两个正交的字段：

* ``step_index``：**走完了几步**（0~3）。``2`` 表示前两步过了、卡在第三步。
* ``blocked_at``：**卡在第几步、因为哪条原因**（从 ``invalidated_reason`` 映射）。

``step_index`` 恒**不因缺原因而丢失**：``invalidated`` 一定有 0~2 的确定值，
这是状态机的事实（作废 = 三步没全过）。``blocked_at`` 才需要原因码，缺原因
就是 ``None``，如实说「不知道卡在哪一条」。

## ``blocked_at`` 只对**已记录原因**的失效行有值

实测 2026-10-21 生产库 228 只票里 135 只（59%）失效行没有原因码：R71 之前的
老行没落库，且**向上走势的作废由判定器设计上就不产原因**（``classify_invalidation``
对 ``trend_direction != -1`` 返回 ``not_in_downtrend``，但 ``invalidation_payload``
在 ``facts is None`` 且非下跌时返回 ``None``）。这类行重跑也补不出来。

所以 ``blocked_at`` 为 ``None`` 时前端必须显示「原因没记录」，**不许猜**。
``no_reason_still_blocked`` 计数专门统计这批 —— 它们**卡在第几步是确定的**
（``step_index`` 有值），只是不知道是哪条原因。

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

## R80：关注列表的「数据跑没跑」—— 用户加票后不至于看一片空白

用户 2026-10-21 的要求：新加的关注要自动跑，**跑不到的时候界面上要提醒**。

「跑过」的判据只有一个：这只票在 ``cpt_signal_event`` 里**有行**。这不是完美
判据（一只票跑过但状态没变就不会写新行，行是旧的），所以每一行都同时给出
``age_days`` —— **「没跑过」和「跑过但数据旧」必须分开说**，合成一句「没数据」
会让人不知道该等还是该跑。``age_days`` 仍只是下界（见上一节）。

``watched`` 由调用方传入（本模块不读 watchlist 文件，那是适配层的事）。
只输出 A 股的关注项；``watched=False`` 时整块字段为 ``None``，前端据此不渲染
提醒区 —— 「用户没关注任何票」不该显示成「关注了 0 只、全都缺数据」。

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
    "REASON_TO_BLOCKED_AT",
    "STEP_TITLES",
    "STALE_AFTER_DAYS",
    "signal_overview",
    "watchlist_status",
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

#: R78：``blocked_at`` 的合法取值 —— 就是「卡在第几步」的步号。
#: 直接用 :data:`STEP_TITLES` 的键，避免再造一套枚举。
STEP_TITLES: dict[str, str] = {
    "two_centers": "第一步：两个同级中枢",
    "divergence_leg": "第二步：中枢二之后还在跌",
    "reversal_bi": "第三步：跌势里冒出一笔反弹",
}

#: R78：原因码 → 卡在哪一步。**顺序即语义**：
#:
#: * ``not_enough_centers`` → 第一步没过；
#: * ``no_divergence_leg`` → 第一步过了、第二步没过（中枢已有，但没再往下走）；
#: * ``not_in_downtrend`` → 方向不对，三步**一步都没开始**，映射到第一步
#:   （「卡在第 0 步」会让用户以为有第 0 步这种怪东西）；
#: * ``previous_invalidated`` → 三步都过了却仍作废，只能归到第三步
#:   （终态不回退，见 :func:`cpt.application.signal_invalidation.classify_invalidation`）。
#:
#: 这张表是**纯函数映射**，不新增任何事实 —— 每个值都能由
#: ``(status, invalidated_reason)`` 唯一还原。
REASON_TO_BLOCKED_AT: dict[str, str] = {
    "not_enough_centers": "two_centers",
    "no_divergence_leg": "divergence_leg",
    "not_in_downtrend": "two_centers",
    "previous_invalidated": "reversal_bi",
}

#: ``step_index`` 的语义边界：0 = 一步都没过，3 = 三步全过。
_STEP_INDEX_FULL = 3

#: R78：``blocked_at`` 的稳定顺序，供前端按序渲染（不猜字典顺序）。
BLOCKED_AT_ORDER: tuple[str, ...] = ("two_centers", "divergence_leg", "reversal_bi")


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


def _step_index(status: str, reason: str | None) -> int | None:
    """走完了几步（0~3）；**不确定就返回 None**，不硬凑一个数。

    * ``confirmed`` → 3（三步全过，含反抽确认）；
    * ``structure_ready`` → 2（前两步过了、等第三步的反向笔）；
    * ``invalidated`` → 由原因码定位；**没有原因码返回 None**。

    ## 为什么缺原因时是 None 而不是「保守取 2」

    R78 第一版把没原因的 ``invalidated`` 一律取 2，理由是「作废必然三步没全过，
    所以**最多**走到第二步」。生产实测立刻暴露问题：117 只无原因作废票全部落进
    「第 2 步」，与 55 只真正的 ``structure_ready`` 混在同一桶里 —— 分布显示
    「173 只走到第二步」，读起来像「市场普遍就差临门一脚」，但真相是
    **117 只根本不知道走到哪**。

    上界推断一旦被当成分布读，就会造出与事实相反的结论。故改为 ``None``，
    分布里单列一桶「哪一步不确定」。
    """
    if status == "confirmed":
        return _STEP_INDEX_FULL
    if status == "structure_ready":
        return 2
    if status == "invalidated":
        blocked = _blocked_at(reason)
        if blocked == "two_centers":
            return 0
        if blocked == "divergence_leg":
            return 1
        if blocked == "reversal_bi":
            return 2
        # 原因没记录 ⇒ 卡在第几步**不确定**。
        return None
    # alert / candidate / none 等：三步没全过，保守按 2（等反向笔那一步）。
    return 2


def _blocked_at(reason: str | None) -> str | None:
    """原因码 → 卡在哪一步；**没有原因就返回 None，绝不猜**。"""
    if not reason:
        return None
    return REASON_TO_BLOCKED_AT.get(str(reason))


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
                # R78：走完了几步（0~3）与卡在哪一步（缺原因时 None）。
                "step_index": _step_index(status, reason),
                "blocked_at": _blocked_at(reason),
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


def _step_distribution(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """R78：全市场「走完几步」的分布 + 「卡在哪一步」的原因分解。

    ## 为什么这两张表要分开

    ``step_index_counts`` 回答「多少只票走完了 0/1/2/3 步」—— **恒有值**。
    ``blocked_reason_counts`` 回答「卡住的票里，各自卡在哪一步、因为什么」——
    只有**记了原因**的行才有值。

    两者相加**不等于** ``total``，差的就是「没记原因」的那批。这不是缺陷，
    是数据现状；硬凑平会逼出一句编造的原因。

    :returns: 形如::

            {
              "step_index_counts": {"0": 0, "1": 19, "2": 65, "3": 10, "unknown": 117},
              "blocked_reason_counts": {          # 只含记了原因的行
                "divergence_leg": {"no_divergence_leg": 19},
                "reversal_bi": {"previous_invalidated": 1},
              },
              "no_reason_but_blocked": 117,       # step_index 为 None
              "standing": 65,                     # structure_ready + confirmed
            }

    盲区行（兜底结构）**不进**这里 —— 它们连第一步都没开始，混进来会让
    「第 0 步」虚高（实测 25 只）。

    ⚠️ ``unknown`` 桶与 ``no_reason_but_blocked`` 是同一个数，刻意重复一次：
    前者是分布的一行，后者是给人读的一句话。两者必须相等，测试钉住。
    """
    step_counts: Counter[str] = Counter()
    blocked: dict[str, Counter[str]] = {}
    no_reason_blocked = 0
    standing = 0
    for row in rows:
        if row["is_blind_spot"]:
            continue
        index = row["step_index"]
        if index is None:
            # 原因没记录 ⇒ 哪一步不确定，单列 unknown 桶（不硬凑成 2）。
            step_counts["unknown"] += 1
            no_reason_blocked += 1
        else:
            step_counts[str(int(index))] += 1
        if row["blocked_at"] is not None:
            blocked.setdefault(str(row["blocked_at"]), Counter())[
                str(row["invalidated_reason"])
            ] += 1
        if row["status"] in ("structure_ready", "confirmed"):
            standing += 1
    return {
        # 0~3 全部给键，缺的补 0 —— 前端不用自己补，也不会因缺键显示 undefined。
        "step_index_counts": {
            **{str(i): step_counts.get(str(i), 0) for i in range(_STEP_INDEX_FULL + 1)},
            "unknown": step_counts.get("unknown", 0),
        },
        # 按 BLOCKED_AT_ORDER 给稳定顺序（不猜字典序）。
        "blocked_reason_counts": {
            step: dict(blocked[step]) for step in BLOCKED_AT_ORDER if step in blocked
        },
        "blocked_at_order": list(BLOCKED_AT_ORDER),
        "no_reason_but_blocked": no_reason_blocked,
        "standing": standing,
    }


def watchlist_status(
    codes: Sequence[str],
    latest: dict[str, dict[str, Any]],
    now_ms: int,
    *,
    hours: int,
) -> dict[str, Any]:
    """R80：关注列表里每只票「跑过没有、数据多旧」。

    :param codes: A 股关注代码（调用方已按市场过滤；重复项按首次出现去重保序）。
    :param latest: :func:`_latest_per_key` 的结果 —— 与全景共用同一份「最新事件」，
        避免两处各算一次最新导致口径打架。
    :param hours: 仅用于**文案**（「自动任务每 N 小时跑一次」），不参与判定。
        本模块不读调度器配置，写死一个数会随部署漂移。
    :returns: 形如::

            {
              "watched": True,
              "counts": {"total": 3, "never_run": 1, "stale": 1, "fresh": 1},
              "items": [{"code", "status", "age_days", "never_run"}, ...],
              "refresh_hours": 4,
            }

        或 ``{"watched": False, "counts": None, "items": [], "refresh_hours": hours}``
        （用户没关注任何 A 股）。
    """
    seen: list[str] = []
    for code in codes:
        if isinstance(code, str) and code and code not in seen:
            seen.append(code)
    if not seen:
        return {"watched": False, "counts": None, "items": [], "refresh_hours": hours}
    items: list[dict[str, Any]] = []
    never_run = stale = fresh = 0
    for code in seen:
        event = latest.get(code)
        if event is None:
            never_run += 1
            items.append(
                {
                    "code": code,
                    "status": None,
                    "age_days": None,
                    # 「没跑过」不是「跑了但旧」—— 分开标记，见模块 docstring。
                    "never_run": True,
                }
            )
            continue
        age = _age_days(event, now_ms)
        is_stale = age is not None and age >= STALE_AFTER_DAYS
        if is_stale:
            stale += 1
        else:
            fresh += 1
        items.append(
            {
                "code": code,
                "status": str(event.get("status") or "unknown"),
                "age_days": age,
                "never_run": False,
            }
        )
    return {
        "watched": True,
        "counts": {"total": len(seen), "never_run": never_run, "stale": stale, "fresh": fresh},
        "items": items,
        "refresh_hours": hours,
    }


def signal_overview(
    events: Sequence[dict[str, Any]],
    *,
    now_ms: int,
    watched: Sequence[str] | None = None,
    watch_refresh_hours: int = 4,
) -> dict[str, Any]:
    """把信号事件流折成全景视图。

    :param events: :func:`cpt.storage.signal_event_store.load_signal_events`
        的返回值（时间倒序、每行含 ``code`` / ``status`` / ``structure_id`` /
        ``transition_time`` 毫秒 / ``invalidated_reason``）。
    :param now_ms: 当前时刻（Unix 毫秒）。**必须由调用方传入**：本模块不读
        系统时钟，否则同一份数据在不同时间跑出的「距今天数」不可复现。
    :param watched: R80：A 股关注代码（``None`` = 不评估关注区，与传空列表同义）。
    :param watch_refresh_hours: 关注自动任务的间隔小时数，仅用于前端文案。
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
              "watchlist": {...},                 # R80，见 watchlist_status
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
        # R78：全市场「走完几步 / 卡在哪步、为什么」的分布。
        "step_distribution": _step_distribution(rows),
        "step_titles": dict(STEP_TITLES),
        # R80：关注列表「跑过没有、数据多旧」。
        "watchlist": watchlist_status(watched or (), latest, now_ms, hours=watch_refresh_hours),
        "rows": rows,
    }
