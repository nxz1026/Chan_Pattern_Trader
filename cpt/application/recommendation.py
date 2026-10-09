"""看板的**结构判断摘要**：把信号翻译成「一句话 + 一个动作 + 一个参考价」。

## 为什么买卖与价格**必须**由代码算，不能交给 LLM

用户要的是「直观、简单的推荐（买卖、价格）」，同时要 LLM 出摘要。
这两件事**必须分开**，否则等于让模型在**最要紧的输出上**自由生成：

- **不可复现** —— 同一份快照跑两次给出两个不同结论，没法回归、没法对账；
- **不可测** —— 「推荐得对不对」没有客观判据；
- **会漂移** —— 提示词一改、模型一换，推荐就变，而结构数据没变。

⇒ 本模块**纯确定性**、零 LLM、零 IO，输入是快照里的结构事实，
输出是动作 + 参考价 + 依据。LLM 只在
:func:`cpt.application.llm_cases.summarize_recommendation` 里
**给这段结果配一段人话**，且失败时前端照常显示确定性结果。

## 措辞纪律：这是**结构判断的翻译**，不是投资建议

看板全程标注「只读 · 不构成投资建议」。所以这里的动作词是
``买入关注`` / ``卖出关注`` 这类**结构状态**，不是「建议买卖」。
``action_label`` 刻意用「结构」二字收尾，避免被当成荐股。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

__all__ = [
    "MIN_BARS",
    "build_history",
    "ACTION_BUY",
    "ACTION_SELL",
    "ACTION_WATCH",
    "ACTION_HOLD",
    "build_recommendation",
    "ACTIONS",
]


#: 低于这么多根 K 线就**判不了结构** —— 与 ``factor_recompute`` 的 30 根门槛同源。
#: R45 实测：Oracle 上有 **17 只**新股落在这个区间，它们在推荐卡上原本显示
#: 「观望 / 暂无明确结构信号」—— 与「这只票确实没信号」**一字不差**。
#: 用户无法区分「系统不知道」和「系统知道没有」，而这两件事该有的动作完全不同。
MIN_BARS: Final[int] = 30

ACTION_BUY: Final[str] = "buy"
ACTION_SELL: Final[str] = "sell"
ACTION_WATCH: Final[str] = "watch"  # 有候选/预警，但未确认
ACTION_HOLD: Final[str] = "hold"  # 无信号 / 已失效

ACTIONS: Final[dict[str, str]] = {
    ACTION_BUY: "买入结构",
    ACTION_SELL: "卖出结构",
    ACTION_WATCH: "关注",
    ACTION_HOLD: "观望",
}

#: 信号状态 → 该状态有多「硬」。确认 > 结构具备/候选 > 预警 > 失效。
#:
#: ⚠️ ``structure_ready`` **必须在表里**：它是状态机里正常会出现的状态
#: （结构条件已成立、只差反向笔确认），漏掉就会被下面那句「未知的信号状态」
#: 降级成「暂无明确结构信号」。实测 000002 在 2026-10-05 就吃到过这条
#: （库里 ``reason`` = 「未知的信号状态 'structure_ready'」）。
_STATUS_RANK: Final[dict[str, int]] = {
    "confirmed": 3,
    "structure_ready": 2,
    "candidate": 2,
    "alert": 1,
    "invalidated": 0,
}

#: 信号类型 → 方向
_TYPE_TO_ACTION: Final[dict[str, str]] = {
    "first_buy": ACTION_BUY,
    "first_sell": ACTION_SELL,
}


def _num(value: Any) -> float | None:
    """容忍字符串/None（快照里的价格来自 jsonb，可能是字符串）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def build_recommendation(snapshot: dict[str, Any]) -> dict[str, Any]:
    """从快照算出推荐块。**永不抛异常** —— 结构缺失就降级到「观望」。

    :param snapshot: ``a_share_snapshot`` 的产物（含 ``signal`` / ``candles``）。
    :returns: 见模块 docstring；``available`` 为 ``False`` 时没有 action。
    """
    if not isinstance(snapshot, dict):
        return _degraded("快照不是对象")

    # ⚠️ **先看数据够不够，再谈有没有信号。**
    # 顺序不能反：K 线不足时本来就不可能有可信信号，
    # 先报「没有信号」会把「不知道」说成「知道没有」。
    quality = _data_quality(snapshot)
    if not quality["sufficient"]:
        return _degraded(
            f"K 线仅 {quality['bars']} 根，不足 {MIN_BARS} 根 —— 数据不足，暂无法判断结构",
            quality=quality,
        )

    signal = snapshot.get("signal")
    if not isinstance(signal, dict) or not signal:
        return _degraded("当前没有信号", quality=quality)

    status = str(signal.get("status") or "").strip()
    sig_type = str(signal.get("signal_type") or "").strip()
    divergence = str(signal.get("divergence_status") or "").strip()

    if status not in _STATUS_RANK:
        # R59（审计 M23）：**必须带 quality** —— 不传时 ``_degraded`` 会退回
        # ``_data_quality({})``，把「状态字段不认识」谎报成「0 根 / 数据不足」，
        # 与本模块 :192 的纪律「「不知道」和「没有」必须能分开」直接矛盾。
        # 数据质量在 :103 已经算过，没有任何理由丢掉。
        return _degraded(f"未知的信号状态 {status!r}", quality=quality)
    if sig_type not in _TYPE_TO_ACTION:
        return _degraded(f"未知的信号类型 {sig_type!r}", quality=quality)

    # 失效的信号**不是**「反向信号」—— 它意味着「之前那个判断已经不成立」，
    # 所以动作退回观望，而不是取反。取反会把「失效的一买」说成「卖出」。
    if status == "invalidated":
        # ⚠️ 也要带 price —— 失效**发生在某个价位**上，那个价是有用的上下文
        # （补测试时发现这里把 price 漏了，界面上会显示「—」而不是价位）。
        #
        # 两种 ``invalidated`` 的**人话完全不同**，不能都叫「已失效」：
        # - 结构**从未成立**（同级中枢不足两个）⇒ 是「条件不成立」，不是「失效」；
        #   说成「失效」会让用户以为「之前确认过的一买被打掉了」。
        # - **曾经成立**（``center_ids`` 里有两个中枢）后被打掉 ⇒ 才是「已失效」。
        # 判据是 ``center_ids`` 的条数；**缺这个 key** 说明是 R21 之前的老快照，
        # 不猜，沿用「已失效」措辞 —— 不把「不知道」说成「没有」。
        center_ids = signal.get("center_ids")
        never_ready = isinstance(center_ids, (list, tuple)) and len(center_ids) < 2
        if never_ready:
            headline = "结构条件不成立"
            reason = f"{_label(sig_type)}条件不成立（未形成两个同级中枢）—— 结构尚不足以判定。"
        else:
            headline = "信号已失效"
            reason = f"{_label(sig_type)}已失效（不取反方向）—— 之前的结构判断不再成立。"
        out = _build(
            ACTION_HOLD,
            headline,
            reason,
            price=_num(signal.get("price")) or _last_close(snapshot),
            status=status,
            signal_type=sig_type,
            divergence=divergence,
        )
        out["data_quality"] = _data_quality(snapshot)
        return out

    action = _TYPE_TO_ACTION[sig_type]
    rank = _STATUS_RANK[status]
    if rank >= _STATUS_RANK["confirmed"]:
        headline = f"{_label(sig_type)}**已确认**"
    elif status == "structure_ready":
        # 结构与方向都成立，只差反向笔确认。与「候选 / 预警」不是一回事，
        # 给一句能自解释的文案（此前这条状态会直接掉进「未知状态」降级）。
        action, headline = ACTION_WATCH, f"{_label(sig_type)}结构已具备（待反向笔确认）"
    elif status == "candidate":
        action, headline = ACTION_WATCH, f"{_label(sig_type)}候选（未确认）"
    else:
        action, headline = ACTION_WATCH, f"{_label(sig_type)}预警"

    price = _num(signal.get("price"))
    if price is None:
        price = _last_close(snapshot)

    reasons: list[str] = [f"信号状态：{status}"]
    if divergence == "detected":
        reasons.append("已检测到背驰")
    if price is not None:
        reasons.append(f"参考价 {price:.2f}")

    out = _build(
        action,
        headline,
        _join(reasons),
        price=price,
        status=status,
        signal_type=sig_type,
        divergence=divergence,
    )
    out["data_quality"] = _data_quality(snapshot)
    return out


