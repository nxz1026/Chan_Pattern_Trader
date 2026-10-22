"""把一买三事实翻译成「走到哪一步、卡在哪」的人话进度。

## 为什么要有这一层

R70（2026-10-20）与 R87（2026-10-22）两次实跑都测到：一买信号没有择时能力。
R87 全市场 5,131 只 A 股：「位置已到」之后 20 日平均 +0.56%、胜率 45.6%，
「反抽确认」之后 +0.57%、胜率 45.4%，同一批票随便挑一天 +0.22%、44.8% ——
**等确认没有额外收益**
（``docs/R70-ab-verification.md``）。经用户拍板（m01525），这个信号的用途
从「买点」改成「位置/风险提示」。用途一换，用户真正需要知道的就变成：

1. 这只票现在走到第几步了？
2. 卡在哪一步、为什么卡？
3. 已到位置时该做什么、不该做什么？

## 纪律：只做翻译，不发明结论

每一句人话都必须指回 :class:`~cpt.application.first_buy_bridge.FirstBuyFacts`
里的一个字段或 :func:`~cpt.domain.signal.assess_first_buy` 的一个分支。
本模块**不读行情、不算收益、不给目标价**，也不预测方向。想加一句新话，
先确认它能在 ``FirstBuyFacts`` 里找到对应字段 —— 找不到就不许写。

## 三步的定义（与 ``cpt.domain.rules`` §8.2 一一对应）

===========  ==========================  ==============================
步骤          判据                        对应字段
===========  ==========================  ==============================
① 两个中枢    同级中枢 ≥ 2                ``has_two_centers``
② 背驰段      中枢二之后仍有向下笔        ``has_divergence_leg``
③ 反向笔      背驰段之后出现向上笔        ``has_reversal_bi``
===========  ==========================  ==============================

``divergence_status`` 是**旁证**而非门槛（``assess_first_buy`` 不用它门控），
故单独进 ``evidence``，不占三步里的任何一步。
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = [
    "STEP_DONE",
    "STEP_FAILED",
    "STEP_PENDING",
    "first_buy_progress",
]

STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_PENDING = "pending"

#: 三步的稳定标识。前端按 id 对图标，**不要**按顺序猜。
_STEP_IDS: tuple[str, ...] = ("two_centers", "divergence_leg", "reversal_bi")

_STEP_TITLES: dict[str, str] = {
    "two_centers": "同级中枢满两个",
    "divergence_leg": "中枢二之后还在跌",
    "reversal_bi": "跌势里冒出一笔反弹",
}


class _Facts(Protocol):
    """只需要 ``FirstBuyFacts`` 的四个字段，故用协议而非具体类型。"""

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


def _steps(facts: _Facts) -> list[dict[str, Any]]:
    """三步判定。

    首个 ``failed`` 之后的步骤一律 ``pending`` —— 「没走到」不是「失败」，
    把未判定的步骤报成失败会让用户以为结构被否了两次。
    """
    flags = (
        bool(facts.has_two_centers),
        bool(facts.has_divergence_leg),
        bool(facts.has_reversal_bi),
    )
    steps: list[dict[str, Any]] = []
    blocked = False
    for step_id, passed in zip(_STEP_IDS, flags, strict=True):
        if blocked:
            state = STEP_PENDING
        elif passed:
            state = STEP_DONE
        else:
            state = STEP_FAILED
            blocked = True
        steps.append({"id": step_id, "title": _STEP_TITLES[step_id], "state": state})
    return steps


def _explain(facts: _Facts, steps: list[dict[str, Any]], segment_status: str) -> str:
    """按**第一个未完成的步骤**给一句人话。顺序与判定顺序一致。"""
    if segment_status == "up":
        return "现在不是下跌走势——向上走势里没有一买这回事，这里什么都不会报。"
    if segment_status == "unknown":
        return "数据不足，笔序列太短，还没法判断走到哪一步。"

    states = [step["state"] for step in steps]
    if states[0] == STEP_FAILED:
        return (
            "连第一步都没到：同级中枢还不到两个，说明这段走势还没有形成"
            "「跌—盘整—再跌」的形状，现在谈止跌位置太早。"
        )
    if states[1] == STEP_FAILED:
        return (
            "卡在第二步：两个中枢有了，但中枢二之后没有再往下走一笔——"
            "下跌已经停了，不存在「背驰段」，所以不构成底部位置。"
        )
    if states[2] == STEP_FAILED:
        return (
            "位置到了，但还没反弹：下跌背驰段走完了，价格停在低位。"
            "这一步的用处是提醒你别在恐慌低点割肉，不是叫你加仓。"
        )
    if facts.divergence_status == "detected":
        return (
            "三步全走完，且背驰判定成立（第二段跌得比第一段弱）。"
            "但 R87 实测这类结构之后 20 日平均 +0.57%、胜率 45.4%，"
            "它能证明跌势衰减，不能证明接下来会涨。"
        )
    if facts.divergence_status == "not_detected":
        return (
            "三步全走完，但背驰判定不成立——两段下跌的力度没有明显衰减，"
            "这更像下跌中继里的停顿，不是底部。"
        )
    return (
        "三步全走完，但背驰没算出来（数据不够），力度衰减与否未知。"
        "在这种状态下，结论只能停在「结构形状齐了」。"
    )


def _action(facts: _Facts, steps: list[dict[str, Any]], segment_status: str) -> str:
    """已到位置时该做什么。**只给动作，不给交易指令。**"""
    if segment_status != "down":
        return "不用做任何事：这个结构不成立，等它自己长出来。"
    states = [step["state"] for step in steps]
    if states[2] != STEP_DONE:
        return "不用做任何事：位置还没到，等第二步、第三步依次走完再来看。"
    return (
        "该做的：检查自己有没有在这个位置追空或恐慌割肉。"
        "不该做的：因为这个信号加仓——实测它没有预测力。"
        "同一个面板里，实测比它更有差别的是「收盘跌到前 20 日最低价的 96% 以下」"
        "（20 日 +4.37%、胜率 59.4%）—— 注意不是「随便跌了 4%」，"
        "后者样本多 23 倍、收益只剩 +1.07%。"
    )


def first_buy_progress(
    facts: _Facts | None,
    *,
    segment_status: str = "downlow_unavailable",
    segment_position: float | None = None,
) -> dict[str, Any]:
    """把一买结构事实翻成「走到哪一步 / 卡在哪 / 该做什么」。

    :param facts: :func:`~cpt.application.first_buy_bridge.derive_first_buy_facts`
        的返回值；``None`` 表示本轮**不适用**（向上走势，一买无意义）。
    :param segment_status: 走势方向档位，由调用方从 ``trend_direction`` 映射：
        ``down``（``trend_direction == -1``）/ ``up``（``== 1``）/
        ``unknown``（未提供）。本模块**不自己判走势**，只负责措辞。
    :param segment_position: 当前价 ÷ 本轮下跌起点价；``None`` 表示无法计算。
        只作为证据透传，不参与任何判定。
    :returns: 形如 ``{"steps": [...], "explain": str, "action": str,
        "evidence": {...}}`` 的只读字典。``steps`` 恒为三步、顺序固定。
    """
    if facts is None:
        steps = [
            {"id": sid, "title": _STEP_TITLES[sid], "state": STEP_PENDING} for sid in _STEP_IDS
        ]
        return {
            "steps": steps,
            "explain": "向上走势：一买只对下跌走势有意义，这里不评估。",
            "action": "不用做任何事。",
            "evidence": {"divergence_status": "not_checked", "segment_status": segment_status},
        }

    steps = _steps(facts)
    return {
        "steps": steps,
        "explain": _explain(facts, steps, segment_status),
        "action": _action(facts, steps, segment_status),
        "evidence": {
            "divergence_status": facts.divergence_status,
            "segment_status": segment_status,
            "segment_position": segment_position,
        },
    }
