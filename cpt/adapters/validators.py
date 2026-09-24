"""K 线序列校验：去重、严格递增、契约周期连续性、缺口检测。

本模块是 M4 数据接入的**窄校验层**：只处理 ``CanonicalBar`` 序列的完整性与
契约一致性，不抓数据、不落盘、不生成结构。它独立于
``cpt.adapters.binance_futures``（``docs/architecture.md`` §3.3），因此可单独
单测，也可被回放（``cpt.application.replay``）与实时路径复用。

校验口径来自 ``docs/rules.md`` §8.5（Binance USDⓈ-M 永续 Kline 契约）：

* 5 分钟为底层周期，相邻 K 线 ``open_time`` 间隔必须恰为契约周期；
* **发现缺口不自动填充，并阻止跨缺口生成正式结构** → 缺口抛
  :class:`DataGapError`，由调用方决定"记录缺口并阻断结构生成"，本模块不做
  任何缝合或插值；
* 只在契约周期上连续的一份切片才有资格进入结构计算，见
  :func:`validate_no_gap_segment`。

两个入口的分工：

+-----------------------------+--------+--------+------------+
| 入口                        | 去重   | 排序   | 用途       |
+=============================+========+========+============+
| :func:`validate_canonical_bars` | 是（保留首次） | 只校验、不重排 | 入库/回填边界 |
+-----------------------------+--------+--------+------------+
| :func:`validate_no_gap_segment` | 否（重复即报错） | 只校验、不重排 | 结构计算前的切片守卫 |
+-----------------------------+--------+--------+------------+

两者都**不重排输入**：乱序属于数据缺陷，必须由调用方显式排序后重试。

示例：

.. code-block:: python

    from cpt.adapters.validators import DataGapError, validate_canonical_bars

    try:
        bars = validate_canonical_bars(raw_bars)  # 默认 5m
    except DataGapError as exc:
        ...  # 记录缺口，阻断跨缺口结构生成
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from cpt.domain.models import CanonicalBar

__all__ = [
    "DEFAULT_INTERVAL_MS",
    "DataGapError",
    "DataValidationError",
    "validate_ashare_bars",
    "validate_canonical_bars",
    "validate_no_gap_segment",
]

#: 首版底层周期：5 分钟（``docs/rules.md`` §8.5），单位毫秒。
DEFAULT_INTERVAL_MS: Final[int] = 300_000


class DataValidationError(ValueError):
    """输入 K 线序列**自身不合法**。

    触发场景（本模块所有的"非法"都走这一个异常）：

    * 同一个 ``open_time`` 出现**内容不一致**的重复 K 线（数据冲突）；
    * ``open_time`` 未严格递增（重复或乱序），即需要调用方排序的数据；
    * OHLC 非法：``open`` / ``close`` 落在 ``[low, high]`` 之外；
    * ``close_time`` 不满足契约边界 ``open_time + interval_ms - 1``；
    * 相邻间隔小于契约周期（疑似混入更细周期数据）；
    * ``interval_ms`` 参数本身非正。

    与 :class:`DataGapError` 是**兄弟关系**而非父子：缺口是"数据齐全但缺一段"
    的预期状况，可以由调用方记录后继续；本异常是数据缺陷，必须修数据或拒绝。
    因此 ``except DataValidationError`` 不会吞掉缺口。
    """


class DataGapError(ValueError):
    """输入序列在契约周期上**存在缺口**（缺失整根 K 线）。

    ``docs/rules.md`` §8.5 要求缺口既不自动填充、也不跨缺口生成正式结构；
    本异常即该规则的执行点。消息中含前后 ``open_time``、实测间隔、期望间隔与
    推算出的缺失根数，可直接落盘记录。
    """


def validate_canonical_bars(
    bars: Sequence[CanonicalBar],
    interval_ms: int = DEFAULT_INTERVAL_MS,
) -> tuple[CanonicalBar, ...]:
    """校验并规范化一段 ``CanonicalBar`` 序列，返回不可变元组。

    按固定顺序执行，任一步失败即抛异常（不做部分返回）：

    1. **逐根契约校验**：OHLC 合法（``low <= open, close <= high``）、
       ``close_time == open_time + interval_ms - 1``；
    2. **去重**：按 ``open_time`` 保留**首次**出现的 K 线。内容完全相同的重复
       视为回填分页重叠，静默折叠；同一 ``open_time`` 内容不同则抛
       :class:`DataValidationError`（数据冲突，不可猜测取舍）；
    3. **严格递增**：去重后的 ``open_time`` 必须严格递增，否则抛
       :class:`DataValidationError`（本函数不重排输入）；
    4. **周期连续**：相邻间隔必须恰为 ``interval_ms``。大于即缺口，抛
       :class:`DataGapError`；小于即周期错配，抛 :class:`DataValidationError`。

    Args:
        bars: 待校验 K 线，顺序按调用方给定；函数不改变其顺序。
        interval_ms: 契约周期（毫秒），默认 5 分钟。

    Returns:
        去重后、按输入顺序严格递增且无缺口的 K 线元组。

    Raises:
        DataValidationError: 逐根契约非法、重复内容冲突、乱序、间隔小于周期或
            ``interval_ms`` 非正。
        DataGapError: 相邻 ``open_time`` 间隔大于 ``interval_ms``。

    Note:
        空序列与单根序列均为合法输入，原样返回（不推断缺口）。
    """
    _require_positive_interval(interval_ms)
    for index, bar in enumerate(bars):
        _validate_bar_contract(bar, index, interval_ms)
    deduped = _dedup_by_open_time(bars)
    _require_strictly_increasing(deduped)
    _require_contiguous(deduped, interval_ms)
    return deduped


def validate_ashare_bars(
    bars: Sequence[CanonicalBar],
    interval_ms: int = DEFAULT_INTERVAL_MS,
) -> tuple[CanonicalBar, ...]:
    """校验 A 股日线序列 —— **不做连续性检查**（R15/R16，C3/C5）。

    与 :func:`validate_canonical_bars` 的唯一差别是**跳过第 4 步**：

    1. 逐根契约校验（OHLC 合法 + ``close_time`` 边界）—— 同 crypto；
    2. 按 ``open_time`` 去重 —— 同 crypto；
    3. 严格递增 —— 同 crypto；
    4. ~~周期连续~~ —— **跳过**。

    **为什么跳过连续性**：``validate_canonical_bars`` 的连续契约是**加密市场
    假设**（BTC 24/7 无休）。A 股的日历缺口全部是合法的：

    - **C5 非交易日**：周末与法定节假日不开市（实测踩到 2026-08-14 → 08-17
      的 3 天间隔就是周末）；
    - **C3 停牌**：个股停牌期间无行情。

    这两种缺口**不是数据缺失**，不能被 ``DataGapError`` 拦下，也绝不能填 0 或
    补假日（``docs/rules.md`` §5.3：非交易日不出图、不用 0 填充）。因此 A 股走
    本函数 + :func:`cpt.application.replay.run_replay`，而不是 ``replay_bars``。

    Args:
        bars: 待校验 A 股日线，顺序按调用方给定。
        interval_ms: 契约周期（毫秒），A 股日线为 ``86400000``。

    Returns:
        去重后、严格递增的 K 线元组（**允许日历缺口**）。

    Raises:
        DataValidationError: 逐根契约非法、重复内容冲突、乱序、``interval_ms`` 非正。
    """
    _require_positive_interval(interval_ms)
    for index, bar in enumerate(bars):
        _validate_bar_contract(bar, index, interval_ms)
    deduped = _dedup_by_open_time(bars)
    _require_strictly_increasing(deduped)
    return deduped


def validate_no_gap_segment(
    bars: Sequence[CanonicalBar],
    interval_ms: int = DEFAULT_INTERVAL_MS,
) -> tuple[CanonicalBar, ...]:
    """校验一份切片内部无缺口，返回该切片的不可变元组副本。

    结构计算前的守卫：``docs/rules.md`` §8.5 禁止跨缺口生成正式结构，因此在
    把一份切片交给分型/笔/中枢算法之前用它确认"这份切片确实连续"。

    与 :func:`validate_canonical_bars` 的差别只有一点：**不做去重**。切片理应
    已由入库边界去重，此处的重复说明调用方拼装错误，直接抛
    :class:`DataValidationError` 而不是静默折叠；顺序同样不重排。

    Args:
        bars: 已按 ``open_time`` 升序排列的切片。
        interval_ms: 契约周期（毫秒），默认 5 分钟。

    Returns:
        与输入等长、元素顺序一致的元组。

    Raises:
        DataValidationError: 逐根契约非法、存在重复 ``open_time``、乱序、间隔
            小于周期或 ``interval_ms`` 非正。
        DataGapError: 切片内部存在缺口（相邻间隔大于 ``interval_ms``）。
    """
    _require_positive_interval(interval_ms)
    for index, bar in enumerate(bars):
        _validate_bar_contract(bar, index, interval_ms)
    segment = tuple(bars)
    _require_strictly_increasing(segment)
    _require_contiguous(segment, interval_ms)
    return segment


def _require_positive_interval(interval_ms: int) -> None:
    """``interval_ms`` 必须为正整数，否则契约周期无意义。"""
    if interval_ms <= 0:
        raise DataValidationError(f"interval_ms 必须为正整数, 实测 {interval_ms}")


def _validate_bar_contract(bar: CanonicalBar, index: int, interval_ms: int) -> None:
    """单根 K 线的契约校验：OHLC 合法 + ``close_time`` 边界。

    ``CanonicalBar`` 自身已保证 ``high >= low`` 与数值有限，这里补的是构造期
    无法覆盖的两条 Binance 契约（``docs/rules.md`` §8.5）。
    """
    if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
        raise DataValidationError(
            f"bars[{index}] open_time={bar.open_time} OHLC 非法: "
            f"要求 low<=open,close<=high, 实测 "
            f"low={bar.low} open={bar.open} high={bar.high} close={bar.close}"
        )
    expected_close_time = bar.open_time + interval_ms - 1
    if bar.close_time != expected_close_time:
        raise DataValidationError(
            f"bars[{index}] open_time={bar.open_time} close_time={bar.close_time} "
            f"违反契约边界: 期望 {expected_close_time} (=open_time+{interval_ms}-1)"
        )


def _dedup_by_open_time(bars: Sequence[CanonicalBar]) -> tuple[CanonicalBar, ...]:
    """按 ``open_time`` 去重，保留首次出现；内容冲突抛 ``DataValidationError``。

    使用字典记录每个 ``open_time`` 的首现 K 线，因此回填分页重叠（后一页重复
    前一页尾部）也能被正确折叠，而不要求重复项相邻。
    """
    first_seen: dict[int, CanonicalBar] = {}
    deduped: list[CanonicalBar] = []
    for index, bar in enumerate(bars):
        kept = first_seen.get(bar.open_time)
        if kept is None:
            first_seen[bar.open_time] = bar
            deduped.append(bar)
            continue
        if kept != bar:
            raise DataValidationError(
                f"bars[{index}] open_time={bar.open_time} 与首次出现的内容冲突: "
                f"首现 O/H/L/C=({kept.open}, {kept.high}, {kept.low}, {kept.close}), "
                f"重复 O/H/L/C=({bar.open}, {bar.high}, {bar.low}, {bar.close})"
            )
    return tuple(deduped)


def _require_strictly_increasing(bars: tuple[CanonicalBar, ...]) -> None:
    """``open_time`` 必须严格递增；本函数只报错，不重排（无隐式排序）。"""
    for index in range(1, len(bars)):
        previous = bars[index - 1].open_time
        current = bars[index].open_time
        if current <= previous:
            raise DataValidationError(
                f"bars[{index}] open_time={current} 未严格大于前一根 {previous}; "
                "输入存在重复或乱序, 请先去重并按 open_time 升序排列"
            )


def _require_contiguous(bars: tuple[CanonicalBar, ...], interval_ms: int) -> None:
    """相邻间隔必须恰为 ``interval_ms``：更大即缺口，更小即周期错配。"""
    for index in range(1, len(bars)):
        previous = bars[index - 1].open_time
        current = bars[index].open_time
        delta = current - previous
        if delta == interval_ms:
            continue
        if delta > interval_ms:
            expected_bars, residual = divmod(delta, interval_ms)
            detail = f"缺失 {expected_bars - 1} 根 K 线"
            if residual:
                detail += f" (间隔非 {interval_ms}ms 整数倍, 残差 {residual}ms)"
            raise DataGapError(
                f"bars[{index - 1}] open_time={previous} -> bars[{index}] "
                f"open_time={current} 存在缺口: 实测间隔 {delta}ms, 期望 {interval_ms}ms, "
                f"{detail}"
            )
        raise DataValidationError(
            f"bars[{index}] open_time={current} 与前一根间隔 {delta}ms 小于契约周期 "
            f"{interval_ms}ms, 疑似混入更细周期数据"
        )
