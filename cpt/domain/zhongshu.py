"""笔中枢三笔重叠基础算法（纯 domain 计算）。

位于 ``docs/architecture.md`` §3.1 的 ``Bi[] → ZhongShu[]`` 环节：消费
:func:`cpt.domain.bi.build_bis` 产出的交替笔序列，按 ``docs/rules.md`` §7
（首版继承的「笔中枢三笔重叠基础算法」）与 §9.5（``zs_wzgx = zgd``）的口径
生成笔中枢，为走势类型（§8.1）与一买状态（§8.2）提供结构输入。

首版冻结口径：

1. **输入顺序即时间顺序**：只按 ``bis`` 给定顺序单遍扫描，不排序、不跳窗；
   空输入或不足三笔返回空元组。
2. **三笔建枢**：连续三笔方向两两相反（``+1`` / ``-1`` 交替）且三者价格区间
   存在共同重叠才生成中枢。重叠区间取 ``high = min(三笔 high)``、
   ``low = max(三笔 low)``，并要求**严格** ``high > low``——只在单点接触
   （``high == low``）不算中枢，避免产出零宽退化区间。
3. **中枢字段**：``start_time`` 取第一笔 ``start_time``、``end_time`` 取第三笔
   ``end_time``（延伸时随最后一笔后移）；``high`` / ``low`` 即重叠区间
   （``zg`` / ``zd``）；``bi_ids`` 优先取三笔各自的**首个** ``source_id`` 并保序
   去重（即「三项去重」）；三笔都没有 ``source_id`` 时，退化为三笔
   ``source_ids`` 拼接保序去重后的首个 id，仍为空则给出空元组。
4. **向后延伸**：后续笔与**当前**中枢区间重叠（``bi.high >= low`` 且
   ``bi.low <= high``，闭区间，与 ``zs_wzgx = zgd`` 的「高点比 ``zg``、低点比
   ``zd``」同构）时，更新 ``high = min(high, bi.high)``、
   ``low = max(low, bi.low)``、``end_time = bi.end_time``，并把该笔首个
   ``source_id`` 追加进 ``bi_ids``。更新后仍须保持 ``high > low``，否则该笔视为
   不重叠、延伸就此停止（闭区间只擦边时会出现这种情况）。
5. **延伸结束后的重扫**：不重叠时以结束笔（未能进入当前中枢的那一笔）起的最近
   三笔为新候选窗口继续扫描。已计入上一中枢的笔不重复使用，因此相邻中枢不共享
   笔——``docs/rules.md`` §8.1 的趋势判定以「两个中枢不构成重叠」为前提，若允许
   复用笔会产出时间区间重叠甚至嵌套的中枢，破坏该前提并重复计入同一笔。
6. **级别**：``level=None`` 时要求全部输入笔同级别（否则 ``ValueError``），中枢
   继承该级别；显式 ``level``（``>= 0``）时覆盖。延伸判定只看区间重叠，不再
   重复校验级别——``level=None`` 下全部笔同级，显式 ``level`` 下级别由调用方
   统一声明，两种情况都不会把不同级别的笔混进同一中枢。

不在本模块范围内：``zs_wzgx`` 档位计算（中枢之间的位置关系判定属于走势类型
阶段）、中枢合并/扩展口径、背驰比较字段。本函数对输入只读，不修改 ``Bi``；
重复调用同输入必同输出。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from cpt.domain.models import Bi, ZhongShu

__all__ = ["build_zhongshus"]

#: ``Bi.direction`` 合法取值：向上笔 / 向下笔。
_DIRECTIONS: Final[tuple[int, int]] = (1, -1)


def _is_alternating(first: Bi, second: Bi, third: Bi) -> bool:
    """三笔方向是否两两相反（``+1 / -1`` 交替）。"""
    return first.direction != second.direction and second.direction != third.direction


def _overlap(first: Bi, second: Bi, third: Bi) -> tuple[float, float] | None:
    """三笔共同重叠区间 ``(high, low)``；无重叠（``high <= low``）返回 ``None``。"""
    high = min(first.high, second.high, third.high)
    low = max(first.low, second.low, third.low)
    if high <= low:
        return None
    return high, low


def _covers(bi: Bi, high: float, low: float) -> bool:
    """笔区间是否与中枢闭区间重叠（``zs_wzgx = zgd``：高点比 ``zg``、低点比 ``zd``）。"""
    return bi.high >= low and bi.low <= high


def _bi_id(bi: Bi) -> str | None:
    """笔的首个 ``source_id``；该笔没有 ``source_id`` 时返回 ``None``。"""
    return bi.source_ids[0] if bi.source_ids else None


def _initial_ids(first: Bi, second: Bi, third: Bi) -> tuple[str, ...]:
    """中枢初始 ``bi_ids``：三笔首个 ``source_id`` 保序去重，全缺时退化取首个 id。"""
    firsts = tuple(dict.fromkeys(i for i in (_bi_id(b) for b in (first, second, third)) if i))
    if firsts:
        return firsts
    combined = tuple(dict.fromkeys(sid for bi in (first, second, third) for sid in bi.source_ids))
    return combined[:1]


def _resolve_level(bis: Sequence[Bi], level: int | None) -> int:
    """解析中枢级别：显式 ``level`` 覆盖；否则要求全部输入笔同级别。"""
    if level is not None:
        return level
    levels = sorted({bi.level for bi in bis})
    if len(levels) > 1:
        raise ValueError(f"未显式指定 level 时全部笔必须同级别, 实测 {levels}")
    return levels[0]


def _extend(bis: Sequence[Bi], end: int, high: float, low: float) -> tuple[int, float, float]:
    """从 ``end`` 起向后延伸中枢，返回 ``(新的开区间下标, high, low)``。

    逐笔按输入顺序尝试：与当前区间闭区间重叠且更新后仍严格 ``high > low`` 就
    纳入并收缩区间，否则停下（该笔不属于本中枢）。区间收缩是单调的，因此后续
    笔一律与收缩后的最新区间比较。
    """
    while end < len(bis):
        candidate = bis[end]
        if not _covers(candidate, high, low):
            break
        next_high = min(high, candidate.high)
        next_low = max(low, candidate.low)
        if next_high <= next_low:
            break
        high, low = next_high, next_low
        end += 1
    return end, high, low


def build_zhongshus(bis: Sequence[Bi], level: int | None = None) -> tuple[ZhongShu, ...]:
    """把交替笔序列压缩成笔中枢，返回不可变 :class:`ZhongShu` 元组。

    :param bis: 按时间升序排列的笔序列（``build_bis`` 的输出，或任何 ``Bi``
        序列）；空输入或不足三笔返回空元组。
    :param level: 中枢级别。``None`` 时要求全部输入笔同级别并继承之；显式传入
        时覆盖，必须 ``>= 0``。
    :raises ValueError: 显式 ``level < 0``；存在 ``direction`` 不是 ``1`` / ``-1``
        的笔；``level=None`` 且输入笔级别不一致。
    :returns: 按建枢顺序排列的中枢元组。每个中枢由连续三笔的重叠区间定义，
        后续重叠笔使区间收缩、``end_time`` 后移、``bi_ids`` 追加；不重叠时从
        该结束笔起重新扫描新窗口，笔不跨中枢复用。本函数不计算 ``zs_wzgx``
        档位，不做中枢合并，不生成走势类型与信号。
    """
    if level is not None and level < 0:
        raise ValueError(f"level 必须 >= 0 或 None, 实测 {level}")
    for bi in bis:
        if bi.direction not in _DIRECTIONS:
            raise ValueError(f"Bi.direction 必须是 1 或 -1, 实测 {bi.direction!r}")
    if len(bis) < 3:
        return ()

    zs_level = _resolve_level(bis, level)
    zhongshus: list[ZhongShu] = []
    start = 0
    while start + 3 <= len(bis):
        first, second, third = bis[start], bis[start + 1], bis[start + 2]
        region = _overlap(first, second, third) if _is_alternating(first, second, third) else None
        if region is None:
            start += 1
            continue

        high, low = region
        end, high, low = _extend(bis, start + 3, high, low)
        bi_ids = _initial_ids(first, second, third)
        for bi in bis[start + 3 : end]:
            bi_id = _bi_id(bi)
            if bi_id is not None and bi_id not in bi_ids:
                bi_ids = (*bi_ids, bi_id)
        zhongshus.append(
            ZhongShu(
                level=zs_level,
                start_time=first.start_time,
                end_time=bis[end - 1].end_time,
                high=high,
                low=low,
                bi_ids=bi_ids,
            )
        )
        # 延伸结束于 end：该笔未能进入本中枢，从它起扫描新的候选窗口。
        # 不复用已计入中枢的笔，避免相邻中枢时间重叠/嵌套并重复计入同一笔。
        start = end

    return tuple(zhongshus)
