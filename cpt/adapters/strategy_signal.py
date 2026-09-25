"""A 股策略观察信号取数（读 ``public.strategy_signal``）。

数据来源是 **longkonglong 仓库**（``/stock-legacy/``，旧版股票复盘）的策略观察台：
它的 runner 同时写 JSON 快照与这张表（``lkl/strategy/runner.py``）—— 页面读 JSON、
本模块读表。两者同源，取表的好处是 CPT 与它**同库**，不必跨仓调 HTTP，也不必把
另一个仓库的文件系统路径写进 CPT。

⚠️ 三个必须知道的坑（都是实查踩到的）：

1. ``strategy_signal.name`` 是**策略名称**（实值如「默认多头趋势」），**不是股票名**。
   股票名要另查 ``stock_basic``（见 ``cpt.web.a_share_routes._names``）。字段名
   一样但语义不同，直接拿来显示会把「默认多头趋势」当股票名写进下拉。
2. ``confidence`` 是 ``numeric(4,3)``，psycopg 回的是 ``Decimal``，**必须转 float**
   再参与排序/输出 —— ``Decimal`` 与 ``float`` 混算抛 ``TypeError``，
   且 ``Decimal`` 无法被 ``json.dumps`` 序列化。
3. **``score`` 与 ``confidence`` 在本数据里是负相关的**（实查 2026-09-24：
   ``000607`` 评分 20 / 置信 0.90，``000823`` 评分 35 / 置信 0.80）。即"低分高置信"
   的票几乎都是 ``PASS``。只按综合分排序会把它们拉进 Top5 —— 这正是
   ``ELIGIBLE_ACTIONS`` 存在的理由：**先滤动作，再综合排序**（2026-09-25 拍板口径 D）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "CONFIDENCE_WEIGHT",
    "ELIGIBLE_ACTIONS",
    "SCORE_WEIGHT",
    "StrategyPick",
    "combined_score",
    "fetch_strategy_top",
]

#: 只有这两种动作进入候选。``PASS`` 不进 —— 见模块 docstring 第 3 条。
ELIGIBLE_ACTIONS: frozenset[str] = frozenset({"BUY", "WATCH"})

#: 综合分的两项权重。评分与置信都归一到 0-100 后各占一半。
#: 当前数据下改这两个常数**不影响** Top5 顺序（候选只有 3 只且无并列），
#: 但换到候选更多的日子就会影响，所以显式留在这里而不是内联写死。
SCORE_WEIGHT: float = 0.5
CONFIDENCE_WEIGHT: float = 0.5


def combined_score(score: int, confidence: float) -> float:
    """综合分 = ``评分×0.5 + 置信×100×0.5``（两项都归一到 0-100）。"""
    return score * SCORE_WEIGHT + confidence * 100.0 * CONFIDENCE_WEIGHT


@dataclass(frozen=True)
class StrategyPick:
    """一条策略观察候选。"""

    code: str
    #: ⚠️ 这是**策略名称**（如「默认多头趋势」），不是股票名。
    strategy_name: str
    strategy: str  # 策略标识（如 bull_trend）
    action: str  # BUY | WATCH（PASS 已在取数时滤掉）
    score: int  # 0-100
    confidence: float  # 0-1（已从 Decimal 转 float）
    combined: float  # 综合分
    trade_date: str  # ISO date
    reason: str
    model: str | None


def fetch_strategy_top(
    conn: Any,
    *,
    limit: int | None = 5,
    eligible: frozenset[str] = ELIGIBLE_ACTIONS,
) -> list[StrategyPick]:
    """最新交易日里按综合分取前 ``limit`` 只（同一代码只留最好的一条）。

    过滤与排序**放在 Python 而不是 SQL**：``eligible`` 是**可调口径**（用户拍板的
    业务规则），放进 SQL 字符串就只能靠真库才能验证；放这里可以用假游标覆盖。

    同一 ``code`` 可能因 ``prompt_hash`` / ``strategy`` 不同而有多行（表的唯一键是
    ``(trade_date, code, strategy, prompt_hash)``）—— 按综合分保留最高的一条，
    否则同一只票会在下拉里出现两次。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT max(trade_date) FROM public.strategy_signal")
        row = cur.fetchone()
        trade_date = row[0] if row else None
        if trade_date is None:
            return []
        cur.execute(
            """SELECT code, strategy, name, action, score, confidence, reason, model
               FROM public.strategy_signal
               WHERE trade_date = %s
               ORDER BY code, score DESC, confidence DESC""",
            (trade_date,),
        )
        rows = cur.fetchall()

    best: dict[str, StrategyPick] = {}
    for code, strategy, name, action, score, confidence, reason, model in rows:
        if action not in eligible:
            continue
        pick = StrategyPick(
            code=str(code),
            strategy_name=str(name or ""),
            strategy=str(strategy),
            action=str(action),
            score=int(score),
            confidence=float(confidence),  # Decimal → float，见 docstring 第 2 条
            combined=combined_score(int(score), float(confidence)),
            trade_date=trade_date.isoformat(),
            reason=str(reason or ""),
            model=str(model) if model is not None else None,
        )
        current = best.get(pick.code)
        if current is None or pick.combined > current.combined:
            best[pick.code] = pick

    ordered = sorted(best.values(), key=lambda p: (-p.combined, -p.score, p.code))
    return ordered if limit is None else ordered[:limit]
