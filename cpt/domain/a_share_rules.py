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
``apply_ashare_tags_to_bis``、错误类 ``AShareTagsError``。domain 不 import 任何上层，
也不该出现 SQL 字符串 ——
这条由 CI 门禁「SQL 只许出现在 ``adapters/`` 与 ``storage/``」强制。

按 plan §5.3：
- **C2 涨跌停**：涨停日的笔/中枢**端点可信度低**（涨停挂单买不到、卖单大量堆积），
  需在缠论结构上打 ``is_limit_up`` 标签，让上层画图时用虚线/降透明。±10% 阈值
  不可靠（ST/科创/创业/北交规则不同），**直接读 ``public.derived_bar.is_limit_up``**。
- **C3 停牌**：``public.daily_bar`` 是交易日表，停牌日本来就没行——已在
  :mod:`cpt.adapters.a_share_local` 隐含处理。
- **C4 T+1**：日历约束由 adapters 层的 ``check_t_plus_one_calendar``（查
  ``public.trade_calendar``）回答，再由
  :func:`cpt.application.a_share_snapshot._attach_t_plus_one` 挂进快照的
  ``t_plus_one`` 供前端读；不改 ``Signal`` schema（避免污染跨市场语义——加密
  没有 T+1）。**R66 删除了本模块那个恒返 ``True`` 的 ``t_plus_one_purchase_allowed``**：
  账户持仓与"当日是否已买入同一标的"都不在 CPT（在 emotion-core 的 ``position``
  表），该谓词在这里没有实现基础，留着只会让白名单掩盖一条永不生效的接线目标。
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
