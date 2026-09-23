"""新笔候选过滤（``bi_type_new``，纯 domain 计算）。

位于 ``docs/architecture.md`` §3.1 的 ``Fractal[] → Bi[]`` 环节：消费
:func:`cpt.domain.fractal.detect_fractals` 产出的原始三根窗口分型，按首版
「新笔」口径把分型序列压缩成交替的端点序列，为笔中枢与走势类型提供输入。

规则口径按 ``docs/rules.md`` §2（首版采用新笔 ``bi_type_new``）与 §9.7：

1. **输入顺序即时间顺序**：只按 ``fractals`` 给定顺序单遍扫描，不排序、
   不跳窗；空输入返回空元组。
2. **端点交替**：只有 ``kind`` 相反的两个端点才形成一笔——``bottom → top``
   为 ``direction=+1``，``top → bottom`` 为 ``direction=-1``。同类分型之间
   不直接成笔。
3. **同类取极端**：与当前端点 ``kind`` 相同的分型，只在更极端时替换——
   顶保留 ``high`` 更高者、底保留 ``low`` 更低者；极值相等保留较早者
   （严格不等，结果与扫描顺序唯一对应）。替换即最后一笔端点后移，与更早的
   异类端点重新成笔，这是新笔端点延伸的正常语义。
4. **笔区间**：``start_time`` / ``end_time`` 取两端的 ``start_time`` /
   ``end_time``（按输入顺序）；``high`` / ``low`` 取两端高低价的极值；
   ``source_ids`` 为两端 ``source_ids`` 按顺序拼接后去重（保序）。
5. **``level``**：``level=None`` 时继承端点 ``level``，两端 ``level`` 不同则
   报错；显式传入 ``level``（``>= 0``）时覆盖端点 ``level``。

本模块**不**实现最小跨度 / 「至少 5 根K线」门槛：``docs/rules.md`` §9.7 已明确
该参数是高级别递归的工程参数（``RulesConfig.min_elements_for_higher_bi``），
由递归层负责，不属于最底层笔模块。首版笔过滤只做端点交替与同类极端保留。

本函数对输入只读，不修改 ``Fractal``；重复调用同输入必同输出。
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise
from typing import Final

from cpt.domain.models import Bi, Fractal

__all__ = ["build_bis"]

#: ``Fractal.kind`` 取值：顶分型 / 底分型。
_TOP: Final[str] = "top"
_BOTTOM: Final[str] = "bottom"


def _is_more_extreme(candidate: Fractal, current: Fractal) -> bool:
    """同类端点 ``candidate`` 是否比 ``current`` 更极端。

    严格不等：``high`` / ``low`` 相等时返回 ``False``，保留较早的 ``current``。
    调用方保证两者 ``kind`` 相同且已通过取值校验。
    """
    if candidate.kind == _TOP:
        return candidate.high > current.high
    return candidate.low < current.low


def _direction(start: Fractal) -> int:
    """由笔起点分型方向推出笔方向：底起为向上笔，顶起为向下笔。"""
    return 1 if start.kind == _BOTTOM else -1


def _merge_ids(first: Fractal, second: Fractal) -> tuple[str, ...]:
    """两端点 ``source_ids`` 按顺序拼接并去重（保序，确定性）。"""
    return tuple(dict.fromkeys((*first.source_ids, *second.source_ids)))


def _resolve_level(start: Fractal, end: Fractal, level: int | None) -> int:
    """确定笔级别：显式 ``level`` 覆盖两端；否则继承并要求两端一致。"""
    if level is not None:
        return level
    if start.level != end.level:
        raise ValueError(f"两端分型 level 不同且未显式指定 level: {start.level} != {end.level}")
    return start.level


def _make_bi(start: Fractal, end: Fractal, level: int | None) -> Bi:
    """用两个异类端点构造一笔（不做跨度/根数门槛，由调用方保证交替）。"""
    return Bi(
        level=_resolve_level(start, end, level),
        direction=_direction(start),
        start_time=start.start_time,
        end_time=end.end_time,
        high=max(start.high, end.high),
        low=min(start.low, end.low),
        source_ids=_merge_ids(start, end),
    )


def build_bis(fractals: Sequence[Fractal], level: int | None = None) -> tuple[Bi, ...]:
    """把分型序列压缩成交替端点的候选新笔序列，返回不可变 :class:`Bi` 元组。

    :param fractals: 按时间升序排列的分型序列（``detect_fractals`` 的输出，
        或任何 ``Fractal`` 序列）；空输入返回空元组。
    :param level: 笔级别。``None`` 时继承端点 ``Fractal.level``，两端不一致则
        报错；显式传入时覆盖端点级别，必须 ``>= 0``。
    :raises ValueError: 存在 ``kind`` 不是 ``"top"`` / ``"bottom"`` 的分型；
        ``level < 0``；``level=None`` 时某笔两端分型 ``level`` 不同。
    :returns: 按端点确认顺序排列的笔元组；相邻同类分型中较不极端者被丢弃，
        更极端者替换端点并使最后一笔端点后移。

    只做端点交替与同类极端保留：不做最小跨度 / 「至少 5 根K线」门槛，不做
    分型包含合并，不做笔中枢与走势类型判定。
    """
    if level is not None and level < 0:
        raise ValueError(f"level 必须 >= 0 或 None, 实测 {level}")

    anchors: list[Fractal] = []
    for fractal in fractals:
        if fractal.kind != _TOP and fractal.kind != _BOTTOM:
            raise ValueError(f"Fractal.kind 必须是 'top' 或 'bottom', 实测 {fractal.kind!r}")
        if not anchors:
            anchors.append(fractal)
            continue
        current = anchors[-1]
        if fractal.kind == current.kind:
            if _is_more_extreme(fractal, current):
                anchors[-1] = fractal
        else:
            anchors.append(fractal)

    return tuple(_make_bi(start, end, level) for start, end in pairwise(anchors))