def _data_quality(snapshot: dict[str, Any]) -> dict[str, Any]:
    """数据够不够算结构。**「不知道」和「没有」必须能分开。**"""
    candles = snapshot.get("candles")
    n = len(candles) if isinstance(candles, list) else 0
    return {
        "bars": n,
        "min_bars": MIN_BARS,
        "sufficient": n >= MIN_BARS,
    }


def _last_close(snapshot: dict[str, Any]) -> float | None:
    candles = snapshot.get("candles")
    if not isinstance(candles, list) or not candles:
        return None
    last = candles[-1]
    return _num(last.get("close")) if isinstance(last, dict) else None


def _label(sig_type: str) -> str:
    return {"first_buy": "一买", "first_sell": "一卖"}.get(sig_type, sig_type)


def _join(items: list[str]) -> str:
    return "；".join(x for x in items if x)


def _build(
    action: str,
    headline: str,
    reason: str,
    *,
    price: float | None = None,
    status: str = "",
    signal_type: str = "",
    divergence: str = "",
) -> dict[str, Any]:
    return {
        "available": True,
        "action": action,
        "action_label": ACTIONS.get(action, action),
        "headline": headline,
        "reason": reason,
        "price": price,
        "status": status,
        "signal_type": signal_type,
        "divergence_status": divergence,
        "data_quality": {"bars": 0, "min_bars": MIN_BARS, "sufficient": True},
        "disclaimer": "结构状态翻译，非投资建议",
    }


