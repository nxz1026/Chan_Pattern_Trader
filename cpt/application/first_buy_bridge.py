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
   收盘确认需要盘中反向 K 线，本应由 :func:`transition_first_buy` 的
   ``reversal_closed`` 承担，但该函数当前**没有生产调用点**（P2-10），故本层
   按「出现即算」把 ``has_reversal_bi=True`` 交给 ``assess_first_buy`` ——
   后者据此直接给 ``confirmed``。
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

__all__ = [
    "FirstBuyFacts",
    "derive_first_buy_facts",
    "derive_first_sell_facts",
    "detect_structural_break",
]

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


def _scoped_structure_id(level: int, center_id: str, code: str) -> str:
    """结构 id：**必须带 code**，否则各票共用同一个 ``signal_id``。

    ``signal_id`` 是 ``f"{signal_type}:{level}:{structure_id}"``，而
    :func:`~cpt.storage.signal_event_store.load_previous_signal` **只按
    signal_id 查** —— 不带 code 时 ``level5:bi:8`` 同时属于 000002 与 600707
    （2026-10-08 线上实测 12 组 signal_id 跨股票共用），A 股读到的 ``previous``
    可能是 B 股那一行，状态机因此串味。

    ``code`` 为空（旧调用方 / 只测结构推导的单测）时保持旧格式，不静默改
    已有历史行的主键。
    """
    return f"level{level}:{center_id}:{code}" if code else f"level{level}:{center_id}"


