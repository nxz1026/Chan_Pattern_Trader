"""走势类型分类（``docs/rules.md`` §8.1 / §9.3-9.5，纯 domain 计算）。

位于 ``docs/architecture.md`` §3.1 的 ``Bi[] + ZhongShu[] → TrendType[]`` 环节：
消费 :func:`cpt.domain.zhongshu.build_zhongshus` 产出的笔中枢序列，按「新笔序列
+ 笔中枢 + 中枢之间的连接笔」把走势切分成 :class:`cpt.domain.models.TrendType`。

规则口径按 ``docs/rules.md`` §8.1（构成与完成分类）、§3.4（方向未定的盘整）、
§9.3（开放终态 ``open_end``）、§9.4（``candidate`` 改名 ``forming``）、
§9.5（``zs_wzgx = zgd``）：

1. **输入顺序即时间顺序**：只按 ``bis`` / ``zhongshus`` 给定顺序单遍处理，不
   排序、不跳窗。``bis`` 或 ``zhongshus`` 为空，或中枢时间区间内没有任何笔时
   返回空元组——§8.1 的走势类型必须由笔中枢构成，没有可用中枢就无从分类。
2. **中枢的覆盖笔区间**：中枢 ``start_time`` / ``end_time`` 之内的笔即该中枢
   的覆盖笔（``build_zhongshus`` 的中枢时间区间恰为首笔 ``start_time`` 到末笔
   ``end_time``）。中枢之前的进入笔不归属该走势类型，由上一走势类型或后续
   中枢负责。
3. **相邻中枢分离**：按 §9.5 固定 ``zs_wzgx = zgd``（高点比 ``zg``、低点比
   ``zd``）判断位置关系——后一中枢整体位于前一中枢上方（``low > prev.high``
   且高低点同向上移）记为上移，整体位于下方记为下移，其余为未分离。
4. **趋势 run**：分离方向一致的连续中枢聚为一个 run。run 内至少两个中枢、且
   最后一个中枢之后（``[末笔下标+1, 下一 run 首笔下标)``）存在同方向离开笔时，
   该 run 判定为 ``trend``；分离成立但缺少同方向离开笔时判定为 ``forming``。
5. **单中枢 run**：本走势类型的后续笔为中枢末笔之后、下一 run 首笔之前的笔；其
   中只要存在仍与中枢闭区间重叠的笔（``zs_wzgx = zgd``）即判定为
   ``consolidation``，覆盖区间向后延伸到最后一根重叠笔；一根都不重叠（已经离开
   中枢）时判定为 ``forming``，方向取首根后续笔（离开笔）的方向。
6. **``open_end``**：输出中最后一个走势类型若在其结束之后没有反方向笔，则标记
   为 ``open_end``（§9.3 的开放终态，与 ``closed_by_reversal`` 区分）；方向
   未定（``0``）时无从确认反向，同样标记 ``open_end``。
7. **方向**：趋势 run 取中枢分离方向（``+1`` 上移 / ``-1`` 下移）；单中枢
   ``forming`` 取离开笔方向；盘整与无后续笔的单中枢为 ``0``（§3.4 方向未定）。
8. **区间与来源**：``start_time`` / ``end_time`` 取覆盖区间首末笔的时间，
   ``high`` / ``low`` 取覆盖区间各笔的极值，``source_ids`` 为「成员中枢
   ``bi_ids`` + 覆盖区间笔 ``source_ids``」按顺序拼接后的保序去重（确定性）。

本模块只做分类，**不**实现一买信号、递归映射（``recursion.py``）与级联重构
（``engine/rebuild.py``）；不产出 ``extended`` / ``reclassified`` / ``closed``
——它们由延伸、重分类与后验关闭流程回填。本函数对输入只读，不修改 ``Bi`` /
``ZhongShu``；重复调用同输入必同输出。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, cast

from cpt.domain.models import Bi, TrendKind, TrendType, ZhongShu

__all__ = ["classify_trend"]

#: ``Bi.direction`` 合法取值：向上笔 / 向下笔。
_DIRECTIONS: Final[tuple[int, int]] = (1, -1)

#: 走势方向：向上 / 向下 / 未定。
_UP: Final[int] = 1
_DOWN: Final[int] = -1
_UNDETERMINED: Final[int] = 0

#: 本模块产出的走势类型状态（``TrendType.kind``，``docs/rules.md`` §8.1）。
_KIND_CONSOLIDATION: Final[str] = "consolidation"
_KIND_TREND: Final[str] = "trend"
_KIND_FORMING: Final[str] = "forming"
_KIND_OPEN_END: Final[str] = "open_end"


def _resolve_level(bis: Sequence[Bi], zhongshus: Sequence[ZhongShu], level: int | None) -> int:
    """解析走势类型级别：显式 ``level`` 覆盖；否则要求笔与中枢同级别。"""
    if level is not None:
        return level
    levels = sorted({bi.level for bi in bis} | {zs.level for zs in zhongshus})
    if len(levels) > 1:
        raise ValueError(f"未显式指定 level 时全部笔与中枢必须同级别, 实测 {levels}")
    return levels[0]


def _span(bis: Sequence[Bi], zhongshu: ZhongShu) -> tuple[int, int] | None:
    """中枢覆盖笔的下标区间 ``(首, 末)``（闭区间）；区间内没有笔返回 ``None``。"""
    covered = [
        index
        for index, bi in enumerate(bis)
        if bi.start_time >= zhongshu.start_time and bi.end_time <= zhongshu.end_time
    ]
    if not covered:
        return None
    return covered[0], covered[-1]


def _separation(previous: ZhongShu, current: ZhongShu) -> int:
    """相邻中枢的位置关系：``zs_wzgx = zgd``（高点比 ``zg``、低点比 ``zd``）。

    返回 ``+1`` 后一中枢整体上移、``-1`` 整体下移、``0`` 未同向分离（含重叠、
    接触与嵌套）。分离条件同时要求两中枢不构成重叠（``docs/rules.md`` §8.1）。
    """
    if current.low > previous.high and current.high > previous.high and current.low > previous.low:
        return _UP
    if current.high < previous.low and current.high < previous.high and current.low < previous.low:
        return _DOWN
    return _UNDETERMINED


def _overlaps(bi: Bi, high: float, low: float) -> bool:
    """笔区间是否与中枢闭区间重叠（``zs_wzgx = zgd``：高点比 ``zg``、低点比 ``zd``）。"""
    return bi.high >= low and bi.low <= high


def _departure(bis: Sequence[Bi], start: int, stop: int, direction: int) -> int | None:
    """``[start, stop)`` 内第一根同向笔的下标；不存在返回 ``None``。"""
    for index in range(start, stop):
        if bis[index].direction == direction:
            return index
    return None


def _last_overlap(bis: Sequence[Bi], start: int, stop: int, high: float, low: float) -> int | None:
    """``[start, stop)`` 内与中枢区间重叠的最后一根笔的下标；没有重叠笔返回 ``None``。"""
    last: int | None = None
    for index in range(start, stop):
        if _overlaps(bis[index], high, low):
            last = index
    return last


def _reversed_after(bis: Sequence[Bi], end: int, direction: int) -> bool:
    """``end`` 之后是否存在反方向笔；方向未定（``0``）时恒为 ``False``。"""
    if direction == _UNDETERMINED:
        return False
    return any(bi.direction == -direction for bi in bis[end + 1 :])


def _group_runs(matched: Sequence[tuple[ZhongShu, int, int]]) -> list[list[int]]:
    """把分离方向一致的连续中枢聚成 run，返回各 run 在 ``matched`` 中的下标列表。"""
    runs: list[list[int]] = [[0]]
    for index in range(1, len(matched)):
        run = runs[-1]
        link = _separation(matched[index - 1][0], matched[index][0])
        if link == _UNDETERMINED:
            runs.append([index])
            continue
        run_direction = (
            _UNDETERMINED if len(run) == 1 else _separation(matched[run[0]][0], matched[run[-1]][0])
        )
        if len(run) == 1 or link == run_direction:
            run.append(index)
        else:
            runs.append([index])
    return runs


def _source_ids(
    matched: Sequence[tuple[ZhongShu, int, int]],
    run: Sequence[int],
    bis: Sequence[Bi],
    start: int,
    end: int,
) -> tuple[str, ...]:
    """成员中枢 ``bi_ids`` + 覆盖区间笔 ``source_ids``，按顺序拼接后保序去重。"""
    ids = [bi_id for index in run for bi_id in matched[index][0].bi_ids]
    ids.extend(source_id for bi in bis[start : end + 1] for source_id in bi.source_ids)
    return tuple(dict.fromkeys(ids))


def classify_trend(
    bis: Sequence[Bi], zhongshus: Sequence[ZhongShu], level: int | None = None
) -> tuple[TrendType, ...]:
    """把笔与笔中枢序列分类成走势类型，返回不可变 :class:`TrendType` 元组。

    :param bis: 按时间升序排列的笔序列（``build_bis`` 的输出，或任何 ``Bi``
        序列）。
    :param zhongshus: 按时间升序排列的笔中枢序列（``build_zhongshus`` 的输出）。
        时间区间内没有对应笔的中枢被跳过，不参与分类。
    :param level: 走势类型级别。``None`` 时要求全部输入笔与中枢同级别并继承之；
        显式传入时覆盖，必须 ``>= 0``。
    :raises ValueError: 显式 ``level < 0``；存在 ``direction`` 不是 ``1`` / ``-1``
        的笔；``level=None`` 且输入笔/中枢级别不一致。
    :returns: 按中枢时间顺序排列的走势类型元组。每个走势类型覆盖「成员中枢
        + 从首个成员中枢首笔开始的连接/离开笔」，``kind`` 取 ``consolidation`` /
        ``trend`` / ``forming`` / ``open_end``（见模块 docstring）。``bis`` 或
        ``zhongshus`` 为空、中枢全部缺少覆盖笔时返回空元组。本函数不计算背驰、
        不生成一买信号、不做递归映射与级联重构。
    """
    if level is not None and level < 0:
        raise ValueError(f"level 必须 >= 0 或 None, 实测 {level}")
    for bi in bis:
        if bi.direction not in _DIRECTIONS:
            raise ValueError(f"Bi.direction 必须是 1 或 -1, 实测 {bi.direction!r}")
    if not bis or not zhongshus:
        return ()

    trend_level = _resolve_level(bis, zhongshus, level)

    matched: list[tuple[ZhongShu, int, int]] = []
    for zhongshu in zhongshus:
        span = _span(bis, zhongshu)
        if span is not None:
            matched.append((zhongshu, span[0], span[1]))
    if not matched:
        return ()

    runs = _group_runs(matched)
    trend_types: list[TrendType] = []
    for position, run in enumerate(runs):
        first, last = run[0], run[-1]
        # 本走势类型的扫描上界：下一 run 的首笔，避免相邻走势类型覆盖区间交叠。
        stop = matched[runs[position + 1][0]][1] if position + 1 < len(runs) else len(bis)
        zone = matched[last][0]
        end_index = matched[last][2]
        direction = _UNDETERMINED
        if last > first:
            direction = _separation(matched[first][0], matched[last][0])
            departure = _departure(bis, end_index + 1, stop, direction)
            if departure is None:
                kind = _KIND_FORMING
            else:
                kind = _KIND_TREND
                end_index = departure
        else:
            overlap_end = _last_overlap(bis, end_index + 1, stop, zone.high, zone.low)
            if overlap_end is not None:
                kind = _KIND_CONSOLIDATION
                end_index = overlap_end
            elif end_index + 1 < stop:
                kind = _KIND_FORMING
                direction = bis[end_index + 1].direction
                end_index += 1
            else:
                kind = _KIND_FORMING
        if position == len(runs) - 1 and not _reversed_after(bis, end_index, direction):
            kind = _KIND_OPEN_END

        start_index = matched[first][1]
        covered = bis[start_index : end_index + 1]
        trend_types.append(
            TrendType(
                level=trend_level,
                kind=cast(TrendKind, kind),
                direction=direction,
                start_time=covered[0].start_time,
                end_time=covered[-1].end_time,
                high=max(bi.high for bi in covered),
                low=min(bi.low for bi in covered),
                source_ids=_source_ids(matched, run, bis, start_index, end_index),
            )
        )

    return tuple(trend_types)
