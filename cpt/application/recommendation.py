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

## 措辞纪律：只描述**结构状态**，不给祈使动作（R76）

看板全程标注「只读 · 不构成投资建议」。R45 时的做法是给动作词加「结构」后缀
（``买入结构`` / ``卖出结构``），想把「建议买卖」和「结构状态」分开。

**实测这样分不开**：推荐卡上那个 26px 加粗字、飞书推送的第一行都是
``action_label``，用户看到「买入结构」就会当成「买入」。
而 R70 与 R87 两次实跑都测到这个信号**没有预测力**（见
``docs/rebuild-plan-review.md`` §5.0.1 与 §8：R87 全市场 5,131 只 A 股，
「位置已到」20 日 +0.56% / 胜率 45.6%，「反抽确认」+0.57% / 45.4%，
同一批票随便挑一天 +0.22% / 44.8% —— 等确认没有额外收益）。

⇒ R76 起 ``ACTIONS`` 只描述**价格处在什么位置**（「下跌位置已到」），
``headline`` 自带「不是买点」，``reason`` 里的状态词走 :data:`STATUS_TEXT`。
``用户可见的每一句都不能是祈使句`` —— 这是本模块现在的措辞红线，
由 ``tests/test_recommendation.py::test_no_buy_language_anywhere`` 钉住。

## 码值不动，只动文案

