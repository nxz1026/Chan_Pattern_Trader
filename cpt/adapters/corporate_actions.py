"""公司行动（分红派息 / 送转）的**共享模型与解析**。

R39 新增的起因：R37 的重算原打算拿 Wind 当唯一真值，结果 Wind 账户
**积分余额不足**（24/2197 只就停了）。而真值不一定非得花钱拿 ——
东财的 ``datacenter-web.eastmoney.com`` 分红送配接口**从大阪直连 200**，
字段齐全且免费。于是把模型抽到这里，Wind 与东财两个源共用。

## ⚠️ 单位陷阱（实测出来，不是猜的）

东财的 ``PRETAX_BONUS_RMB`` 是**每 10 股**的税前派息：

    600519 / 2024-12-31 报告期 → 276.73  ⇒ 每股 27.673 元
    （与茅台 2024 年度分红 27.673 元/股的公开数字一致）

不除以 10，因子会差整整一个数量级 ⇒ 所有历史价格 ×10。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CorporateAction",
    "ex_div_ratio",
    "filter_implemented",
]


@dataclass(frozen=True)
class CorporateAction:
    """一次公司行动（除权除息）。

    :param ex_date: 除权除息日（ISO）。**没有日期就丢弃** —— 定位不到因子台阶。
    :param cash_pre_tax: 税前每股派息（元）。``None`` = 该源没给这一项。
    :param cash_after_tax: 税后每股派息。
    :param share_bonus: 每股送股数。
    :param transfer: 每股转增数。
    :param source: 哪个源给的（``"wind"`` / ``"eastmoney"``）—— 出问题时能追回去。
    :param status: 实施进度原文（东财有"实施分配"这类值，未实施的**不能**用）。
    """

    ex_date: str
    cash_pre_tax: float | None = None
    cash_after_tax: float | None = None
    share_bonus: float | None = None
    transfer: float | None = None
    source: str = ""
    status: str = ""

    @property
    def share_ratio(self) -> float:
        """送股 + 转增的合计比例（``1 + s`` 里的那个 ``s``）。"""
        return (self.share_bonus or 0.0) + (self.transfer or 0.0)

    @property
    def is_implemented(self) -> bool:
        """是否**已实施**。

        东财会把"预案/股东大会通过"等未实施的方案也混在结果里 —— 用未实施的
        派息去算因子，等于按一个还没发生的事件调价。
        """
        if not self.status:
            return True  # 源没给这个字段时不武断（Wind 侧原本就是"实施完毕"）
        return any(k in self.status for k in ("实施", "除权", "完成"))


def ex_div_ratio(cash_per_10: float) -> float:
    """东财的「每 10 股派息」→ **每股**派息。

    ⚠️ 这个 10 是实测钉死的（茅台 276.73 ⇒ 27.673 元/股）。忘了除 ⇒
    因子差 10 倍、每张图价格差 10 倍。
    """
    return cash_per_10 / 10.0


def filter_implemented(actions: Sequence[CorporateAction]) -> tuple[CorporateAction, ...]:
    """只留已实施的；按日期升序。"""
    out = [a for a in actions if a.ex_date and a.is_implemented]
    out.sort(key=lambda a: a.ex_date)
    return tuple(out)


def _as_float(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
