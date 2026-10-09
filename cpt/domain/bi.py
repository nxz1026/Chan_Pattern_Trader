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
    """用两个**异类**端点构造一笔（不做跨度/根数门槛，但强制端点异类）。

    2026-10-08 审计 H9：原来只在 docstring 里「由调用方保证交替」，于是
    ``_build_bis_gated`` 按 +1 步进时静默产出了「底→底」的伪笔 —— 中枢漏检、
    背驰分母错位，而图形上看起来完全正常。现在把这条不变量变成异常。
    """
    if start.kind == end.kind:
        raise ValueError(
            f"一笔的两端必须异类（底→顶 / 顶→底），实测 {start.kind}→{end.kind}"
            f"（start_time={start.start_time}, end_time={end.end_time}）—— "
            f"两端同类是「伪笔」，会让中枢漏检、背驰分母错位。"
        )
    return Bi(
        level=_resolve_level(start, end, level),
        direction=_direction(start),
        start_time=start.start_time,
        end_time=end.end_time,
        high=max(start.high, end.high),
        low=min(start.low, end.low),
        source_ids=_merge_ids(start, end),
    )


def build_bis(
    fractals: Sequence[Fractal],
    level: int | None = None,
    *,
    min_bi_len: int | None = None,
) -> tuple[Bi, ...]:
    """把分型序列压缩成交替端点的候选新笔序列，返回不可变 :class:`Bi` 元组。

    :param fractals: 按时间升序排列的分型序列（``detect_fractals`` 的输出，
        或任何 ``Fractal`` 序列）；空输入返回空元组。
    :param level: 笔级别。``None`` 时继承端点 ``Fractal.level``，两端不一致则
        报错；显式传入时覆盖端点级别，必须 ``>= 0``。
    :param min_bi_len: **底层笔的最少跨度**，量纲＝**去包含后**的 K 线根数
        （``RulesConfig.min_bi_len``）。``None``（默认）＝不设门槛。

        跨度不足时**合并端点**而不是丢弃该笔：丢掉会让笔序列出空洞，后续中枢
        与背驰的分母就错了。合并的含义是「当前笔的终点顺延到下一个够远的
        **异类**端点」，中间那个端点被吸收掉。顺延只落在异类分型上，所以笔的两端
        永远一底一顶（审计 H9：旧实现按 +1 步进会落到同类分型，产出两端同类的伪笔）。
        若顺延后没有更多异类端点可用，该笔不成立 —— 此时笔数会少于不设门槛时，
        这正是门槛的语义，不是丢数据。
    :raises ValueError: 存在 ``kind`` 不是 ``"top"`` / ``"bottom"`` 的分型；
        构造出的笔两端同类（伪笔，见 :func:`_make_bi`）；
        ``level < 0``；``level=None`` 时某笔两端分型 ``level`` 不同；
        ``min_bi_len < 1``；**要求门槛但某个分型的 ``merged_index`` 是
        ``None``**（量纲不可知时宁可报错，也不拿 ``bar_index`` 静默量错单位）。
    :returns: 按端点确认顺序排列的笔元组；相邻同类分型中较不极端者被丢弃，
        更极端者替换端点并使最后一笔端点后移。

    只做端点交替与同类极端保留：不做分型包含合并，不做笔中枢与走势类型判定。
    """
    if level is not None and level < 0:
        raise ValueError(f"level 必须 >= 0 或 None, 实测 {level}")
    if min_bi_len is not None and min_bi_len < 1:
        raise ValueError(f"min_bi_len 必须 >= 1 或 None, 实测 {min_bi_len}")

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

    if min_bi_len is None:
        return tuple(_make_bi(start, end, level) for start, end in pairwise(anchors))
    if len(anchors) < 2:
        return ()  # 空 / 单端点：没有笔可言（与不设门槛时 pairwise 的行为一致）
    return _build_bis_gated(anchors, level, min_bi_len)


def _build_bis_gated(
    anchors: Sequence[Fractal], level: int | None, min_bi_len: int
) -> tuple[Bi, ...]:
    """跨度门槛版：不足则**把终点顺延**到下一个够远的端点（合并，不丢弃）。

    量纲只用 ``Fractal.merged_index``（去包含后 K 线序列的下标）。缺失就抛 ——
    拿 ``bar_index``（原始下标）顶替会让门槛偏严，因为包含关系合并 K 线，
    去包含后根数 <= 原始根数，结果就是笔数与 czsc 参照侧对不上，而这种偏差在
    图表上看起来完全正常。
    """
    for a in anchors:
        if a.merged_index is None:
            raise ValueError(
                f"分型 {a.kind}@{a.start_time} 没有 merged_index，无法按"
                f"「去包含后 K 线根数」施加 min_bi_len={min_bi_len} 门槛。"
                f"bar_index 是**原始**下标，拿它顶替会量错单位 —— 请先用"
                f" detect_fractals() 产出的分型，或显式传 min_bi_len=None。"
            )

    out: list[Bi] = []
    start_idx = 0
    end_idx = 1
    while end_idx < len(anchors):
        start = anchors[start_idx]
        end = anchors[end_idx]
        # 跨度 = end 所在 bar 到 start 所在 bar 的**闭区间**长度
        span = int(end.merged_index) - int(start.merged_index) + 1  # type: ignore[arg-type]
        if span < min_bi_len:
            # 不足：吞掉这个端点，拿下一个**异类**端点再试 —— 这就是「合并」。
            # 直接丢弃本笔会让笔序列在时间轴上出空洞。
            #
            # 步进必须是 2：``anchors`` 严格交替（底/顶/底/…），+1 会落到与
            # ``start`` **同类**的分型上，于是 ``_make_bi`` 拿到「底→底」造出伪笔
            # （2026-10-08 审计 H9：min_bi_len=6 时实测产出 ``+1 0→9``、``+1 8→15``
            # 这种两端同类的笔）。顺延到下一个异类端点才是 docstring 说的语义。
            end_idx += 2
            continue
        out.append(_make_bi(start, end, level))
        start_idx = end_idx
        end_idx += 1
    return tuple(out)
