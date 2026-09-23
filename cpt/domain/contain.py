"""缠论K线包含处理（纯 domain 计算）。

位于 ``docs/architecture.md`` §6 的 ``BarLike[] → 缠论K线[]`` 环节：为后续正式
分型 / 新笔算法提供稳定输入。规则口径按 ``docs/rules.md`` §7 直接采用参考实现
的「缠论K线包含处理」，不做 CPT 自创。

规则要点：

1. **包含判定**：相邻两根 bar 的价格区间互相覆盖即合并为一根
   （``a`` 包住 ``b`` 或 ``b`` 包住 ``a``，端点相等算包含）；
2. **合并方向**：由最近两个非包含 bar 的高低同时上移 / 同时下移推断
   （``+1`` 向上、``-1`` 向下）。无法判断（首根 bar、或只有一侧移动）时
   沿用当前方向；``direction`` 参数仅在还没有任何推断结果时作为 tie-break；
3. **合并取值**：向上取 ``high=max``、``low=max``；向下取 ``high=min``、
   ``low=min``；
4. **合并保留**：最早 ``open_time``、最晚 ``close_time``、第一根 ``open``、
   最后一根 ``close``，量能字段（``volume`` / ``quote_volume`` / ``trade_count``
   / ``taker_buy_base_volume`` / ``taker_buy_quote_volume``）累加，
   ``source_indices`` 拼接成分 bar 的原始下标；
5. **``is_closed``**：取最后一根成分 bar 的值（合并 bar 的结束时间由它决定）。

本模块只依赖 :mod:`cpt.domain.types`，不引入 oracle / 适配层 / 应用层；输入既
可以是 K 线（``CanonicalBar``），也可以是任何满足 :class:`~cpt.domain.types.BarLike`
的结构元素——后者没有 OHLC 与量能字段，按中性映射（``open=close=(high+low)/2``、
量能为 0、``is_closed=True``）补齐，不虚构方向。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from cpt.domain.types import BarLike

__all__ = [
    "MergedBar",
    "merge_contained_bars",
]

#: 允许的合并方向口径（``RulesConfig.contain_direction`` 的取值集合）。
_CONTAIN_DIRECTIONS: Final[tuple[str, str]] = ("forward", "backward")

#: 方向状态：向上 / 向下 / 未定。
_UP: Final[int] = 1
_DOWN: Final[int] = -1
_FLAT: Final[int] = 0

#: ``direction`` 参数在方向尚未推断出来时的初始 tie-break。
_INITIAL_TREND: Final[dict[str, int]] = {"forward": _UP, "backward": _DOWN}


@runtime_checkable
class _DetailedBar(BarLike, Protocol):
    """带 OHLC 与量能的 bar（``CanonicalBar`` / :class:`MergedBar` 均满足）。

    :class:`BarLike` 只承诺时间、高低与方向，合并所需的开收盘价和量能字段由本
    协议探测；不满足时走 :func:`_extras` 的中性映射。
    """

    @property
    def open(self) -> float: ...

    @property
    def close(self) -> float: ...

    @property
    def volume(self) -> float: ...

    @property
    def quote_volume(self) -> float: ...

    @property
    def trade_count(self) -> int: ...

    @property
    def taker_buy_base_volume(self) -> float: ...

    @property
    def taker_buy_quote_volume(self) -> float: ...

    @property
    def is_closed(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class MergedBar:
    """包含处理后的缠论K线（不可变）。

    通过 ``@property`` 暴露 ``BarLike`` 的 5 个属性（``open_time``、
    ``close_time``、``high``、``low``、``direction``），可用
    ``isinstance(bar, BarLike)`` 检查。

    ``direction`` 是派生属性而非字段：由 ``close`` 与 ``open`` 比较得出，
    ``+1``=阳线、``-1``=阴线、``0``=平盘；合并方向（趋势）本身体现在
    ``high`` / ``low`` 的取值上，不另设字段。

    ``source_indices`` 记录成分 bar 在输入序列中的下标（升序、只增不减），
    用于把缠论K线追溯回原始K线，这是后续分型 / 新笔回溯的唯一入口。

    注意：与 ``CanonicalBar`` 同理，此处**不继承** ``BarLike`` Protocol ——
    Protocol 的 ``@property`` 成员会被 dataclass 当作带默认值的类属性。
    """

    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float
    trade_count: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float
    is_closed: bool
    source_indices: tuple[int, ...]

    # BarLike 协议暴露
    @property
    def direction(self) -> int:
        """由 ``close`` / ``open`` 比较得出：``+1`` 阳、``-1`` 阴、``0`` 平。"""
        if self.close > self.open:
            return 1
        if self.close < self.open:
            return -1
        return 0


@dataclass(frozen=True, slots=True)
class _Extras:
    """合并时需要但不属于 :class:`BarLike` 的字段快照。"""

    open: float
    close: float
    volume: float
    quote_volume: float
    trade_count: int
    taker_buy_base_volume: float
    taker_buy_quote_volume: float
    is_closed: bool


def _extras(bar: BarLike) -> _Extras:
    """取 bar 的 OHLC / 量能；纯结构元素缺失这些字段时按中性值补齐。"""
    if isinstance(bar, _DetailedBar):
        return _Extras(
            open=bar.open,
            close=bar.close,
            volume=bar.volume,
            quote_volume=bar.quote_volume,
            trade_count=bar.trade_count,
            taker_buy_base_volume=bar.taker_buy_base_volume,
            taker_buy_quote_volume=bar.taker_buy_quote_volume,
            is_closed=bar.is_closed,
        )
    mid = (bar.high + bar.low) / 2.0
    return _Extras(
        open=mid,
        close=mid,
        volume=0.0,
        quote_volume=0.0,
        trade_count=0,
        taker_buy_base_volume=0.0,
        taker_buy_quote_volume=0.0,
        is_closed=True,
    )


def _is_contained(a: BarLike, b: BarLike) -> bool:
    """a 与 b 是否互相包含（含端点相等的退化情形）。"""
    if a.high >= b.high and a.low <= b.low:
        return True
    return a.high <= b.high and a.low >= b.low


def _infer_trend(previous: BarLike, current: BarLike) -> int:
    """由相邻两根非包含 bar 推断趋势：高低同时上移 → ``+1``，同时下移 → ``-1``。"""
    if current.high > previous.high and current.low > previous.low:
        return _UP
    if current.high < previous.high and current.low < previous.low:
        return _DOWN
    return _FLAT


def _as_merged(bar: BarLike, index: int) -> MergedBar:
    """把单根 bar 原样映射为 :class:`MergedBar`。"""
    extras = _extras(bar)
    return MergedBar(
        open_time=bar.open_time,
        open=extras.open,
        high=bar.high,
        low=bar.low,
        close=extras.close,
        volume=extras.volume,
        close_time=bar.close_time,
        quote_volume=extras.quote_volume,
        trade_count=extras.trade_count,
        taker_buy_base_volume=extras.taker_buy_base_volume,
        taker_buy_quote_volume=extras.taker_buy_quote_volume,
        is_closed=extras.is_closed,
        source_indices=(index,),
    )


def _merge_pair(accumulated: MergedBar, bar: BarLike, index: int, trend: int) -> MergedBar:
    """按 ``trend`` 把 ``bar`` 合并进 ``accumulated``。"""
    extras = _extras(bar)
    if trend == _UP:
        high = max(accumulated.high, bar.high)
        low = max(accumulated.low, bar.low)
    else:
        high = min(accumulated.high, bar.high)
        low = min(accumulated.low, bar.low)
    return MergedBar(
        open_time=min(accumulated.open_time, bar.open_time),
        open=accumulated.open,
        high=high,
        low=low,
        close=extras.close,
        volume=accumulated.volume + extras.volume,
        close_time=max(accumulated.close_time, bar.close_time),
        quote_volume=accumulated.quote_volume + extras.quote_volume,
        trade_count=accumulated.trade_count + extras.trade_count,
        taker_buy_base_volume=accumulated.taker_buy_base_volume + extras.taker_buy_base_volume,
        taker_buy_quote_volume=accumulated.taker_buy_quote_volume + extras.taker_buy_quote_volume,
        is_closed=extras.is_closed,
        source_indices=accumulated.source_indices + (index,),
    )


def merge_contained_bars(
    bars: Sequence[BarLike],
    direction: str = "forward",
) -> tuple[MergedBar, ...]:
    """对 ``bars`` 做缠论K线包含处理，返回合并后的缠论K线序列。

    :param bars: 时间升序的输入序列（``CanonicalBar`` 或任意 ``BarLike``）；
        不足 2 根时原样逐根映射为 :class:`MergedBar`。
    :param direction: 合并方向口径 tie-break，只接受 ``"forward"``（向上，
        ``high/low`` 同取高）或 ``"backward"``（向下，``high/low`` 同取低）；
        其他取值抛 ``ValueError``。
    :returns: 合并后的缠论K线元组；相邻元素之间不再互为包含关系。

    每根输出的 ``source_indices`` 给出其在 ``bars`` 中的成分下标，顺序与
    ``bars`` 一致且互不重叠、并集覆盖全部输入下标。
    """
    if direction not in _CONTAIN_DIRECTIONS:
        raise ValueError(
            f"direction 必须是 {_CONTAIN_DIRECTIONS[0]!r} 或 {_CONTAIN_DIRECTIONS[1]!r} 之一, "
            f"实测 {direction!r}"
        )
    if not bars:
        return ()

    merged: list[MergedBar] = [_as_merged(bars[0], 0)]
    trend = _INITIAL_TREND[direction]
    for index in range(1, len(bars)):
        bar = bars[index]
        last = merged[-1]
        if _is_contained(last, bar):
            merged[-1] = _merge_pair(last, bar, index, trend)
        else:
            merged.append(_as_merged(bar, index))
        if len(merged) >= 2:
            inferred = _infer_trend(merged[-2], merged[-1])
            if inferred != _FLAT:
                trend = inferred
    return tuple(merged)