def derive_first_buy_facts(
    *,
    level: int,
    trend_direction: int,
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    code: str = "",
) -> FirstBuyFacts | None:
    """由结构对象推导一买三事实。

    :param level: 评估级别；只取该级别的中枢与笔。
    :param trend_direction: 走势方向，必须 ``1`` 或 ``-1``。
    :param bis: 全部笔（按时间升序），本函数自行按 ``level`` 与时间筛选。
    :param zhongshus: 全部中枢，本函数自行按 ``level`` 与时间筛选。
    :param code: A 股 6 位代码；非空时写进 ``structure_id``（⇒ 写进 ``signal_id``），
        否则不同股票的同级别同位置结构会共用一行信号历史。默认空串 = 旧行为。
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
        structure_id = _scoped_structure_id(level, center_ids[1], code)
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
        divergence_status = _divergence_status(level_bis)

    return FirstBuyFacts(
        has_two_centers=has_two_centers,
        has_divergence_leg=has_divergence_leg,
        has_reversal_bi=has_reversal_bi,
        center_ids=center_ids,
        structure_id=structure_id,
        divergence_status=divergence_status,
    )


#: 段落切片长度上限（笔数）：与 czsc ``cxt_first_{buy,sell}_V221126`` 模板的
#: ``n`` 序列上界同值（``tests/test_first_buy.py:_NS`` 的 ``21``）。
_MAX_SEGMENT_LEN: int = 21


def _divergence_segment(level_bis: Sequence[Bi], *, direction: int) -> list[Bi]:
    """从``末笔``向前锚定出一段可用于 ``check_first_buy/sell`` 的笔段。

    **为什么必须重新锚定**（R69）：``check_first_buy`` 的口径是
    「**一整段**创新低的下跌」，要求首笔 high 为段内最高、末笔 low 为段内最低。
    原实现把「首个中枢开始之后的**全部**笔」整段传进去 —— 段内前面必然出现过
    反弹高点，``max(high) == bis[0].high`` 恒不成立。400 只 A 股实测：
    ``is_high_at_first`` 命中 **0/45**，即该判定在生产上**从未触发过**。
    而 czsc 官方模板的口径是从末尾切片（``bis[-n:]``，n 从 21 递减到 5，
    见 ``tests/test_first_buy.py:260 _our_hit_n``）——本函数与之一致。

    锚定规则：

    1. 末笔方向必须是 ``direction``，否则不是该方向的衰竭段 ⇒ 空段；
    2. 末笔 ``low``（买）/ ``high``（卖）必须是**整个序列**的极值 —— 即
       「这一跌/涨创了全段新低/新高」。不是极值 ⇒ 空段（还在半途，谈不上衰竭）；
    3. 从序列末尾向前，取**最长**的、笔数为奇数、且首笔为段内反向极值的段；
       长度上限 :data:`_MAX_SEGMENT_LEN`，与 czsc 模板同量纲；
    4. 找不到 ⇒ 空段（``not_detected``），不臆造段落。

    不改变判定器本身：``check_first_buy/sell`` 一行未动，czsc 逐段对照仍成立。
    """
    if not level_bis or level_bis[-1].direction != direction:
        return []
    last = level_bis[-1]
    if direction == _DOWN:
        if last.low != min(bi.low for bi in level_bis):
            return []
    elif last.high != max(bi.high for bi in level_bis):
        return []

    # 从最近处向前扩，取最长的合格段：首笔必须是当前段的反向极值。
    best: list[Bi] = []
    for length in range(3, min(len(level_bis), _MAX_SEGMENT_LEN) + 1, 2):
        segment = list(level_bis[-length:])
        if segment[0].direction != direction:
            continue
        if direction == _DOWN:
            if max(bi.high for bi in segment) != segment[0].high:
                continue
        elif min(bi.low for bi in segment) != segment[0].low:
            continue
        best = segment
    return best


def _divergence_status(level_bis: Sequence[Bi]) -> str:
    """背驰三态：``detected`` / ``not_detected`` / ``not_checked``（力度未填充）。

    ``level_bis`` 应传**本级别的全部笔**（按时间升序）；段落锚定由
    :func:`_divergence_segment` 负责，调用方不必自己切段。

    **判定顺序：先查数据缺，再判段落**。``length <= 0`` 是整段序列的全局
    事实，不该因为「恰好凑不出合格段落」就退化成 ``not_detected`` ——
    那会把「数据缺」谎报成「算过了，不背驰」。
    """
    if any(bi.length <= 0 for bi in level_bis):
        return "not_checked"
    segment = _divergence_segment(level_bis, direction=_DOWN)
    if not segment:
        return "not_detected"
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
    code: str = "",
) -> FirstBuyFacts | None:
    """由结构对象推导一卖三事实（``derive_first_buy_facts`` 的镜像）。

    :param code: 同 :func:`derive_first_buy_facts`：非空时写进 ``structure_id``，
        否则不同股票共用同一 ``signal_id``。默认空串 = 旧行为。
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
        structure_id = _scoped_structure_id(level, center_ids[1], code)
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
        divergence_status = _divergence_status_sell(level_bis)

    return FirstBuyFacts(
        has_two_centers=has_two_centers,
        has_divergence_leg=has_divergence_leg,
        has_reversal_bi=has_reversal_bi,
        center_ids=center_ids,
        structure_id=structure_id,
        divergence_status=divergence_status,
    )


def _divergence_status_sell(level_bis: Sequence[Bi]) -> str:
    """一卖背驰三态：``detected`` / ``not_detected`` / ``not_checked``。

    段落锚定走 :func:`_divergence_segment`，与一买**同一套规则**的镜像
    （末笔向上、末笔 high 为全段最高、首笔 low 为段内最低）；
    改前同样传「首个中枢之后的全部笔」，同属 R69 修掉的调用错位。
    """
    if any(bi.length <= 0 for bi in level_bis):
        return "not_checked"
    segment = _divergence_segment(level_bis, direction=_UP)
    if not segment:
        return "not_detected"
    try:
        return "detected" if check_first_sell(segment) else "not_detected"
    except ValueError:
        return "not_checked"


def detect_structural_break(bis: Sequence[Bi]) -> bool:
    """笔序列**首次**跌破前低 → 结构预警。

    扫描本级别笔序列（按 ``start_time`` 升序），找到第一根**向下**笔，其
    ``low`` 严格低于**前一根向下**笔的 ``low`` —— 即「首次跌破前低」。

    只比较向下笔（同向笔之间的低点递降才有结构意义），忽略向上笔。
    空序列 / 不足两根向下笔 → ``False``。
    """
    down_bis = [bi for bi in bis if bi.direction == _DOWN]
    for i in range(1, len(down_bis)):
        if down_bis[i].low < down_bis[i - 1].low:
            return True
    return False
