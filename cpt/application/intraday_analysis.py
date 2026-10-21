"""盘中分析（ADR 0003 的「看盘」那一半）。**纯函数，尚未接进任何生产路径。**

## 这个模块回答什么问题

采集层（``cpt/adapters/intraday_probe.py``）只负责「把数字拿回来」。
本模块负责**把它变成人能读的东西** —— 而且是**能直接做决定**的那种：

| 问题 | 本模块的函数 |
|---|---|
| 现在有几只在封板？封得牢不牢？ | :func:`seal_board` |
| 哪些票刚好在往板上打（还没封）？ | :func:`seal_board` 的 ``approaching`` |
| 这批封板的票里，有没有松动（要炸板）的？ | :func:`detect_weakening` |
| 用一个数字概括现在封板情绪 | :func:`board_health` |

## 三条纪律（沿用 R70/R76 的教训）

1. **不给买卖建议**。R70 已实测「一买无 alpha」，R76 把全链路的祈使语气删净。
   本模块只说「几只封着、封单多少、谁在松」，不说「该不该买」。
2. **缺数据不许当 0**。停牌票没档位是 ``None``，不是「买一 0 手」。
   ``bid1_lots is None`` 的票在输出里标 ``unknown``，不参与中位数计算。
3. **数字必须能指回字段**。每个汇总值都要写得清「这是怎么来的」——
   ``median_bid_lots`` 是哪些票的中位数，``weakening`` 是按哪两个条件判的。
"""

from __future__ import annotations

import statistics
from typing import Any, Final

__all__ = [
    "SEAL_HEALTHY_LOTS",
    "board_health",
    "detect_weakening",
    "seal_board",
]

#: 封单「厚」的参考线（手）。**只是分档线，不是判据** ——
#: ADR D3 的触发门槛是 ``SEAL_BID1_VOL_MIN = 30,000`` 手，这里是给看板分
#: 「厚/薄」用的，故意比触发线高（已经持仓的人关心的是「够不够稳」）。
SEAL_HEALTHY_LOTS: Final[float] = 50_000.0


def seal_board(snapshot: dict[str, Any]) -> dict[str, Any]:
    """把一轮采集结果整理成「封板看板」。

    Args:
        snapshot: :meth:`cpt.adapters.intraday_probe.IntradayCollector.scan` 的输出。

    Returns:
        ``{
            "sealed": [...],          # 已封板的票（按封单厚度降序）
            "approaching": [...],     # 距涨停 <=0.5% 但还没封的（往板上打）
            "unavailable": [...],     # 判不了的（缺价/停牌）
            "counts": {"sealed","approaching","unavailable","candidates"},
            "median_bid_lots": float | None,
            "total_bid_lots": float,
        }``

        每行都带 ``code`` / ``name`` / ``bid1_lots`` / ``distance_pct`` /
        ``state``（``sealed`` / ``approaching`` / ``unknown``）。
    """
    quotes: dict[str, dict[str, Any]] = snapshot.get("quotes") or {}
    sealed: list[dict[str, Any]] = []
    approaching: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []

    for cand in snapshot.get("candidates") or []:
        code = str(cand.get("code") or "")
        quote = quotes.get(code)
        if quote is None:
            unavailable.append(
                {
                    "code": code,
                    "name": cand.get("name"),
                    "bid1_lots": None,
                    "distance_pct": cand.get("distance_pct"),
                    "state": "unknown",
                }
            )
            continue

        # 用采集层已经算好的判定结果，**不在这里重算** ——
        # 重算等于第二份口径，两份迟早打架。
        sealed_codes = {s["code"] for s in snapshot.get("sealed") or []}
        if code in sealed_codes:
            row = next(s for s in snapshot["sealed"] if s["code"] == code)
            sealed.append(
                {
                    "code": code,
                    "name": row.get("name") or cand.get("name"),
                    "bid1_lots": row.get("bid1_lots"),
                    "distance_pct": row.get("distance_pct"),
                    "state": "sealed",
                }
            )
        else:
            approaching.append(
                {
                    "code": code,
                    "name": cand.get("name"),
                    "bid1_lots": (
                        None if quote.get("bid1_vol") is None else quote["bid1_vol"] / 100.0
                    ),
                    "distance_pct": cand.get("distance_pct"),
                    "state": "approaching",
                }
            )

    sealed.sort(key=lambda r: -(r["bid1_lots"] or 0.0))
    approaching.sort(key=lambda r: abs(r["distance_pct"] or 0.0))

    lots = [r["bid1_lots"] for r in sealed if r["bid1_lots"] is not None]
    return {
        "sealed": sealed,
        "approaching": approaching,
        "unavailable": unavailable,
        "counts": {
            "sealed": len(sealed),
            "approaching": len(approaching),
            "unavailable": len(unavailable),
            "candidates": len(snapshot.get("candidates") or []),
        },
        "median_bid_lots": float(statistics.median(lots)) if lots else None,
        "total_bid_lots": float(sum(lots)),
    }


