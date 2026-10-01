"""A 股规则标签（C2/C4 — R15-3）。

**本模块是纯领域逻辑：零 IO、零 SQL。**（R24 分层恢复，2026-10-01）

原本混在这里的三处数据库查询已按职责下沉到 :mod:`cpt.adapters.a_share_local`：

======================  ==================================  ============
搬走的                  去处                                查的表
======================  ==================================  ============
``fetch_daily_tags``    ``adapters.a_share_local``          ``public.derived_bar``
``check_t_plus_one_calendar``  ``adapters.a_share_local``    ``public.trade_calendar``
``_next_trade_date``    ``adapters.a_share_local``          ``public.trade_calendar``
======================  ==================================  ============

留在本模块的是**不碰 IO 的部分**：值对象 ``AShareDailyTag``、纯函数
``apply_ashare_tags_to_bis`` 与 ``t_plus_one_purchase_allowed``、错误类
``AShareTagsError``。domain 不 import 任何上层，也不该出现 SQL 字符串 ——
这条由 CI 门禁「SQL 只许出现在 ``adapters/`` 与 ``storage/``」强制。

按 plan §5.3：
- **C2 涨跌停**：涨停日的笔/中枢**端点可信度低**（涨停挂单买不到、卖单大量堆积），
  需在缠论结构上打 ``is_limit_up`` 标签，让上层画图时用虚线/降透明。±10% 阈值
  不可靠（ST/科创/创业/北交规则不同），**直接读 ``public.derived_bar.is_limit_up``**。
- **C3 停牌**：``public.daily_bar`` 是交易日表，停牌日本来就没行——已在
  :mod:`cpt.adapters.a_share_local` 隐含处理。
- **C4 T+1**：在 ``Signal`` 上打 ``t_plus_one: bool``，由 :mod:`cpt.domain.signal`
  在评估一买/一卖时读取。**本模块提供纯谓词**，不直接改 ``Signal`` schema
  （避免污染跨市场语义——加密没有 T+1）；日历查询在 adapters 层。
- **C5 非交易日**：`public.daily_bar` 天然不画图，不需额外处理。

## 决定
``public.derived_bar`` 已有 ``is_limit_up / is_bomb / is_one_word / touched_limit``
等列（plan §5.2 表列）。CPT **不重新计算涨跌停**（避免出错路径），由 adapters
层直接查 ``derived_bar`` 并把标签挂到 ``CanonicalBar`` 上。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from cpt.domain.models import Bi

__all__ = [
    "AShareDailyTag",
    "apply_ashare_tags_to_bis",
    "AShareTagsError",
    "t_plus_one_purchase_allowed",
]


class AShareTagsError(RuntimeError):
    """A 股规则标签应用失败。"""


@dataclass(frozen=True)
class AShareDailyTag:
    """单日的 A 股衍生标签（从 ``public.derived_bar`` 读出）。"""

    code: str
    trade_date: str  # ISO date
    is_limit_up: bool
    is_limit_down: bool
    is_bomb: bool
    is_one_word: bool

    @property
    def has_any_extreme(self) -> bool:  # noqa: D401
        """任意极端形态标签 — 用于决定"该日 K 线画虚线 / 降透明"。"""
        return any((self.is_limit_up, self.is_limit_down, self.is_bomb, self.is_one_word))


def apply_ashare_tags_to_bis(
    bis: Iterable[Bi],
    tags: dict[str, AShareDailyTag],
) -> list[Bi]:
    """把 A 股衍生标签挂到 ``Bi`` 的 ``source_ids`` 上（不修改源对象，返回新列表）。

    通过给 ``source_ids`` 注入合成 id（``"ashare:is_limit_up:2026-09-21"``）
    让消费者据此过滤。**不改 ``Bi`` schema**（保持跨市场一致）。

    **端点匹配规则**：每个 bi 的 ``end_time``（毫秒）转 ISO date；若当日任一
    极端标签命中，则把对应合成 id 合并到 ``source_ids``。起点 (``start_time``)
    不检查——起点在涨停日意味着"上涨结束于涨停"，但端点本身是涨停日的收盘
    价（仍可能真实），所以检查末端的标签更稳健（C2 §5.3 原话）。

    实现选择：``source_ids`` 已经承载笔的来源 id（A 股 / BTC / ETH ...），
    再加日期化 id 不冲突。
    """
    out: list[Bi] = []
    for bi in bis:
        d_iso = datetime.fromtimestamp(bi.end_time / 1000, tz=UTC).date().isoformat()
        tag = tags.get(d_iso)
        if tag is None or not tag.has_any_extreme:
            out.append(bi)
            continue
        extra_ids: list[str] = []
        if tag.is_limit_up:
            extra_ids.append(f"ashare:is_limit_up:{d_iso}")
        if tag.is_limit_down:
            extra_ids.append(f"ashare:is_limit_down:{d_iso}")
        if tag.is_bomb:
            extra_ids.append(f"ashare:is_bomb:{d_iso}")
        if tag.is_one_word:
            extra_ids.append(f"ashare:is_one_word:{d_iso}")
        merged = tuple(dict.fromkeys((*bi.source_ids, *extra_ids)))
        out.append(replace(bi, source_ids=merged))
    return out


def t_plus_one_purchase_allowed(previous_close_date_iso: str | None) -> bool:
    """``Signal`` 评估用：给定前一日收盘日期，返回"今日能否买入"。

    加密市场无 T+1 — ``previous_close_date_iso=None`` 表示无前一交易日 → 允许
    （即"首次建仓无前置"）。A 股语义下：若前日已收盘 → 今日允许买入（标的没在
    已持仓名单里）；持仓状态由仓位管理模块决定，本函数**只回答日历约束**。
    """
    if previous_close_date_iso is None:
        return True
    # 实际生产应查询"当前账户持仓 + 是否当日已买入同一标的"；此处只返 True 保持
    # A 股日历允许 — 持仓层面的 T+1 由仓位层负责。
    return True
