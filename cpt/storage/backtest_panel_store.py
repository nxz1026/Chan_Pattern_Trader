"""回测面板的取数（SQL 住在这里，符合分层门禁）。

``cpt/backtest/panel.py`` 只做「行 → :class:`~cpt.backtest.panel.BarPanel`」
的装配，**不写 SQL**。分开的原因见 ``scripts/check_sql_layering.py``：
SQL 只许在 ``cpt/adapters/`` 与 ``cpt/storage/``，回测层不例外。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any, Final

__all__ = ["PANEL_SQL", "PANEL_SCOPE_SQL", "fetch_panel_rows", "latest_market_day"]

#: 面板查询的骨架。``{scope}`` 由 :data:`PANEL_SCOPE_SQL` 填。
#:
#: ## 为什么 ``LEFT JOIN derived_bar``
#: ``daily_bar`` 有 221 行在 ``derived_bar`` 里没有对应行（实测 3,445,867 vs
#: 3,445,646）。用 ``INNER JOIN`` 会**悄悄少 221 行**，而那正好是最近的数据 ——
#: 缺的几日会造出假的「数据缺口」。JOIN 不到标签时列是 ``NULL``，由装配层按
#: 「未标记涨停」处理（保守方向）。
#:
#: ## 为什么必须 ``LEFT JOIN asel.ref_adjust_factor``
#: ``daily_bar.close`` 是**未复权原始价**。直接拿它算持有收益会在除权日
#: 造出假跌：10 送 10 ⇒ 前收 20 元、除权开 10 元，原始价算出来是 -50%，
#: 而真实收益是 0。
#:
#: 实测（300 只票、76,798 个 h20 样本，R86）：
#:   后复权 mean=+0.133% / 原始价 mean=+0.031%，胜率 44.7% vs 44.3%，
#:   两套口径有 29 个样本差异超过 15 个百分点。
#: 也就是说用原始价做回测，**基线本身就被除权污染**。
#:
#: 复权口径选**后复权**（``hfq_factor``），与 ``fetch_validated_klines`` 一致：
#: 结构层拿到的就是后复权价，收益层必须同口径，否则「信号在 10 元触发、
#: 20 元卖出」这种收益对不上结构图中看到的价格。
#:
#: 缺因子时 ``hfq_factor`` 为 ``NULL`` ⇒ 回退到原始价，**并在装配层记
#: ``adjusted=False``**（不让「没复权」悄悄混进统计）。
PANEL_SQL: Final[str] = """
    SELECT d.code, d.date, d.open, d.high, d.low, d.close, d.pre_close, d.volume,
           b.is_limit_up, b.is_limit_down, b.is_bomb, b.is_one_word, b.touched_limit,
           f.hfq_factor
    FROM public.daily_bar d
    LEFT JOIN public.derived_bar b ON b.code = d.code AND b.date = d.date
    LEFT JOIN asel.ref_adjust_factor f ON f.code = d.code AND f.trade_date = d.date
    WHERE d.date BETWEEN %s AND %s
      AND d.close > 0 AND d.open > 0 AND d.high > 0 AND d.low > 0
    {scope}
    ORDER BY d.code, d.date
"""

#: 可选的票范围过滤。``{placeholder}`` 始终是 ``%s``（psycopg 参数化）。
PANEL_SCOPE_SQL: Final[str] = "AND d.code = ANY(%s)"


def fetch_panel_rows(
    conn: Any,
    *,
    start: dt.date,
    end: dt.date,
    codes: Sequence[str] | None = None,
) -> list[tuple[Any, ...]]:
    """按 ``(code, date)`` 升序返回原始行。

    Args:
        conn: psycopg 连接。
        start: 起始日期（含）。
        end: 结束日期（含）。
        codes: 只取这些 6 位代码（``None`` = 全部）。

    Returns:
        每行 14 列，顺序同 :data:`PANEL_SQL` 的 SELECT 列表；最后一列是
        ``hfq_factor``（可为 ``NULL``）。
    """
    scope = "" if codes is None else PANEL_SCOPE_SQL
    sql = PANEL_SQL.format(scope=scope)
    params: list[Any] = [start, end]
    if codes is not None:
        params.append(list(codes))
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall())


def latest_market_day(conn: Any) -> dt.date | None:
    """``public.daily_bar`` 里的最新交易日；表空时返 ``None``。

    界面用它区分「今天休市」（库里最新交易日落后于今天，正常）与
    「采集挂了」（同样的日期差，但不正常）。两者必须分开说 —— 合成一句
    「没数据」会让人不知道该等还是该查。

    SQL 住在这里而不是调用方（``cpt/web/app.py``）：分层门禁要求 SQL 只许
    出现在 ``cpt/adapters/`` 与 ``cpt/storage/``。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT max(date) FROM public.daily_bar")
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    value = row[0]
    # ``date`` 列在 psycopg 里给 ``date``；某些配置下给 ``datetime`` —— 统一成 date。
    if isinstance(value, dt.datetime):
        return value.date()
    return value if isinstance(value, dt.date) else None
