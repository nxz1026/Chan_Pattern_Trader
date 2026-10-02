"""**按市场**的级别标签表（R28-9）。

## 为什么需要它

``RulesConfig.levels = (5, 30)``，docstring 明写「级别链，元素为**分钟**级别」。
这对加密市场成立（1m/1h 序列），对 **A 股不成立** —— A 股喂的是
``daily_bar`` 日线（``INTERVAL_MS = 24*3600*1000``），却复用了同一份配置。

后果实测过（2026-10-02，oracle）：给 A 股结构 ``level=5``，LLM 解释正文写的是

    「该结构为深物业A在**5 分钟级别**（level=5）的一笔向上运动」

**模型没胡说** —— 它忠实照着「level 的单位是分钟」讲的。问题在上游的标签。

一条听起来很专业、实则完全错误的解释，比「不知道」有害得多：用户会拿它去做判断。

## 本模块做什么（以及刻意不做什么）

**做**：按市场给每个 level 一个**人类可读的标签**，并把标签连同「不要把数字当
分钟」这句一起喂给模型。

**不做**：不重编 A 股的 level 数字、不改任何计算逻辑。level 的**计算**含义
（哪个级别的结构）在两个市场里其实是同一套（相对周期关系），对不上的是
**展示标签**。把标签修对，比动计算便宜得多、风险也小得多。

## A 股的 level 到底对应什么

A 股当前**只产出 level 5**（`a_share_snapshot` 取 ``config.levels[0]``），数据源是
日线。所以 level 5 标成「日线级别」。level 30 在 A 股侧并没有对应产物 ——
这里**如实写成未启用**而不是编一个「30 分钟」，因为后者会制造第二个错误。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "CN_LEVELS",
    "CRYPTO_LEVELS",
    "LEVEL_TABLES",
    "LevelSpec",
    "level_label",
    "level_table_for",
]


@dataclass(frozen=True, slots=True)
class LevelSpec:
    """一个级别在本市场的展示标签。

    :param key: ``RulesConfig.levels`` 里的数值。
    :param label: 人类可读标签（**不要**带「级」以外的单位歧义）。
    :param minutes: 真实时间跨度（分钟）。**非分钟周期为 ``None``** —— 日线不是
        「1440 分钟级别」那种换算说法，说「日线」才准确。
    :param produced: 本市场当前是否真的产出该级别。``False`` 时标签写成
        「未启用」，避免模型拿一个不存在的级别编内容。
    """

    key: int
    label: str
    minutes: int | None
    produced: bool = True
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"label": self.label}
        if self.minutes is not None:
            out["minutes"] = self.minutes
        if not self.produced:
            out["produced"] = False
        if self.note:
            out["note"] = self.note
        return out


#: 加密市场：``levels`` 就是分钟数，直接对应。
CRYPTO_LEVELS: Final[MappingProxyType[int, LevelSpec]] = MappingProxyType(
    {
        5: LevelSpec(5, "5 分钟级别", 5, note="由分钟线计算"),
        30: LevelSpec(30, "30 分钟级别", 30, note="由分钟线计算"),
    }
)

#: A 股：数据源是**日线**，``level`` 只是结构层级的相对编号，**不是分钟数**。
CN_LEVELS: Final[MappingProxyType[int, LevelSpec]] = MappingProxyType(
    {
        5: LevelSpec(
            5,
            "日线级别",
            None,
            note="本市场数据源为日线（daily_bar），level=5 是最基础的日线级别，不是 5 分钟",
        ),
        30: LevelSpec(
            30,
            "日线之上的高级别",
            None,
            produced=False,
            note="A 股侧当前只产出 level=5，本级别未启用 —— 不要基于它编内容",
        ),
    }
)

#: 市场 kind → 级别表。key 与 ``snapshot["market"]["kind"]`` 对齐。
LEVEL_TABLES: Final[MappingProxyType[str, Mapping[int, LevelSpec]]] = MappingProxyType(
    {
        "a_share": CN_LEVELS,
        "crypto": CRYPTO_LEVELS,
    }
)

#: 未知市场的空表。用模块常量而非每次新建，避免每次调用都分配。
_EMPTY: Final[Mapping[int, LevelSpec]] = MappingProxyType({})

#: 未知市场时用这个 —— 只说「未标注」，**绝不**默认按分钟解释。
_UNKNOWN: Final[LevelSpec] = LevelSpec(
    -1,
    "未标注级别",
    None,
    produced=False,
    note="该市场的级别语义未登记，不要推断其单位",
)


def level_table_for(market: str) -> Mapping[int, LevelSpec]:
    """按市场取级别表；未知市场返回空表（调用方需能降级）。"""
    return LEVEL_TABLES.get(str(market).strip().lower(), _EMPTY)


def level_label(market: str, level: Any) -> str:
    """某级别在本市场的人类可读标签。

    查不到时返回「未标注级别（level=N）」而不是「N 分钟级别」——
    猜错单位比不回答更糟。
    """
    table = level_table_for(market)
    spec = table.get(level) if isinstance(level, int) else None
    if spec is None:
        return f"{_UNKNOWN.label}（level={level}）"
    return spec.label
