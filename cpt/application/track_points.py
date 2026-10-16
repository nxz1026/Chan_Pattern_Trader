"""我的追踪 — BUY / SELL 建议点计算（段 1 核心新逻辑）。

## 算法

每点出 **两个版本**（段 1 决定）：

- ``reference``：永远有（当有结构数据时）；算结构性"参考位"，不保证现在就该买/卖。
- ``confirmed``：只有当信号状态确凿（``confirmed``）时才有；与 ``reference`` 不同源
  —— 是**当下应该执行**的位。

另带 ``stop_loss_reference``，仅在 ``note`` 含"长期"或 BUY.reference 存在时给出。

## 单位

输入：v2 snapshot 的 ``overlays`` 在**后复权**域（按快照默认口径）。同时输入
``price_ratio``（来自快照后的 ``build_recommendation``）。输出：**转回 raw 域**，
这样用户看到的数与他能在交易机下单的数**同口径**。

⚠️ ``price_ratio`` 为 ``None`` 时不做除法（除零风险），整段建议点 ``null``。

## 数据来源

``cpt.domain.models.{Bi, ZhongShu}`` 字段是结构化的（``high`` / ``low`` / ``direction``），
但 snapshot 的 ``overlays`` 是 ``asdict`` 后的 dict。本模块用 duck typing：
``obj["high"]`` 与 ``getattr(obj, "high", None)`` 都认，避免因序列化层变一下就改业务代码。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Final

_LOG = logging.getLogger(__name__)

__all__ = [
    "BUY_BUFFER",
    "SELL_BUFFER",
    "STOP_LOSS_RATIO",
    "compute_points",
]

#: BUY reference 的缓冲：在算出的 raw 价上**再上调 2%**，给行情波动留余地
#: （基本面：中枢下沿是"触底即反弹"的参考，2% 缓冲避免刚好擦边进不去）
BUY_BUFFER: Final[float] = 1.02
#: SELL reference 的缓冲：在算出的 raw 价上**再下调 3%**
#: （阻力位附近"摸一下"是常态，3% 缓冲降低"差一分钱没到"的挫败）
SELL_BUFFER: Final[float] = 0.97
#: 止损相对 BUY reference 的比例
STOP_LOSS_RATIO: Final[float] = 0.95

#: 双向各取最近 N 笔 + 全部中枢（一般 1-2 个）
_BI_LOOKBACK: Final[int] = 3
_ZHONGSHU_LOOKBACK: Final[int] = 2


def _h(obj: Any) -> float | None:
    """结构对象的 ``high``（dict 或 dataclass 都认）。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        v = obj.get("high")
    else:
        v = getattr(obj, "high", None)
    return float(v) if v is not None else None


def _l(obj: Any) -> float | None:
    """结构对象的 ``low``。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        v = obj.get("low")
    else:
        v = getattr(obj, "low", None)
    return float(v) if v is not None else None


def _direction(obj: Any) -> int | None:
    """笔的方向 ``+1`` 向上 / ``-1`` 向下。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        v = obj.get("direction")
    else:
        v = getattr(obj, "direction", None)
    return int(v) if v is not None else None


def _to_raw(adjusted_price: float, price_ratio: float | None) -> float | None:
    """后复权 → raw。``price_ratio`` 不可用时返 None，不静默用错数。"""
    if price_ratio is None or price_ratio <= 0:
        return None
    return adjusted_price / price_ratio


def _last_bar_day(snapshot: dict[str, Any]) -> str | None:
    """快照末根 K 线的日期（ISO 字符串）；取不到 → ``None``。

    只用于撰写给人看的 ``reason`` 文案，异常一律吞掉（同 ``_to_raw`` 的「宁缺勿错」）。
    """
    candles = snapshot.get("candles")
    if not isinstance(candles, list) or not candles:
        return None
    last = candles[-1]
    if not isinstance(last, dict):
        return None
    open_time = last.get("open_time")
    if not isinstance(open_time, (int, float)) or isinstance(open_time, bool):
        return None
    try:
        return datetime.fromtimestamp(open_time / 1000, tz=UTC).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _ratio_reason(snapshot: dict[str, Any]) -> str:
    """``price_ratio`` 不可用时给前端的一句解释（R4/S0 · C4）。

    加这个键之前，面板只能显示一排「—」，用户分不清「没有结构数据」与
    「有结构但换算不出 raw」。``dashboard/dash-track.js`` 的 points 折叠
    已有「有 ``reason`` 才渲染 hint」的分支，键一出现即生效。
    """
    day = _last_bar_day(snapshot)
    if day is None:
        return "price_ratio 不可用（快照缺少末根 K 线日期）→ 结构位无法换算为 raw"
    return f"price_ratio 不可用（快照末根 {day} 与 daily_bar 无同日配对）→ 结构位无法换算为 raw"


