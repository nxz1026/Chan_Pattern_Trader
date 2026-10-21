"""回测面板：一次把某段时间的 K 线 + 涨跌停标签 + 前视收益装进内存。

## 设计取舍（都是踩过坑才这么写的）

**为什么是「面板」而不是「逐票迭代器」**
R70 的探针每只票单独查 ``derived_bar``，5,131 只票 = 5,131 次往返，
光取数就占了大半时间。本模块一次 ``COPY``-式扫描把区间内所有票拉进来，
查询次数从「票数」降到「1」。

**为什么收益在面板里预先算好**
前视收益的窗口定义（``h1``/``h5``/``h10``/``h20``）一旦分散到各各处
就会漂：有人用「信号日收盘→第 N 日收盘」，有人用「次日开盘→第 N 日收盘」。
本模块**只提供一种**：``(close[t+h] / close[t] - 1)``，且 ``t+h`` 必须还是
**同一只票**的同一段连续数据（中间停牌不跨越）。

**为什么默认排除涨停**
涨停当天买入是假想成交。``tradable_only=True``（默认）把
``is_limit_up=True`` 的**信号日**整个剔掉 —— 那天你根本买不进去。
跌停同理（卖不出），但只在做空/卖出侧才需要，本层统一给列、默认不剔。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from cpt.storage.backtest_panel_store import fetch_panel_rows

__all__ = [
    "HORIZONS",
    "PANEL_COLUMNS",
    "BarPanel",
    "load_panel",
]

#: 前视窗口（交易日根数）。R70 用过的四个，不多不少。
HORIZONS: Final[tuple[int, ...]] = (1, 5, 10, 20)

#: 每根 bar 一行的列名（顺序即 SQL 里的顺序）。
PANEL_COLUMNS: Final[tuple[str, ...]] = (
    "code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "is_limit_up",
    "is_limit_down",
    "is_bomb",
    "is_one_word",
    "touched_limit",
)


@dataclass(frozen=True)
class BarPanel:
    """一只票的连续 bar 序列 + 预先算好的前视收益。

    不存 ``datetime``，存 **Unix 毫秒**：下游（结构层、收益层）全都按毫秒走，
    在边界处转换会在夏令时/时区上出岔（本机是 UTC，但不要赌）。

    Attributes:
        code: 6 位纯代码（不带市场前缀）。
        dates_ms: 每根 bar 的日期（Unix 毫秒，UTC 零点）。
        open/high/low/close/pre_close: 价格序列。
        volume: 成交量。
        is_limit_up/is_limit_down/is_bomb/is_one_word/touched_limit: 涨跌停标签。
        returns: ``{(h, index): pct}``。``pct`` 是**小数**（0.0123 = +1.23%），
            不是百分数。缺窗口（样本末尾不足 h 根）不出现在 dict 里。
    """

    code: str
    dates_ms: tuple[int, ...]
    open: tuple[float, ...]
    high: tuple[float, ...]
    low: tuple[float, ...]
    close: tuple[float, ...]
    pre_close: tuple[float, ...]
    volume: tuple[float, ...]
    is_limit_up: tuple[bool, ...]
    is_limit_down: tuple[bool, ...]
    is_bomb: tuple[bool, ...]
    is_one_word: tuple[bool, ...]
    touched_limit: tuple[bool, ...]
    returns: dict[tuple[int, int], float] = field(default_factory=dict)
    #: 每根 bar 的价格是否**已复权**（``hfq_factor`` 取到了）。缺因子时为 ``False``。
    #: 收益只用复权价算；缺因子的位置整个不出现在 ``returns`` 里 ——
    #: 拿原始价算就会在除权日造出假跌（见 :data:`cpt.storage.backtest_panel_store.PANEL_SQL`）。
    adjusted: tuple[bool, ...] = ()

    def __len__(self) -> int:
        return len(self.dates_ms)

    def return_at(self, h: int, i: int) -> float | None:
        """第 ``i`` 根之后 ``h`` 根的前视收益（小数）；窗口不足返 ``None``。"""
        return self.returns.get((h, i))

    def is_tradable(self, i: int) -> bool:
        """第 ``i`` 根当天能不能买入 —— 涨停买不进。"""
        return not self.is_limit_up[i]


def load_panel(
    conn: Any,
    *,
    start: dt.date,
    end: dt.date,
    codes: Sequence[str] | None = None,
    min_bars: int = 250,
    tradable_only: bool = True,
) -> dict[str, BarPanel]:
    """一次扫描把区间内的数据装成 ``{code: BarPanel}``。

    Args:
        conn: psycopg 连接。
        start: 起始日期（含）。
        end: 结束日期（含）。前视收益会用到 ``end`` 之后的数据 —— **不要**
            把 ``end`` 设成「今天」就以为拿到了完整窗口，末 20 根的 ``h20``
            会是 ``None``。要完整窗口，``end`` 至少往前推 20 个交易日。
        codes: 只取这些票（``None`` = 全部）。上层别传 5000 只再自己过滤，
            让 SQL 用 ``= ANY(...)`` 过滤。
        min_bars: 少于这么多根的票整个丢掉。R70 用 250（结构层冷启动需要的量）。
        tradable_only: 默认 ``True`` —— 把涨停日的「买入机会」剔除。**注意**
            剔除的是「信号日涨停」的样本，不是「未来某天涨停」的样本；后者仍
            计入收益（你买进去之后它涨停了，那是真实的）。

    Returns:
        ``{code: BarPanel}``，按代码升序。
    """
    # 多取 30 个自然日，保证 end 附近还有足够的 bar 算 h20 收益。
    query_end = end + dt.timedelta(days=30)
    rows = fetch_panel_rows(conn, start=start, end=query_end, codes=codes)

    buckets: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        buckets.setdefault(str(row[0]), []).append(row)

    out: dict[str, BarPanel] = {}
    for code, code_rows in buckets.items():
        panel = _build_panel(code, code_rows, end=end, tradable_only=tradable_only)
        if panel is not None and len(panel) >= min_bars:
            out[code] = panel
    return dict(sorted(out.items()))


def _build_panel(
    code: str,
    rows: Iterable[tuple[Any, ...]],
    *,
    end: dt.date,
    tradable_only: bool,
) -> BarPanel | None:
    """把一个代码的行序列装成 :class:`BarPanel`。

    ``tradable_only=True`` 时，**涨停日之后的窗口起点也算不可买** ——
    准确说：只在计算收益时把「信号日涨停」的 ``returns`` 条目去掉。
    这样面板本身仍然完整（结构层要看到全部 bar），只是那个位置不可用。
    """
    dates: list[int] = []
    op: list[float] = []
    hi: list[float] = []
    lo: list[float] = []
    cl: list[float] = []
    pc: list[float] = []
    vol: list[float] = []
    up: list[bool] = []
    dn: list[bool] = []
    bomb: list[bool] = []
    one_word: list[bool] = []
    touched: list[bool] = []
    usable: list[bool] = []
    adj: list[bool] = []

    for r in rows:
        d = r[1]
        if isinstance(d, dt.datetime):
            d = d.date()
        dates.append(int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).timestamp() * 1000))
        op.append(float(r[2]))
        hi.append(float(r[3]))
        lo.append(float(r[4]))
        cl.append(float(r[5]))
        pc.append(float(r[6]) if r[6] is not None else 0.0)
        vol.append(float(r[7]) if r[7] is not None else 0.0)
        # LEFT JOIN 可能给出 NULL（daily_bar 有、derived_bar 缺）—— 按 False 处理，
        # 并且**不**把它当成「非涨停」。真相是「不知道」，但标签列只有布尔，
        # 记 False 意味着「未标记涨停」，这是保守方向（不会误剔样本）。
        up.append(bool(r[8]))
        dn.append(bool(r[9]))
        bomb.append(bool(r[10]))
        one_word.append(bool(r[11]))
        touched.append(bool(r[12]))
        # 第 13 列是 ``hfq_factor``。取到就用它把价格换成后复权价；
        # 取不到就退回原始价，但**把这一根标记成未复权**，收益层据此跳过它。
        factor = float(r[13]) if len(r) > 13 and r[13] is not None else None
        if factor is not None and factor > 0:
            op[-1] *= factor
            hi[-1] *= factor
            lo[-1] *= factor
            cl[-1] *= factor
            if pc[-1] > 0:
                # pre_close 也乘同一个因子：涨停判定用的是「当日价 vs 昨收」，
                # 两边同乘不变，所以这里乘不乘都不影响标签，但让价格自洽。
                pc[-1] *= factor
            adj.append(True)
        else:
            adj.append(False)
        usable.append(not (tradable_only and bool(r[8])) and adj[-1])

    if not dates:
        return None

    returns: dict[tuple[int, int], float] = {}
    for h in HORIZONS:
        for i in range(len(cl) - h):
            # 信号日之后仍有 bar、该位置可买、且价格已复权，才算一个可执行样本。
            # 三者缺一不可：涨停买不进、没复权的价格会造出假跌。
            if not usable[i]:
                continue
            base = cl[i]
            if base <= 0:
                continue
            returns[(h, i)] = cl[i + h] / base - 1.0

    return BarPanel(
        code=code,
        dates_ms=tuple(dates),
        open=tuple(op),
        high=tuple(hi),
        low=tuple(lo),
        close=tuple(cl),
        pre_close=tuple(pc),
        volume=tuple(vol),
        is_limit_up=tuple(up),
        is_limit_down=tuple(dn),
        is_bomb=tuple(bomb),
        is_one_word=tuple(one_word),
        touched_limit=tuple(touched),
        returns=returns,
        adjusted=tuple(adj),
    )
