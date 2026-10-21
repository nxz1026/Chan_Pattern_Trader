"""把「信号失效」翻译成一个可归类、可落库的原因码。

## 为什么需要它

R71 的进度条回答「现在走到哪一步」，但它**只对当下有效**。用户真正会问的下一个
问题是「这只票上次那个信号，为什么没了？」——那需要一条可以事后查阅的记录。

调研结论（2026-10-21，实测 ``public.cpt_signal_event`` 266 条失效行）：
**历史表读不回原因**。266 条里 265 条的 ``prev_status`` 是 ``None``（首次评估就
失效，没有「之前那个状态」可比较），且表里不存三条件的值。所以本模块的立场是：

1. **不反推历史** —— 没有证据的归因就是编造；
2. **在失效发生的那一刻算出来并落库**，让往后每一条失效都可追溯；
3. 落库**不加列**（用户反对冗余列）：三个原因恰好能从现有列还原，见下。
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = [
    "REASON_CODES",
    "REASON_TEXT",
    "classify_invalidation",
    "explain_invalidation",
    "invalidation_payload",
]

#: 失效原因码。**稳定标识**，前端按码对文案，不要按顺序猜。
REASON_CODES: tuple[str, ...] = (
    "not_in_downtrend",
    "not_enough_centers",
    "no_divergence_leg",
    "previous_invalidated",
)

#: 原因码 → 一句人话。与 :mod:`cpt.application.signal_progress` 的措辞同源，
#: 但**不复用**它的 ``_explain``：那边讲的是「卡在第二步行进中」，这边讲的是
#: 「这个结构已经作废了」，时态不同，混用会说错话。
REASON_TEXT: dict[str, str] = {
    "not_in_downtrend": (
        "失效原因：现在不是下跌走势。一买只对下跌走势有意义，"
        "方向不对就不评估——它消失不是因为跌完了，是因为这段走势不再是跌势。"
    ),
    "not_enough_centers": (
        "失效原因：同级中枢不到两个。这段走势没形成「跌—盘整—再跌」的形状，"
        "底部位置无从谈起。"
    ),
    "no_divergence_leg": (
        "失效原因：中枢二之后没有再往下走一笔。两个中枢有了，"
        "但下跌已经停了，不存在「背驰段」，所以不构成底部位置。"
    ),
    "previous_invalidated": (
        "失效原因：这个结构此前已被判定作废，本轮没有重新成立。"
        "作废是**终态**（``assess_first_buy`` 一旦进 invalidated 就不再回退），"
        "要重新出现位置提示，得等一个新结构。"
    ),
}


class _Facts(Protocol):
    """同 :class:`cpt.application.signal_progress._Facts`：只要四个字段。"""

    #: 全部用 ``@property`` 而不是裸字段：``FirstBuyFacts`` 是 **frozen dataclass**，
    #: 它的属性是只读的。裸字段声明意味着「可读写」，mypy 会判
    #: ``FirstBuyFacts | None`` 不满足 ``_Facts | None``（R76 实测 2 处报错）。
    #: 本模块只读这些字段，声明成只读正好、也没多写一行实现。
    @property
    def has_two_centers(self) -> bool: ...

    @property
    def has_divergence_leg(self) -> bool: ...

    @property
    def has_reversal_bi(self) -> bool: ...

    @property
    def divergence_status(self) -> str: ...


def classify_invalidation(
    *,
    trend_direction: int,
    facts: _Facts | None,
    previous_status: str | None = None,
) -> str | None:
    """判定本轮失效的原因码；**没失效就返回 ``None``**。

    ## 判定顺序（与状态机对得上，但**目的不同**）

    状态机 :func:`~cpt.domain.signal.assess_first_buy` 关心「结果是什么」，
    所以 ``previous == invalidated`` 排第一（进了终态就是 invalidated，不再看结构）。
    归因关心的是「**这次为什么不成立**」，所以结构原因必须优先：

    若照搬状态机的顺序，一个失效过的结构在本轮重算时永远只得到
    ``previous_invalidated``（实测 000006/000014 都是这样），答案退化成
    「以前失效过」—— 等于没回答。故顺序为：

    1. 方向不对 → ``not_in_downtrend``（最硬的原因，与方向无关的其它判断都不该覆盖它）
    2. 中枢不足 → ``not_enough_centers``
    3. 无背驰段 → ``no_divergence_leg``
    4. 结构三条件都满足却仍作废（只可能是终态没回退）→ ``previous_invalidated``

    :param trend_direction: ``1`` 向上 / ``-1`` 向下 / ``0`` 不知道。
    :param facts: 结构事实；``None`` 且方向为 ``-1`` 视为「事实没算出来」，
        此时归因证据不足，返回 ``None``（**不猜**）。
    :param previous_status: 上一轮状态；只在结构条件全满足时用来解释「为何仍作废」。
    :returns: :data:`REASON_CODES` 之一，或 ``None``（没失效 / 证据不足）。
    """
    # ① 方向不对（含 ``0`` = 不知道）：一买不评估。``facts is None`` 也走这里
    #    —— 向上走势下 bridge 本就返回 None，是同一件事的两种表达。
    if trend_direction != -1:
        return "not_in_downtrend"
    if facts is None:
        return None
    # ②③ 结构原因优先，理由见 docstring。
    if not facts.has_two_centers:
        return "not_enough_centers"
    if not facts.has_divergence_leg:
        return "no_divergence_leg"
    # ④ 结构都成立却仍作废：只剩「终态不回退」这一种解释。
    if previous_status == "invalidated":
        return "previous_invalidated"
    # 三步都过 ⇒ 不失效（confirmed / structure_ready）。
    return None


def explain_invalidation(reason: str | None) -> str:
    """原因码 → 一句人话。未知码返回**显式未知**，绝不返回空串冒充「没原因」。"""
    if reason is None:
        return "未失效。"
    return REASON_TEXT.get(reason, f"失效原因码 {reason!r} 未登记文案。")


def invalidation_payload(
    *,
    trend_direction: int,
    facts: _Facts | None,
    previous_status: str | None = None,
    segment_position: float | None = None,
) -> dict[str, Any] | None:
    """组装落库/展示用的失效归因；未失效返回 ``None``。

    返回的键刻意**只放能指回域内字段的东西**：``reason`` 能由
    (``trend_direction``, ``has_two_centers``, ``has_divergence_leg``,
    ``previous_status``) 唯一还原，``explain`` 是 ``reason`` 的纯函数。
    """
    reason = classify_invalidation(
        trend_direction=trend_direction, facts=facts, previous_status=previous_status
    )
    if reason is None:
        return None
    return {
        "reason": reason,
        "explain": explain_invalidation(reason),
        "evidence": {
            "trend_direction": trend_direction,
            "has_two_centers": None if facts is None else bool(facts.has_two_centers),
            "has_divergence_leg": None if facts is None else bool(facts.has_divergence_leg),
            "previous_status": previous_status,
            "segment_position": segment_position,
        },
    }