# ── 主入口 ────────────────────────────────────────────────────────────


def compute_points(
    snapshot: dict[str, Any],
    recommendation: dict[str, Any],
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """算 BUY / SELL 两点的双版本。

    :param snapshot: ``snapshot_payload(code)`` 的产物（含 ``overlays.bis`` /
        ``overlays.zhongshus`` / ``candles``）。
    :param recommendation: ``build_recommendation`` 的产物（含 ``status`` /
        ``action`` / ``raw_close`` / ``price_ratio``）。
    :param note: 该追踪行的备注（"长期持有"会让 ``stop_loss_reference`` 出现）。
    :returns: ``suggested_points`` dict（见 schema 文档）；任一关键量缺则该字段为 ``None``。
        另含可选 ``reason``（R4/S0·C4）：五槽为空或止损被 note 门槛挡掉时，
        给一句可直接展示给用户的中文解释；五槽齐全且止损正常时**不带**该键。
    """
    overlays = snapshot.get("overlays") or {}
    bis = list(overlays.get("bis") or [])
    zhongshus = list(overlays.get("zhongshus") or [])

    rec = recommendation or {}
    price_ratio = rec.get("price_ratio")
    if not isinstance(price_ratio, (int, float)) or price_ratio <= 0:
        # 没 ratio 就不能把结构位转 raw；直接给 None，不假装有数。
        # R4/S0·C4：连原因一起给 —— 否则前端只有一排「—」，
        # 用户分不清「本来就没有结构」与「有结构但换算不出来」。
        return {
            "buy": _empty_sides(),
            "sell": _empty_sides(),
            "stop_loss_reference": None,
            "reason": _ratio_reason(snapshot),
        }

    buy_ref = _buy_reference(bis, zhongshus, price_ratio)
    sell_ref = _sell_reference(bis, zhongshus, price_ratio)

    status = str(rec.get("status") or "").lower()
    is_confirmed_buy = status == "confirmed" and rec.get("action") in ("buy", "watch")
    is_confirmed_sell = status == "confirmed" and rec.get("action") == "sell"

    # confirmed：当下应该执行的位。
    # 严格用最近一笔的低/高点（结构位），与 reference 取**不同源**：
    #   - reference：取"所有近期低点/高点"中较保守的一个（用中位/较低位）
    #   - confirmed：取**最新一笔**的低/高点（更贴近当前结构）+ 0 缓冲
    # 这样数字接近但不一致，UI 渲染时**两个同时**出现会告诉用户：
    # "现在你确实可以动手；参考位告诉你结构更稳的位在哪里。"
    raw_close = rec.get("raw_close")
    last_bi = bis[-1] if bis else None
    last_zs = zhongshus[-1] if zhongshus else None
    buy_confirmed = (
        _confirmed_point(last_bi, last_zs, price_ratio, "buy") if is_confirmed_buy else None
    )
    sell_confirmed = (
        _confirmed_point(last_bi, last_zs, price_ratio, "sell") if is_confirmed_sell else None
    )

    # 止损：仅在有 BUY reference 且 note 含"长期"时显示
    note_norm = (note or "").lower()
    show_stop = "长期" in (note or "") or "long" in note_norm or "hold" in note_norm
    stop = (
        round(buy_ref["price_level"] * STOP_LOSS_RATIO, 2)
        if show_stop and buy_ref and buy_ref.get("price_level")
        else None
    )

    out: dict[str, Any] = {
        "buy": {
            "reference": buy_ref,
            "confirmed": buy_confirmed,
        },
        "sell": {
            "reference": sell_ref,
            "confirmed": sell_confirmed,
        },
        "stop_loss_reference": stop,
        "raw_close": raw_close,
        "as_of_basis": "后复权→raw 转换价 = adjusted ÷ price_ratio",
    }
    if stop is None and buy_ref and buy_ref.get("price_level"):
        # 有买点却给不出止损 ⇒ 唯一原因是该追踪行没标注长期持有（门槛②，docs/18 §4.3）。
        # 说出来，用户才知道止损为什么空、以及怎么让它出现。
        out["reason"] = (
            "止损未给出：该追踪行未标注「长期持有」"
            "（note 含「长期」/long/hold 才输出 stop_loss_reference）"
        )
    return out


# ── 内部 ──────────────────────────────────────────────────────────────


def _empty_sides() -> dict[str, Any]:
    return {"reference": None, "confirmed": None}


def _buy_reference(
    bis: list[Any], zhongshus: list[Any], price_ratio: float
) -> dict[str, Any] | None:
    """参考买点 = 较保守的下沿。

    优先用中枢下沿（结构比单笔强），其次最近 N 笔低点。**取最低**后上调 2% 留缓冲。
    """
    candidates: list[tuple[str, float]] = []
    for zs in zhongshus[-_ZHONGSHU_LOOKBACK:]:
        low = _l(zs)
        if low is not None:
            candidates.append((f"中枢下沿 {low:.2f}", low))

    for bi in bis[-_BI_LOOKBACK:]:
        # 向下笔的 low 才是"低点"参考（向上笔的 high 不算买点附近的参考）
        if _direction(bi) == -1:
            low = _l(bi)
            if low is not None:
                candidates.append((f"向下笔低点 {low:.2f}", low))

    if not candidates:
        return {
            "price_level": None,
            "condition": "无结构数据，刷新后再看",
            "rationale": "overlays.bis / zhongshus 为空（可能是 degraded snapshot）",
        }

    # 取最低（最保守：让价格真正下来才到）
    label, raw_adj = min(candidates, key=lambda x: x[1])
    raw_price = _to_raw(raw_adj, price_ratio)
    if raw_price is None:
        return None
    final = round(raw_price * BUY_BUFFER, 2)
    return {
        "price_level": final,
        "condition": "若价格回到此区间且 structure_ready 形成",
        "rationale": (
            f"取最保守下沿：{label}（adjusted）× {BUY_BUFFER} 缓冲 ÷ {price_ratio:.4f} 倍率"
        ),
    }


def _sell_reference(
    bis: list[Any], zhongshus: list[Any], price_ratio: float
) -> dict[str, Any] | None:
    """参考卖点 = 较保守的上方。

    优先用中枢上沿，其次最近 N 笔高点（向上笔）。**取最高**后下调 3% 留缓冲。
    """
    candidates: list[tuple[str, float]] = []
    for zs in zhongshus[-_ZHONGSHU_LOOKBACK:]:
        high = _h(zs)
        if high is not None:
            candidates.append((f"中枢上沿 {high:.2f}", high))

    for bi in bis[-_BI_LOOKBACK:]:
        if _direction(bi) == 1:
            high = _h(bi)
            if high is not None:
                candidates.append((f"向上笔高点 {high:.2f}", high))

    if not candidates:
        return {
            "price_level": None,
            "condition": "无结构数据，刷新后再看",
            "rationale": "overlays.bis / zhongshus 为空（可能是 degraded snapshot）",
        }

    # 取最高（最保守：等价格真正上去才卖）
    label, raw_adj = max(candidates, key=lambda x: x[1])
    raw_price = _to_raw(raw_adj, price_ratio)
    if raw_price is None:
        return None
    final = round(raw_price * SELL_BUFFER, 2)
    return {
        "price_level": final,
        "condition": "若价格上行至此区间或出现一卖信号",
        "rationale": (
            f"取最保守上沿：{label}（adjusted）× {SELL_BUFFER} 缓冲 ÷ {price_ratio:.4f} 倍率"
        ),
    }


def _confirmed_point(
    last_bi: Any, last_zs: Any, price_ratio: float, side: str
) -> dict[str, Any] | None:
    """确凿点 = **最新一笔**的低/高点 + 0 缓冲（与 reference 同源不同采）。

    仅在 ``signal.status == "confirmed"`` 且 ``action`` 匹配时被调。
    """
    obj = last_zs if last_zs is not None else last_bi
    if obj is None:
        return None
    if side == "buy":
        adj = _l(obj)
        if adj is None:
            return None
        raw = _to_raw(adj, price_ratio)
        if raw is None:
            return None
        return {
            "price_level": round(raw, 2),
            "condition": "信号已确凿 + 价格处于此区间",
            "rationale": f"取最新结构的下沿 {adj:.2f}（adjusted）÷ {price_ratio:.4f}",
        }
    if side == "sell":
        adj = _h(obj)
        if adj is None:
            return None
        raw = _to_raw(adj, price_ratio)
        if raw is None:
            return None
        return {
            "price_level": round(raw, 2),
            "condition": "信号已确凿 + 价格处于此区间",
            "rationale": f"取最新结构的上沿 {adj:.2f}（adjusted）÷ {price_ratio:.4f}",
        }
    return None
