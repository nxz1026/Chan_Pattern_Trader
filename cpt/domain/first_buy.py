"""一买 / 一卖结构谓词（纯 domain 计算，R14 移植自 czsc）。

移植来源：czsc ``crates/czsc-signals/src/utils/cxt.rs`` 的
``check_first_buy`` / ``check_first_sell``（Apache-2.0）。

**为什么移植而不是调用**：czsc 的 Python 绑定把这两个函数包在 ``call_signal``
信号模板体系里（需要 ``CZSC`` 对象、模板名与参数字典），而 CPT 需要的是一个
自足的结构谓词——输入一串 :class:`~cpt.domain.models.Bi`，输出 ``bool``。
算法本身是纯函数，逐行移植比隔着模板层调用更可控、可测。

与 :mod:`cpt.domain.signal` 的分工：``signal.py`` 是**状态机**，把
``has_two_centers`` / ``has_divergence_leg`` / ``has_reversal_bi`` 当**入参**；
本模块负责**算出**那个最关键的判定——"这一段向下走势是否构成一买"。

判定口径（与 czsc 逐条对齐）：

1. 笔数必须为**奇数**（首尾同向，中间两两交替）；
2. 末笔方向必须是**向下**（一买）；首笔与末笔必须**同向**；
3. 整段最高点必须是**首笔**的高点、最低点必须是**末笔**的低点
   （即"下跌途中不断创出新低"）；
4. 收集 ``key_bis``：从下标 0 开始每隔 2 取一笔，首个恒取；其余只在
   **低点低于前两笔的低点**时取（逐级创新低的关键笔）；
5. 背驰：末笔与"前一笔（倒数第三笔）和 ``key_bis`` 均值"的较大者比较，
   ``power_price`` 变小 **且**（``power_volume`` 或 ``length`` 至少一个变小）
   才算背驰。

``check_first_sell`` 是其镜像（末笔向上、创新高、``key_bis`` 取更高点）。

**前置条件**：所有笔的力度度量（``power_price`` / ``power_volume`` / ``length``）
必须已填充。默认值 ``0`` 表示"未填充"——本模块会**响亮报错**而不是静默返回
``False``（否则背驰判定会拿 0 去比较，得出"不背驰"的错误结论）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final

from cpt.domain.models import Bi

__all__ = [
    "check_first_buy",
    "check_first_sell",
    "mean",
    "round_to_2_digit",
]

#: 笔方向。
_UP: Final[int] = 1
_DOWN: Final[int] = -1


def round_to_2_digit(value: float) -> float:
    """保留 2 位小数，半数**远离零**（对齐 Rust ``f64::round``）。

    Python 内建 ``round`` 对 .5 走银行家舍入，与 Rust 不同；这里显式实现，
    避免 ``power_price`` 在 .005 边界上与 czsc 分叉。
    """
    scaled = value * 100.0
    rounded = math.floor(scaled + 0.5) if scaled >= 0 else math.ceil(scaled - 0.5)
    return rounded / 100.0


def mean(values: Sequence[float]) -> float:
    """算术平均；空序列返回 ``0.0``（对齐 czsc ``utils::math::mean``）。"""
    if not values:
        return 0.0
    return sum(values) / len(values)


def _require_power_metrics(bis: Sequence[Bi]) -> None:
    """校验力度度量已填充。

    ``length`` 是笔的**去包含后K线根数**，至少为 1；``0`` 是"未填充"标记。
    不校验会让背驰比较拿 0 参与运算并静默返回 ``False``。
    """
    for index, bi in enumerate(bis):
        if bi.length <= 0:
            raise ValueError(
                f"bis[{index}].length={bi.length} 表示力度度量未填充；"
                f"一买判定需要 power_price/power_volume/length 三者齐备"
                f"（czsc 后端会直接填充，自研后端需先补算）"
            )


def _key_bis(bis: Sequence[Bi], *, by_low: bool) -> list[Bi]:
    """收集关键笔：下标 0, 2, 4, ... 至 ``len-3``。

    首个恒取；其余只在**创新低**（``by_low=True``，一买）或**创新高**
    （``by_low=False``，一卖）时取——即"逐级推进的关键笔"。
    """
    key: list[Bi] = []
    for i in range(0, len(bis) - 2, 2):
        if i == 0:
            key.append(bis[i])
        else:
            earlier, current = bis[i - 2], bis[i]
            if (current.low < earlier.low) if by_low else (current.high > earlier.high):
                key.append(current)
    return key


def _is_divergent(last: Bi, prev: Bi, key_bis: Sequence[Bi]) -> bool:
    """背驰判定：价格力度变小 **且**（量或长度至少一个变小）。"""
    bc_price = last.power_price < max(prev.power_price, mean([b.power_price for b in key_bis]))
    bc_volume = last.power_volume < max(prev.power_volume, mean([b.power_volume for b in key_bis]))
    bc_length = float(last.length) < max(
        float(prev.length), mean([float(b.length) for b in key_bis])
    )
    return bc_price and (bc_volume or bc_length)


def check_first_buy(bis: Sequence[Bi]) -> bool:
    """判断给定笔序列末端是否构成**一买**。

    :param bis: 按时间升序的笔序列（末尾即当前）。空序列返回 ``False``。
    :raises ValueError: 存在 ``length <= 0`` 的笔（力度度量未填充）。
    :returns: 构成一买返回 ``True``。判定口径见模块 docstring。
    """
    if not bis:
        return False
    if len(bis) % 2 != 1:
        return False
    first, last = bis[0], bis[-1]
    if last.direction != _DOWN or first.direction != last.direction:
        return False
    if max(bi.high for bi in bis) != first.high:
        return False
    if min(bi.low for bi in bis) != last.low:
        return False

    _require_power_metrics(bis)
    key_bis = _key_bis(bis, by_low=True)
    if not key_bis:
        return False
    return _is_divergent(last, bis[-3], key_bis)


def check_first_sell(bis: Sequence[Bi]) -> bool:
    """判断给定笔序列末端是否构成**一卖**（``check_first_buy`` 的镜像）。

    :param bis: 按时间升序的笔序列（末尾即当前）。空序列返回 ``False``。
    :raises ValueError: 存在 ``length <= 0`` 的笔（力度度量未填充）。
    :returns: 构成一卖返回 ``True``。
    """
    if not bis:
        return False
    if len(bis) % 2 != 1:
        return False
    first, last = bis[0], bis[-1]
    if last.direction != _UP or first.direction != last.direction:
        return False
    if max(bi.high for bi in bis) != last.high:
        return False
    if min(bi.low for bi in bis) != first.low:
        return False

    _require_power_metrics(bis)
    key_bis = _key_bis(bis, by_low=False)
    if not key_bis:
        return False
    return _is_divergent(last, bis[-3], key_bis)
