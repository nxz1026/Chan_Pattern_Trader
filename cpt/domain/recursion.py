"""递归结构元素映射（走势类型 → 高级别结构元素，纯 domain 计算）。

位于 ``docs/architecture.md`` §3.1 的 ``TrendType[] → StructureElement[]`` 环节，
即 ``docs/rules.md`` §3 递归链与 §8.3 统一递归流程的第一步：

    低级别走势类型 → 当前级别候选结构元素 → 当前级别分型 → 当前级别新笔

本模块只做第一步映射，把 :func:`cpt.domain.trend_type.classify_trend` 产出的走势
类型适配成实现 :class:`~cpt.domain.types.BarLike` 的 :class:`StructureElement`，
使高级别分型 / 新笔能复用同一套算法（:func:`cpt.domain.fractal.detect_fractals`
等）——这是「一套算法、两层输入」（``docs/architecture.md`` §3.1 核心设计）的
落地点。

规则口径按 ``docs/rules.md`` §3.1（结构元素字段）、§3.2（高级别分型用元素整体
``high`` / ``low``）、§3.3（高级别新笔 ≥5 元素）、§8.3（映射流程）与 §9.1
（结构元素时间相邻、不重叠，只过滤不合并）：

1. **一对一映射**：每个 ``direction`` 已确认（``±1``）的低级别走势类型映射为一个
   高级别候选结构元素；``direction = 0``（§3.4 方向未定的盘整）及任何非 ``±1``
   取值都不产出元素，只留在低级别作为候选。
2. **输入顺序即时间顺序**：只按 ``trends`` 给定顺序单遍扫描，不排序、不跳窗；
   空输入返回空元组。
3. **时间相邻不重叠**：候选元素的 ``[start_time, end_time]`` 与上一已产出元素区间
   存在严格交集（真重叠）时跳过该候选，保留较早者；端点相接
   （``start_time == 上一个.end_time``）是正常相邻，不算重叠（§9.1）。
4. **字段映射**：``open_time`` / ``close_time`` 取走势类型的 ``start_time`` /
   ``end_time``；``high`` / ``low`` 取走势类型整体极值——§3.2 的高级分型识别
   正是建立在这两个值上；``source_structure_ids`` 取 ``TrendType.source_ids``
   （保序、不变），``status`` 继承 ``TrendType.kind``（§9.3 的 ``open_end``、§9.4
   的 ``forming`` 等状态原样下传，本层不改写）；``kind`` 固定 ``"trend_type"``
   （``docs/architecture.md`` §6 ``StructureState.kind`` 词表），标明元素来源的
   结构类型。
5. **``source_revision`` 固定 ``0``**：元素基于低级别「初始 revision」的走势类型；
   低级别 revision 变化导致的依赖滞后由级联重构流程（§9.2）标记，本模块不猜测、
   不回填。
6. **``min_elements``**：§3.3 / §9.7 冻结的「高级别新笔至少 N 个结构元素」是本
   递归层的工程参数，在本模块只做值域校验（``>= 1``）并作为冻结接口的一部分暴露
   给调用方，**不裁剪输出**——该门槛作用在「元素 → 高级别新笔」阶段，由高级别
   :func:`cpt.domain.bi.build_bis` 施加；在此裁剪会破坏元素序列的可追溯性，也会
   让只需 3 个元素的高级分型无法工作。
7. **``target_level``**：产出元素的级别（递归链的上一级，如 5m 走势类型 → 30m
   元素），必须 ``>= 0``；本函数不校验它与输入 ``TrendType.level`` 的大小关系，
   级别链由 ``RulesConfig.levels`` 决定，属调用方职责。

本模块**不**生成高级别分型 / 新笔 / 中枢 / 走势类型（它们由同一套 domain 算法在
高级别重跑），**不**做包含合并（§9.1 已冻结结构元素层「只过滤、不合并」），不写
事件、不改低级别结构。对输入只读，重复调用同输入必同输出。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from cpt.domain.models import TrendType
from cpt.domain.types import BarLike

__all__ = [
    "StructureElement",
    "map_trend_types",
]

#: 已确认方向取值：向上 / 向下。其余取值（含 §3.4 的未定方向 ``0``）不映射。
_CONFIRMED_DIRECTIONS: Final[tuple[int, int]] = (1, -1)

#: ``StructureElement.kind``：本元素的来源结构类型（``StructureState.kind`` 词表）。
_KIND: Final[str] = "trend_type"

#: 递归元素初始 ``source_revision``：来自低级别结构的初始 revision（固定 ``0``）。
_SOURCE_REVISION: Final[int] = 0


@dataclass(frozen=True, slots=True)
class StructureElement:
    """高级别候选结构元素（低级别走势类型的 ``BarLike`` 适配，不可变）。

    ``docs/rules.md`` §3.1 / §8.3 要求结构元素至少保存方向、起止时间、高低价、
    成立状态与来源；本类型通过 ``@property`` 暴露 :class:`~cpt.domain.types.BarLike`
    的 5 个属性（``open_time``、``close_time``、``high``、``low``、``direction``），
    可直接喂给 :func:`cpt.domain.fractal.detect_fractals` 等结构算法。

    注意：与 ``CanonicalBar`` / ``MergedBar`` 同理，此处**不继承** ``BarLike``
    Protocol——Protocol 的 ``@property`` 成员会被 dataclass 当作带默认值的类属性。

    Attributes:
        level: 元素级别（递归链的上一级）。
        kind: 来源结构类型，固定 ``"trend_type"``。
        direction: 方向，``+1`` 向上 / ``-1`` 向下（只有已确认方向才构成元素）。
        open_time: 起止时间中的起始时刻（Unix 毫秒），即低级别走势类型的
            ``start_time``。
        close_time: 结束时刻（Unix 毫秒），即低级别走势类型的 ``end_time``。
        high: 元素整体最高价（§3.2 高级别顶分型判据）。
        low: 元素整体最低价（§3.2 高级别底分型判据）。
        source_structure_ids: 追溯链，原样保留低级别 ``TrendType.source_ids``。
        source_revision: 来源低级别 revision，首版固定 ``0``。
        status: 成立状态，继承低级别 ``TrendType.kind``（如 ``trend`` /
            ``consolidation`` / ``forming`` / ``open_end``），本层不改写。
    """

    level: int
    kind: str
    direction: int
    open_time: int
    close_time: int
    high: float
    low: float
    source_structure_ids: tuple[str, ...]
    source_revision: int
    status: str

    def __post_init__(self) -> None:
        if self.level < 0 or self.direction not in (-1, 0, 1):
            raise ValueError("StructureElement level/direction invalid")
        if self.open_time > self.close_time or self.high < self.low:
            raise ValueError("StructureElement time/range invalid")

    # BarLike 协议暴露（时间别名）
    @property
    def start_time(self) -> int:
        """起始时刻别名，与 ``open_time`` 一致。"""
        return self.open_time

    @property
    def end_time(self) -> int:
        """结束时刻别名，与 ``close_time`` 一致。"""
        return self.close_time

    # BarLike 协议暴露（追溯链别名）
    @property
    def source_ids(self) -> tuple[str, ...]:
        """追溯链别名，与 ``source_structure_ids`` 一致。"""
        return self.source_structure_ids


if TYPE_CHECKING:  # pragma: no cover - 编译期断言，无运行时开销

    def _conforms_barlike(element: StructureElement) -> BarLike:
        """静态断言：``StructureElement`` 满足 ``BarLike``（由 mypy 校验）。"""
        return element


def _overlaps(previous: StructureElement, trend: TrendType) -> bool:
    """候选走势类型的时间区间是否与上一元素真重叠（§9.1）。

    端点相接（``trend.start_time == previous.close_time``）是正常相邻，返回
    ``False``；只有区间存在严格交集时才返回 ``True``。
    """
    return trend.start_time < previous.close_time and previous.open_time < trend.end_time


def map_trend_types(
    trends: Sequence[TrendType], target_level: int, min_elements: int = 5
) -> tuple[StructureElement, ...]:
    """把低级别走势类型映射成高级别候选结构元素，返回不可变元组。

    :param trends: 按时间升序排列的走势类型序列（:func:`cpt.domain.trend_type.
        classify_trend` 的输出）。``direction`` 非 ``±1`` 的走势类型被跳过
        （§3.4 方向未定的盘整只作低级别候选）。
    :param target_level: 产出元素的级别（递归链上一级），必须 ``>= 0``。
    :param min_elements: 高级别新笔的最少结构元素数（§3.3 / §9.7），必须
        ``>= 1``。本函数只校验取值、不裁剪输出，门槛由高级别 ``build_bis`` 施加
        （见模块 docstring 第 6 条）。
    :raises ValueError: ``target_level < 0`` 或 ``min_elements < 1``。
    :returns: 按输入时间顺序排列的结构元素元组；每个元素一对一对应一个已确认方向
        的走势类型，除时间真重叠的候选被跳过（保留较早者）。``trends`` 为空、或
        全部走势类型方向未确认时返回空元组。元素可直接喂给
        :func:`cpt.domain.fractal.detect_fractals`。本函数对输入只读，不生成
        高级别分型 / 新笔，不写事件。
    """
    if target_level < 0:
        raise ValueError(f"target_level 必须 >= 0, 实测 {target_level}")
    if min_elements < 1:
        raise ValueError(f"min_elements 必须 >= 1, 实测 {min_elements}")

    elements: list[StructureElement] = []
    for trend in trends:
        if trend.direction not in _CONFIRMED_DIRECTIONS:
            continue
        if elements and _overlaps(elements[-1], trend):
            continue
        elements.append(
            StructureElement(
                level=target_level,
                kind=_KIND,
                direction=trend.direction,
                open_time=trend.start_time,
                close_time=trend.end_time,
                high=trend.high,
                low=trend.low,
                source_structure_ids=trend.source_ids,
                source_revision=_SOURCE_REVISION,
                status=trend.kind,
            )
        )
    return tuple(elements)
