"""领域层类型定义。

本模块只放跨层共享的、零依赖的类型契约。`BarLike` 是 K 线层与结构
元素层（分型、笔、中枢等）的公共抽象：只要一个对象能提供 bar 的时间
范围、高低区间与方向，同一套算法就能跨级别复用，无需关心具体实现类。
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

PLACEHOLDER_TIME: Final[int] = -1

__all__ = ["BarLike", "PLACEHOLDER_TIME"]


@runtime_checkable
class BarLike(Protocol):
    """K 线与结构元素的公共抽象。

    属性语义：
    - ``open_time``: Unix 毫秒，bar/结构的起始时刻。
    - ``close_time``: Unix 毫秒，bar/结构的结束时刻。
    - ``high``: 区间内的最高价。
    - ``low``: 区间内的最低价。
    - ``direction``: 方向标记，``+1``=向上，``-1``=向下，``0``=中性/未定。

    该协议刻意保持最小：不约束来源（K 线、分型、笔、中枢皆可实现），
    使分型/笔/中枢等结构元素能被当作 bar 参与更高一级的区间运算。

    所有成员属性为只读（``@property`` 形式），允许具体实现用派生属性。

    使用示例（``@runtime_checkable`` 允许在运行时做 ``isinstance`` 检查，
    但更推荐依赖类型标注而非运行时判断）：

    .. code-block:: python

        from cpt.domain.types import BarLike

        class KLine:
            def __init__(self, open_time, close_time, high, low, direction):
                self._open_time = open_time
                self._close_time = close_time
                self._high = high
                self._low = low
                self._direction = direction

            @property
            def open_time(self) -> int:
                return self._open_time

            @property
            def close_time(self) -> int:
                return self._close_time

            @property
            def high(self) -> float:
                return self._high

            @property
            def low(self) -> float:
                return self._low

            @property
            def direction(self) -> int:
                return self._direction

        assert isinstance(KLine(0, 1, 2.0, 1.0, 1), BarLike)  # True
    """

    @property
    def open_time(self) -> int: ...

    @property
    def close_time(self) -> int: ...

    @property
    def high(self) -> float: ...

    @property
    def low(self) -> float: ...

    @property
    def direction(self) -> int: ...
