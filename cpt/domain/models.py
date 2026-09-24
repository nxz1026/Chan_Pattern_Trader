"""不可变领域对象。

本模块定义 CPT 结构引擎的核心值对象。所有对象均为 ``@dataclass(frozen=True,
slots=True)`` 的不可变数据类，构造后不得修改；结构演进通过 `revision` 递增与
`StructureEvent` 事件追加表达（见 ``docs/rules.md`` §8.6），而非原地改写。

``CanonicalBar`` 实现 ``BarLike`` 协议（``cpt.domain.types``），使 K 线可被
分型/笔/中枢等结构元素算法跨级别复用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

FractalKind = Literal["top", "bottom"]
TrendKind = Literal[
    "consolidation", "trend", "extended", "forming", "reclassified", "closed", "open_end"
]
StructureKind = Literal["fractal", "bi", "zhongshu", "trend_type", "signal"]
StructureStatus = Literal["forming", "confirmed", "invalidated", "open_end"]
EventType = Literal["created", "updated", "confirmed", "reclassified", "invalidated", "closed"]
SignalStatus = Literal["structure_ready", "alert", "candidate", "confirmed", "invalidated"]
DivergenceStatus = Literal["not_checked", "not_detected", "detected"]  # noqa: F401  # re-exported for runtime_checkable

__all__ = [
    "CanonicalBar",
    "Fractal",
    "Bi",
    "ZhongShu",
    "TrendType",
    "StructureState",
    "StructureEvent",
    "Signal",
    "make_canonical_bar",
    "FractalKind",
    "TrendKind",
    "StructureKind",
    "StructureStatus",
    "EventType",
    "SignalStatus",
    "DivergenceStatus",
]


@dataclass(frozen=True, slots=True)
class CanonicalBar:
    """规范化 K 线（Binance USDT-M 永续 Kline 契约，``docs/rules.md`` §8.5）。

    通过 ``@property`` 暴露 ``BarLike`` 协议的 5 个属性（``open_time``、
    ``close_time``、``high``、``low``、``direction``），运行时可用
    ``isinstance(b, BarLike)`` 检查。

    ``direction`` 由 ``close`` 与 ``open`` 比较得出：``+1``=阳线、
    ``-1``=阴线、``0``=平盘。

    注意：此处**不继承** ``BarLike`` Protocol —— Protocol 的 ``@property``
    成员会被 dataclass 当作带默认值的类属性，导致 Python 3.14 dataclass 拒绝。
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

    def __post_init__(self) -> None:
        """数值 / 时间字段值域校验;非法值直接抛 ValueError 而非静默污染导出。"""
        if not (self.open_time < self.close_time):
            raise ValueError(
                f"open_time({self.open_time}) 必须严格小于 close_time({self.close_time})"
            )
        if not (self.high >= self.low):
            raise ValueError(f"high({self.high}) 必须 >= low({self.low})")
        for name in (
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "taker_buy_base_volume",
            "taker_buy_quote_volume",
        ):
            v = getattr(self, name)
            if not math.isfinite(v):
                raise ValueError(f"{name}({v!r}) 必须是有限数值, 不接受 NaN/inf")
        if self.trade_count < 0:
            raise ValueError(f"trade_count({self.trade_count}) 必须 >= 0")

    # BarLike 协议暴露
    @property
    def direction(self) -> int:
        if self.close > self.open:
            return 1
        if self.close < self.open:
            return -1
        return 0


@dataclass(frozen=True, slots=True)
class Fractal:
    """分型（缠论 K 线三分型）。"""

    kind: str
    level: int
    bar_index: int
    start_time: int
    end_time: int
    high: float
    low: float
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Bi:
    """笔（新笔）。

    后三个字段是**力度度量**（一买/一卖背驰比较用，见 :mod:`cpt.domain.first_buy`），
    默认 ``0`` 表示"未填充"：

    - ``power_price``：两端分型价格之差的绝对值，保留 2 位小数；
    - ``power_volume``：笔内**中间** K 线的成交量之和（不含两端）；
    - ``length``：笔的**去包含后** K 线根数。

    口径与 czsc ``BI.get_power_price`` / ``get_power_volume`` / ``get_length``
    对齐（``crates/czsc-core/src/objects/bi.rs``）。
    """

    level: int
    direction: int  # +1 | -1
    start_time: int
    end_time: int
    high: float
    low: float
    source_ids: tuple[str, ...]
    power_price: float = 0.0
    power_volume: float = 0.0
    length: int = 0


@dataclass(frozen=True, slots=True)
class ZhongShu:
    """笔中枢。"""

    level: int
    start_time: int
    end_time: int
    high: float
    low: float
    bi_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TrendType:
    """走势类型（新笔序列 + 笔中枢 + 连接笔，``docs/rules.md`` §8.1）。"""

    level: int
    # kind ∈ {consolidation, trend, extended, forming, reclassified, closed, open_end}
    kind: TrendKind
    direction: int
    start_time: int
    end_time: int
    high: float
    low: float
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StructureState:
    """结构当前状态（``docs/rules.md`` §8.6）。"""

    id: str
    level: int
    # kind ∈ {fractal, bi, zhongshu, trend_type, signal}
    kind: StructureKind
    direction: int
    start_time: int
    end_time: int
    # status ∈ {forming, confirmed, invalidated, open_end}
    status: StructureStatus
    revision: int
    first_seen_at: int
    confirmed_at: int | None
    invalidated_at: int | None
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StructureEvent:
    """结构事件（只追加，不删除，``docs/rules.md`` §8.6）。"""

    # event_type ∈ {created, updated, confirmed, reclassified, invalidated, closed}
    event_type: EventType
    structure_id: str
    revision: int
    payload: dict[str, object]
    occurred_at: int


@dataclass(frozen=True, slots=True)
class Signal:
    """一买信号（``docs/rules.md`` §8.6）。"""

    signal_id: str
    level: int
    # signal_type = "first_buy"
    signal_type: Literal["first_buy"]
    # status ∈ {structure_ready, alert, candidate, confirmed, invalidated}
    status: SignalStatus
    structure_id: str
    center_ids: tuple[str, ...]
    # divergence_status ∈ {not_checked, not_detected, detected}
    divergence_status: DivergenceStatus
    alert_time: int | None
    candidate_time: int | None
    confirmed_time: int | None
    invalidated_time: int | None
    price: float
    source_revision: int


def make_canonical_bar(
    open_time: int,
    open: float,
    high: float,
    low: float,
    close: float,
    close_time: int,
    *,
    volume: float = 0.0,
    quote_volume: float = 0.0,
    trade_count: int = 0,
    taker_buy_base_volume: float = 0.0,
    taker_buy_quote_volume: float = 0.0,
    is_closed: bool = True,
) -> CanonicalBar:
    """简化构造 ``CanonicalBar``。

    ``direction`` 无需传入，由 ``close``/``open`` 自动得出；成交量等字段提供默认值。
    """
    return CanonicalBar(
        open_time=open_time,
        open=open,
        high=high,
        low=low,
        close=close,
        volume=volume,
        close_time=close_time,
        quote_volume=quote_volume,
        trade_count=trade_count,
        taker_buy_base_volume=taker_buy_base_volume,
        taker_buy_quote_volume=taker_buy_quote_volume,
        is_closed=is_closed,
    )