def detect_weakening(
    board: dict[str, Any],
    *,
    threshold_lots: float = SEAL_HEALTHY_LOTS,
) -> list[dict[str, Any]]:
    """挑出「封单薄、可能松动」的已封板票。

    判据只有一条，且故意保守：**买一量低于 ``threshold_lots``**。
    这里**不判**「相对自己封板时的量衰减了多少」—— 那个基准（``seal_bid1_vol``）
    由带状态的 ``SealTracker`` 提供，属于轮间比较；本模块拿到的是单轮快照，
    拿不到基准。要跨轮判定，调用方应当**另外**用
    ``emotion_core.algorithms.intraday.seal_weaken``。

    为什么这条判据有用：封单从 30 万手掉到 3 万手是明确的松动信号，
    而看绝对值的下降不需要历史状态。

    Args:
        board: :func:`seal_board` 的输出。
        threshold_lots: 分档线（手）。低于它算薄。

    Returns:
        薄封的票（按买一手数升序，最薄的在前）。每条带 ``reason`` 一句话。
    """
    out: list[dict[str, Any]] = []
    for row in board.get("sealed") or []:
        lots = row.get("bid1_lots")
        if lots is None:
            # 封着但没有量数据 —— 这件事本身值得知道，但不该说成「薄」
            out.append({**row, "reason": "封着但买一量取不到（数据缺失，不是薄）"})
            continue
        if lots < threshold_lots:
            out.append(
                {
                    **row,
                    "reason": f"封单 {lots:,.0f} 手，低于 {threshold_lots:,.0f} 手的厚线",
                }
            )
    out.sort(key=lambda r: (r.get("bid1_lots") is None, r.get("bid1_lots") or 0.0))
    return out


def board_health(board: dict[str, Any]) -> dict[str, Any]:
    """用一个数字概括现在封板情绪：``board_strength``。

    定义（写清楚，因为这是自造指标）：

    ``board_strength = median_bid_lots / SEAL_HEALTHY_LOTS``
    —— 中位封单量相对「厚线」的倍数，**截断到 [0, 3]**。

    为什么用中位数不用均值：单只 105 万手的票（实测三峡新材）会把均值拉爆，
    而中位数反映的是「典型的一只封成什么样」。

    为什么截断到 3：超过 3 倍厚线之后，再多一倍的**信息量**远小于数字的
    变化幅度 —— 不截断会让一个指标在极值上失去区分度。

    Returns:
        ``{"board_strength": float | None, "label": str, "explain": str,
        "sealed": int, "median_bid_lots": float | None}``

        ``board_strength is None`` 表示**没有封板样本**，不是「情绪极低」。
    """
    median = board.get("median_bid_lots")
    sealed = int((board.get("counts") or {}).get("sealed") or 0)
    if median is None or sealed == 0:
        return {
            "board_strength": None,
            "label": "没样本",
            "explain": "这一轮没有任何票封在板上（或全部取不到买一量），无法评价封板强度。",
            "sealed": 0,
            "median_bid_lots": None,
        }

    raw = median / SEAL_HEALTHY_LOTS
    strength = max(0.0, min(3.0, raw))
    if strength >= 2.0:
        label = "厚"
    elif strength >= 1.0:
        label = "正常"
    elif strength >= 0.5:
        label = "偏薄"
    else:
        label = "薄"

    explain = (
        f"{sealed} 只封在板上，封单量中位 {median:,.0f} 手"
        f"（厚线 {SEAL_HEALTHY_LOTS:,.0f} 手，比值 {raw:.2f}"
        + ("，已截断到 3" if raw > 3.0 else "")
        + "）。"
    )
    return {
        "board_strength": strength,
        "label": label,
        "explain": explain,
        "sealed": sealed,
        "median_bid_lots": median,
    }
