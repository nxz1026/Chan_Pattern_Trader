"""A 股规则标签（C2/C4 — R15-3）。

按 plan §5.3：
- **C2 涨跌停**：涨停日的笔/中枢**端点可信度低**（涨停挂单买不到、卖单大量堆积），
  需在缠论结构上打 ``is_limit_up`` 标签，让上层画图时用虚线/降透明。±10% 阈值
  不可靠（ST/科创/创业/北交规则不同），**直接读 ``public.derived_bar.is_limit_up``**。
- **C3 停牌**：``public.daily_bar`` 是交易日表，停牌日本来就没行——已在
  :mod:`cpt.adapters.a_share_local` 隐含处理。
- **C4 T+1**：在 ``Signal`` 上打 ``t_plus_one: bool``，由 :mod:`cpt.domain.signal`
  在评估一买/一卖时读取。**本模块提供查询工具**，不直接改 ``Signal`` schema
  （避免污染跨市场语义——加密没有 T+1）。
- **C5 非交易日**：`public.daily_bar` 天然不画图，不需额外处理。

## 决定
``public.derived_bar`` 已有 ``is_limit_up / is_bomb / is_one_word / touched_limit``
等列（plan §5.2 表列）。本模块不重新计算涨跌停（避免出错路径），直接查
``derived_bar`` 把标签挂到 ``CanonicalBar`` 上。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from cpt.domain.models import Bi

__all__ = [
    "AShareDailyTag",
    "apply_ashare_tags_to_bis",
    "AShareTagsError",
]

# 涨跌停标签——一旦某日触发，整根 bar 都受影响（开盘涨停 / 收盘涨停 / 一字板）
_DERIVED_FIELDS = (
    "is_limit_up",  # 收盘涨停
    "is_limit_down",  # 收盘跌停（用于对称展示）
    "is_bomb",  # 炸板（封板后开板）
    "is_one_word",  # 一字板（开/收/高/低全相等）
)


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


def fetch_daily_tags(conn: Any, code: str, start_ms: int, end_ms: int) -> dict[str, AShareDailyTag]:
    """从 ``public.derived_bar`` 拉取区间内的衍生标签。

    :param conn: psycopg 连接（测试中可注入 mock）
    :returns: ``{iso_date: AShareDailyTag}``；缺失日期不出现在 dict 中
    """
    start_d = datetime.fromtimestamp(start_ms / 1000, tz=UTC).date()
    end_d = datetime.fromtimestamp(end_ms / 1000, tz=UTC).date()
    bare_code = code.split(".", 1)[0]

    fields_sql = ", ".join(_DERIVED_FIELDS)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT date, {fields_sql}
                FROM public.derived_bar
                WHERE code = %s AND date BETWEEN %s AND %s""",
            (bare_code, start_d, end_d),
        )
        out: dict[str, AShareDailyTag] = {}
        for row in cur.fetchall():
            d = row[0]
            d_iso = d.isoformat() if hasattr(d, "isoformat") else str(d)
            tag = AShareDailyTag(
                code=bare_code,
                trade_date=d_iso,
                is_limit_up=bool(row[1]),
                is_limit_down=bool(row[2]),
                is_bomb=bool(row[3]),
                is_one_word=bool(row[4]),
            )
            out[tag.trade_date] = tag
    return out


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
