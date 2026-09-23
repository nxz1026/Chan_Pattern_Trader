"""三根缠论K线分型识别（纯 domain 计算）。

位于 ``docs/architecture.md`` §3.1 的 ``缠论K线[] → Fractal[]`` 环节：消费
:func:`cpt.domain.contain.merge_contained_bars` 产出的缠论K线（或任何满足
:class:`~cpt.domain.types.BarLike` 的结构元素），识别原始三根窗口分型，为后续
新笔（``bi_type_new``）提供输入。

规则口径按 ``docs/rules.md`` §2 冻结的 ``fx_qy_middle`` + ``fx_qj_ck``：

1. **窗口连续三根**：只取输入序列中下标相邻的 ``(i-1, i, i+1)``，不做跳窗、
   不做补窗；输入不足 3 根时无窗口可用，返回空元组。
2. **严格不等**：顶分型要求中间 ``high`` 高于左右两根 ``high`` **且** 中间
   ``low`` 高于左右两根 ``low``；底分型要求中间 ``high`` 低于左右两根
   ``high`` **且** 中间 ``low`` 低于左右两根 ``low``。任一侧相等（含平盘、
   退化为同一区间）都不成型，保证方向唯一。
3. **``fx_qy_middle``**：分型高低区域取**中间那根**缠论K线的 ``high`` / ``low``，
   不使用三根窗口的极值，也不使用左右两根。
4. **``fx_qj_ck``**：分型区间取**中间那根**缠论K线的时间范围
   （``open_time`` / ``close_time``）；对包含处理后的缠论K线而言这是合并后的
   整体区间，对高级别结构元素而言即该结构元素自身的起止时间。
5. **原始窗口语义**：本函数只做三根窗口识别，相邻窗口可能产出同方向分型
   （例如连续两根中间 K 线各自满足顶分型），也可能产出未被用上的分型；
   间隔 ``≥1`` 根、端点交替、笔端点确认等过滤属于新笔阶段的职责，本模块
   不负责。

``docs/architecture.md`` §8 已冻结点 1：高级别分型**不做**包含合并，只做过滤。
因此本函数对输入只读，不合并、不改写，重复调用同输入必同输出。

``bar_index`` 取中间缠论K线的 ``source_indices`` 首项（原始K线下标），这是把
分型追溯回原始K线的唯一入口；若中间 bar 没有 ``source_indices``
（非 :class:`~cpt.domain.contain.MergedBar` 的自定义 ``BarLike``），退化为
中间窗口序号本身（此时该 bar 就是原始 bar，自身下标即其窗口下标）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Protocol, runtime_checkable

from cpt.domain.models import Fractal
from cpt.domain.types import BarLike

__all__ = ["detect_fractals"]

#: 分型窗口宽度：中间K线 + 左右各一根。
_WINDOW: Final[int] = 3

#: ``Fractal.kind`` 取值：顶分型 / 底分型。
_TOP: Final[str] = "top"
_BOTTOM: Final[str] = "bottom"

#: ``source_ids`` 前缀：缠论K线下标 / 原始K线下标。
_MERGED_PREFIX: Final[str] = "merged:"
_BAR_PREFIX: Final[str] = "bar:"


@runtime_checkable
class _SourceIndexed(BarLike, Protocol):
    """带 ``source_indices`` 的 bar（:class:`~cpt.domain.contain.MergedBar` 满足）。

    :class:`BarLike` 只承诺时间、高低与方向；把缠论K线追溯回原始K线所需的成分
    下标由本协议探测，缺失时按“自身即原始 bar”退化处理。
    """

    @property
    def source_indices(self) -> tuple[int, ...]: ...


def _component_indices(bar: BarLike, fallback: int) -> tuple[int, ...]:
    """取 bar 的成分原始下标；无下标或为空时退化为 ``(fallback,)``。"""
    if isinstance(bar, _SourceIndexed):
        indices = tuple(bar.source_indices)
        if indices:
            return indices
    return (fallback,)


def _classify(left: BarLike, middle: BarLike, right: BarLike) -> str | None:
    """判定三根窗口的分型方向，不成型返回 ``None``。

    严格不等：任一侧相等即不成型（``docs/rules.md`` §2 的严格定义口径）。
    """
    if (
        middle.high > left.high
        and middle.high > right.high
        and middle.low > left.low
        and middle.low > right.low
    ):
        return _TOP
    if (
        middle.high < left.high
        and middle.high < right.high
        and middle.low < left.low
        and middle.low < right.low
    ):
        return _BOTTOM
    return None


def _source_ids(merged_index: int, indices: tuple[int, ...]) -> tuple[str, ...]:
    """确定性分型 ID：缠论K线下标 + 中间 bar 的成分原始K线下标。"""
    return (_MERGED_PREFIX + str(merged_index),) + tuple(
        _BAR_PREFIX + str(index) for index in indices
    )


def detect_fractals(bars: Sequence[BarLike], level: int = 0) -> tuple[Fractal, ...]:
    """识别 ``bars`` 中的三根窗口分型，返回不可变 :class:`Fractal` 元组。

    :param bars: 时间升序的缠论K线序列（``merge_contained_bars`` 的输出，或任何
        :class:`~cpt.domain.types.BarLike` 序列，例如高级别结构元素）；长度不足
        3 或为空时返回空元组。
    :param level: 分型所属级别，写入每个 ``Fractal.level``；必须 ``>= 0``。
    :raises ValueError: ``level < 0``。
    :returns: 按中间 bar 输入下标升序的分型元组；每项区间为该中间 bar 的时间范围，
        高低为该中间 bar 的 ``high`` / ``low``，``source_ids`` 首项为
        ``merged:<中间下标>``，其后依次为中间 bar 的成分原始K线下标。

    只做原始三根窗口识别：不对相邻/同向分型做过滤，不判断笔端点，不做包含合并。
    """
    if level < 0:
        raise ValueError(f"level 必须 >= 0, 实测 {level}")
    if len(bars) < _WINDOW:
        return ()

    fractals: list[Fractal] = []
    for middle_index in range(1, len(bars) - 1):
        middle = bars[middle_index]
        kind = _classify(bars[middle_index - 1], middle, bars[middle_index + 1])
        if kind is None:
            continue
        indices = _component_indices(middle, middle_index)
        fractals.append(
            Fractal(
                kind=kind,
                level=level,
                bar_index=indices[0],
                start_time=middle.open_time,
                end_time=middle.close_time,
                high=middle.high,
                low=middle.low,
                source_ids=_source_ids(middle_index, indices),
            )
        )
    return tuple(fractals)
