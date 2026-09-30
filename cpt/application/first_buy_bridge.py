"""一买结构事实翻译层：结构 → :mod:`cpt.domain.signal` 状态机的入参。

**为什么需要这一层**（R20 新增）：``cpt.domain.signal.assess_first_buy`` 是状态机，
它把三个**结构事实布尔值**当**入参**（``has_two_centers`` /
``has_divergence_leg`` / ``has_reversal_bi``）。而生产侧的结构对象里没有这三个
字段——``ZhongShu``（``cpt/domain/models.py:151``）只有
``level/start_time/end_time/high/low/bi_ids``，``TrendType``（``:163``）更是连中枢数
都没有。这三个值**既不在数据里、也没被任何人算过**，所以这不是「接线」而是**补算**。

与 :mod:`cpt.domain.first_buy` 的分工：那边 ``check_first_buy`` 算的是 czsc 的
**笔段力度背驰**口径（要一整段创新低的 bis 段），本模块算的是缠论 §8.2 的
**结构三条件**，两者口径不同、都喂给状态机——前者决定
``divergence_status``，后者决定状态能否离开 ``invalidated``。

**保守口径**（R20 定案，四条都选了「宁可判否、不误报」的一侧）：

1. **两个中枢** = 本级别中枢按 ``start_time`` 升序后**最后两个**。不用「任意两个」：
   时间上相隔很远的中枢之间没有有效的一买结构。
2. **背驰段** = 中枢二 ``end_time`` **之后**、方向与 ``trend_direction`` 相同的第一笔。
   不含中枢连接笔——连接笔属于中枢本身，不算「离开中枢后的延续」。
3. **反向笔** = 背驰段之后第一笔方向相反的笔。**出现即算**，不要求收盘确认：
   收盘确认需要盘中反向 K 线，由 :func:`transition_first_buy` 的
   ``reversal_closed`` 承担，本层不重复把关。
4. **背驰不是硬门槛**。沿用 :func:`cpt.domain.signal._structure_ready` 的现状：
   ``divergence_status`` 只被记录，不参与状态门控。

**降级**（不静默）：``check_first_buy`` 在力度度量未填充时**响亮报错**
（``cpt/domain/first_buy.py:72 _require_power_metrics``）。本层捕获该 ``ValueError``
并降级为 ``not_checked``——信号结构照常评估，只是背驰未知。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cpt.domain.first_buy import check_first_buy, check_first_sell
from cpt.domain.models import Bi, ZhongShu

__all__ = ["FirstBuyFacts", "derive_first_buy_facts", "derive_first_sell_facts"]

#: 一买只对**向下**趋势有意义，与 ``signal._DOWN`` 同值。
_DOWN: int = -1
_UP: int = 1


@dataclass(frozen=True, slots=True)
class FirstBuyFacts:
    """三结构事实 + 状态机需要的身份字段。"""

    has_two_centers: bool
    has_divergence_leg: bool
    has_reversal_bi: bool
    center_ids: tuple[str, ...]
    structure_id: str
    divergence_status: str


def _center_id(zhongshu: ZhongShu) -> str:
    """中枢的稳定代表 id。

    ``ZhongShu`` 没有自己的 id 字段，只有 ``bi_ids``——中枢由其笔定义，故取首笔 id
    作为代表（与 ``Bi.source_ids`` / 合成标签同款的可追溯约定）。
    """
    return zhongshu.bi_ids[0] if zhongshu.bi_ids else f"zs@{zhongshu.start_time}"


def derive_first_buy_facts(
    *,
    level: int,
    trend_direction: int,
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
) -> FirstBuyFacts | None:
    """由结构对象推导一买三事实。

    :param level: 评估级别；只取该级别的中枢与笔。
    :param trend_direction: 走势方向，必须 ``1`` 或 ``-1``。
    :param bis: 全部笔（按时间升序），本函数自行按 ``level`` 与时间筛选。
    :param zhongshus: 全部中枢，本函数自行按 ``level`` 与时间筛选。
    :returns: ``None`` 表示**不适用**（``trend_direction != -1``，一买无意义）；
        否则返回结构事实。注意 ``has_*`` 全为 ``False`` 仍会返回对象——那是
        「评估了、结构不满足」，与 ``None`` 的「不评估」语义不同。
    :raises ValueError: ``trend_direction`` 不是 ``1`` / ``-1``。
    """
    if trend_direction not in (1, -1):
        raise ValueError(f"trend_direction 必须是 1 或 -1, 实测 {trend_direction!r}")
    if trend_direction != _DOWN:
        return None

    centers = sorted(
        (zs for zs in zhongshus if zs.level == level), key=lambda zs: (zs.start_time, zs.end_time)
    )
    level_bis = sorted(
        (bi for bi in bis if bi.level == level), key=lambda bi: (bi.start_time, bi.end_time)
    )
    has_two_centers = len(centers) >= 2

    center_ids: tuple[str, ...] = ()
    structure_id = ""
    has_divergence_leg = False
    has_reversal_bi = False
    divergence_status = "not_checked"

    if has_two_centers:
        second = centers[-1]
        center_ids = (_center_id(centers[-2]), _center_id(second))
        structure_id = f"level{level}:{center_ids[1]}"
        # 背驰段：中枢二结束之后、仍沿原方向运行的**第一笔**（保守口径第 2 条）。
        leg_index = next(
            (
                index
                for index, bi in enumerate(level_bis)
                if bi.start_time >= second.end_time and bi.direction == _DOWN
            ),
            None,
        )
        if leg_index is not None:
            has_divergence_leg = True
            # 反向笔：背驰段之后的第一笔反向笔（保守口径第 3 条，出现即算）。
            has_reversal_bi = any(bi.direction != _DOWN for bi in level_bis[leg_index + 1 :])
        divergence_status = _divergence_status(centers[-2], level_bis)

    return FirstBuyFacts(
        has_two_centers=has_two_centers,
        has_divergence_leg=has_divergence_leg,
        has_reversal_bi=has_reversal_bi,
        center_ids=center_ids,
        structure_id=structure_id,
        divergence_status=divergence_status,
    )


def _divergence_status(first_center: ZhongShu, level_bis: Sequence[Bi]) -> str:
    """背驰三态：``detected`` / ``not_detected`` / ``not_checked``（力度未填充）。"""
    segment = [bi for bi in level_bis if bi.end_time > first_center.start_time]
    try:
        return "detected" if check_first_buy(segment) else "not_detected"
    except ValueError:
        # ``_require_power_metrics`` 报的是「力度度量未填充」——这是**数据缺**，
        # 不是「不背驰」，必须与 not_detected 区分开，否则面板会显示错的结论。
        return "not_checked"


def derive_first_sell_facts(
    *,
    level: int,
    trend_direction: int,
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
) -> FirstBuyFacts | None:
    """由结构对象推导一卖三事实（``derive_first_buy_facts`` 的镜像）。

    :param trend_direction: 走势方向，必须 ``1`` 或 ``-1``。
    :returns: ``None`` 表示**不适用**（``trend_direction != 1``，一卖无意义）。
    """
    if trend_direction not in (1, -1):
        raise ValueError(f"trend_direction 必须是 1 或 -1, 实测 {trend_direction!r}")
    if trend_direction != _UP:
        return None

    centers = sorted(
        (zs for zs in zhongshus if zs.level == level), key=lambda zs: (zs.start_time, zs.end_time)
    )
    level_bis = sorted(
        (bi for bi in bis if bi.level == level), key=lambda bi: (bi.start_time, bi.end_time)
    )
    has_two_centers = len(centers) >= 2

    center_ids: tuple[str, ...] = ()
    structure_id = ""
    has_divergence_leg = False
    has_reversal_bi = False
    divergence_status = "not_checked"

    if has_two_centers:
        second = centers[-1]
        center_ids = (_center_id(centers[-2]), _center_id(second))
        structure_id = f"level{level}:{center_ids[1]}"
        # 背驰段：中枢二结束之后、仍沿原方向（向上）运行的**第一笔**。
        leg_index = next(
            (
                index
                for index, bi in enumerate(level_bis)
                if bi.start_time >= second.end_time and bi.direction == _UP
            ),
            None,
        )
        if leg_index is not None:
            has_divergence_leg = True
            # 反向笔：背驰段之后的第一笔反向笔（向下）。
            has_reversal_bi = any(bi.direction != _UP for bi in level_bis[leg_index + 1 :])
        divergence_status = _divergence_status_sell(centers[-2], level_bis)

    return FirstBuyFacts(
        has_two_centers=has_two_centers,
        has_divergence_leg=has_divergence_leg,
        has_reversal_bi=has_reversal_bi,
        center_ids=center_ids,
        structure_id=structure_id,
        divergence_status=divergence_status,
    )


def _divergence_status_sell(first_center: ZhongShu, level_bis: Sequence[Bi]) -> str:
    """一卖背驰三态：``detected`` / ``not_detected`` / ``not_checked``。"""
    segment = [bi for bi in level_bis if bi.end_time > first_center.start_time]
    try:
        return "detected" if check_first_sell(segment) else "not_detected"
    except ValueError:
        return "not_checked"