``action``（``buy``/``sell``/…）进了 ``cpt_track_snapshot`` 的 payload、
``/api/trade/*`` 的决策批次、``a_share_routes._RECOMMENDATION_KEYS``，
前端着色也读 ``data-action``。改码值会动历史数据与下游契约，
所以 R76 **只换** ``action_label`` / ``headline`` / ``reason`` 的文案。
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
    "STATUS_TEXT",
    "SIGNAL_TYPE_TEXT",
    "DIVERGENCE_TEXT",
    "status_text",
    "action_text",
    "divergence_text",
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

#: R76：动作词**去掉买点语气**。
#:
#: 改前是「买入结构 / 卖出结构 / 关注 / 观望」。问题不在「建议」二字被写出了没有，
#: 而在**动作词本身就是祈使句**：推荐卡上那个 26px 加粗字是整页最大的字，
#: PUSH 推送第一行也是它 —— 用户一眼看到「买入结构」就会照着做，
#: 而 R70 已经实测过它没有预测力（见 ``docs/rebuild-plan-review.md`` §5.0.1）。
#:
#: 现在的词只描述**结构状态**，不描述该做什么：
#:
#: ========= =========================================================
#: buy       下跌位置已到（原「买入结构」）
#: sell      上涨位置已到（原「卖出结构」）
#: watch     位置已到，还没确认（原「关注」）
#: hold      没有位置提示 / 已作废（原「观望」）
#: ========= =========================================================
#:
#: ⚠️ ``action`` 的**码值**（``buy``/``sell``/...）一个都没改 —— 它进了
#: ``cpt_track_snapshot`` 的 payload、``/api/trade/*`` 的决策批次与
#: ``a_share_routes._RECOMMENDATION_KEYS``，改码值会动历史数据与下游契约。
#: 前端着色也仍读 ``data-action="buy"``，只换文案不换键。
ACTIONS: Final[dict[str, str]] = {
    ACTION_BUY: "下跌位置已到",
    ACTION_SELL: "上涨位置已到",
    ACTION_WATCH: "位置已到，还没确认",
    ACTION_HOLD: "没有位置提示",
}

#: 信号状态 → 人话（R76）。
#:
#: 改前 ``reason`` 里是 ``f"信号状态：{status}"`` —— 直接把 ``structure_ready``
#: 这种英文枚举值甩给用户。R71 已经在前端 ``STATUS_LABELS`` 里翻了同一批词，
#: 这里是**后端**的同一张表：推送、追踪卡、LLM 提示词都读后端，前端翻译够不着。
STATUS_TEXT: Final[dict[str, str]] = {
    "confirmed": "反抽确认了一笔（不是买点）",
    "structure_ready": "位置已到（还没确认）",
    "candidate": "反抽 K 线已收盘，还站得住",
    "alert": "盘中冒了一根反抽 K 线，还没收盘",
    "invalidated": "结构已作废",
}

#: 信号类型 → 人话（R76）。``一买``/``一卖`` 是缠论术语，用户看不懂。
SIGNAL_TYPE_TEXT: Final[dict[str, str]] = {
    "first_buy": "下跌位置提示",
    "first_sell": "上涨位置提示",
}

#: 背驰三态 → 人话（R76）。前端 ``DIVERGENCE_LABELS`` 是同一张表的前端副本，
#: 后端出口（追踪卡 / 推送）读这一份。
DIVERGENCE_TEXT: Final[dict[str, str]] = {
    "detected": "两段力度在衰减（背驰）",
    "not_detected": "两段力度没有明显衰减",
    "not_checked": "还没算出来（数据不够）",
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
            reason = (
                f"{_label(sig_type)}条件不成立（没形成两段重合的价格区间）"
                "—— 「不知道」和「没有」是两回事：这里是真的没有。"
            )
        else:
            headline = "结构已作废"
            reason = (
                f"{_label(sig_type)}已作废（不是反向信号，也不该反着做）—— 之前那个判断不成立了。"
            )
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
        # R76：「已确认」单独出现等于在说「这下可以买了」。带上 R70 的实测数字，
        # 让这句话自己否掉这个理解 —— 后端所有出口（推送 / 追踪卡 / LLM 提示词）
        # 都读这一个字符串，改这里一处即全链路生效。
        headline = (
            f"{_label(sig_type)}：跌势里已经冒出一笔反弹"
            "（不是买点 —— R87 实测这类信号之后 20 日平均 +0.57%、胜率 45.4%，"
            "与「位置已到」的 +0.56% / 45.6% 几乎一样，等确认没有额外收益）"
        )
    elif status == "structure_ready":
        # 结构与方向都成立，只差反向笔确认。与「候选 / 预警」不是一回事，
        # 给一句能自解释的文案（此前这条状态会直接掉进「未知状态」降级）。
        action, headline = ACTION_WATCH, f"{_label(sig_type)}：位置到了，还差一笔反弹确认"
    elif status == "candidate":
        action, headline = ACTION_WATCH, f"{_label(sig_type)}：反抽 K 线收盘了，还站得住"
    else:
        action, headline = ACTION_WATCH, f"{_label(sig_type)}：盘中冒了一笔反弹，还没收盘"

    price = _num(signal.get("price"))
    if price is None:
        price = _last_close(snapshot)

    # R76：原来第一段是 ``f"信号状态：{status}"``（英文枚举直出）。
    reasons: list[str] = [f"现在这一步：{status_text(status)}"]
    if divergence == "detected":
        reasons.append("两段下跌的力度确实在衰减（背驰）")
    elif divergence == "not_detected":
        reasons.append("两段下跌的力度没有明显衰减 —— 更像下跌中继里的停顿")
    if price is not None:
        reasons.append(f"时点收盘价 {price:.2f}（你真正挂得出去的那个价）")

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
    """信号类型 → 中文人话（R76：不再叫「一买 / 一卖」）。"""
    return SIGNAL_TYPE_TEXT.get(sig_type, sig_type)


def status_text(status: str) -> str:
    """信号状态 → 中文人话。未知状态原样返回 —— **不猜**。"""
    return STATUS_TEXT.get(status, status)


def action_text(action: str) -> str:
    """动作码 → 中文人话。未知码原样返回。"""
    return ACTIONS.get(action, action)


def divergence_text(divergence: str) -> str:
    """背驰三态 → 中文人话。空串/未知值原样返回（**不猜**）。"""
    return DIVERGENCE_TEXT.get(divergence, divergence)


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