def _degraded(why: str, *, quality: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "available": False,
        "action": ACTION_HOLD,
        "action_label": ACTIONS[ACTION_HOLD],
        "headline": "数据不足，暂无法判断"
        if (quality or {}).get("sufficient") is False
        else "暂无明确结构信号",
        "reason": why,
        "price": None,
        "status": "",
        "signal_type": "",
        "divergence_status": "",
        "data_quality": quality or _data_quality({}),
        "disclaimer": "结构状态翻译，非投资建议",
    }


def build_history(
    events: Sequence[dict[str, Any]], *, epoch_ms: int | None = None
) -> dict[str, Any]:
    """把信号事件压成**可回看**的摘要（R45 P2）。

    ## 为什么要这个

    因子表在 R45 一天内切了 **4 次**、口径变过 2 次，但推荐是**当天才有的** ——
    「切表前推荐长什么样」这个问题，**当时没有答案**。
    幸而 ``cpt_signal_event`` 一直在记信号状态变迁（49 行 / 40 只票），
    配合 ``cpt_factor_epoch.switched_at`` 就能分清**旧口径 / 新口径**。

    ⇒ 至少让「这只票的判断在切表前后变没变」变成**看得见**的，
    而不是只能推断。

    :param events: :func:`cpt.storage.signal_event_store.load_signal_events` 的产物。
    :param epoch_ms: 口径切换点的毫秒时间戳；``None`` 表示不分组。
    """
    rows: list[dict[str, Any]] = []
    before = after = 0
    for e in events:
        t = e.get("transition_time")
        t_ms = int(t) if isinstance(t, int | float) else None
        legacy = bool(epoch_ms and t_ms and t_ms < epoch_ms)
        if epoch_ms and t_ms:
            before += 1 if legacy else 0
            after += 0 if legacy else 1
        rows.append(
            {
                "at_ms": t_ms,
                "status": e.get("status") or "",
                "signal_type": e.get("signal_type") or "",
                "price": _num(e.get("price")),
                "divergence_status": e.get("divergence_status") or "",
                "legacy": legacy,
            }
        )
    return {
        "available": bool(rows),
        "count": len(rows),
        "items": rows,
        "epoch_ms": epoch_ms,
        "legacy_count": before,
        "current_count": after,
    }
