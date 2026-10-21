"""盘中封板判定（ADR 0003 D5 的 CPT 侧实现）。**纯领域逻辑：零 IO、零 SQL。**

## 为什么要这个模块（而不是让 emotion-core 自己做）

ADR 0003 定的分工是「**原始采集与口径归 CPT，触发判据归 emotion-core**」。
但「口径」里有一块是判据本身要用到的：**这只票今天的涨停价是多少**。

这件事在 CPT 里是**有意不做**的 —— ``cpt/domain/a_share_rules.py`` 模块头
写明：「±10% 阈值不可靠（ST/科创/创业/北交规则不同），**直接读
``public.derived_bar.is_limit_up``**」。那条决定对**收盘后**是对的：不重算就不
出错。但它对**盘中**不成立 —— ``derived_bar`` 是收盘才写的，盘中根本读不到。

所以盘中这一层必须自己算，而算的规则不该散在调用点里。这就是本模块存在的
唯一理由：**把「盘中怎么算涨停」写一次，写成可测的纯函数**。

## 与 emotion-core `algorithms/intraday.py` 的关系

那边已经有一份 ``board_pct_limit`` / ``limit_up_price`` / ``is_at_limit``。
**两份是有意的**，不是疏忽：

- emotion-core 那份是**判据**（这条信号该不该触发），它的消费者是产品面；
- 这份是**口径**（这个价格是多少），它的消费者是 CPT 的采集与分析。

两份的差异由 ``tests/test_intraday_domain.py`` 里的对账用例逐条钉住 ——
不是靠人记得去同步。真要让它们合并，得等 ADR 0003 的分工被推翻。

## 四条口径

1. **`>=` 不是 `>`** —— 涨停价四舍五入到分，成交价可以正好等于它。
2. **`Decimal` 不是 `float`** —— ``10.0 * 1.1`` 在浮点里是 ``11.000000000000002``，
   交易所给的是 ``11.00``。差 0.002 就会把真封板漏掉。
3. **`None` 不是 `0`** —— 停牌时新浪把价量一起填 0，采集层已规整成 ``None``。
   把「没有这一档」读成「买一 0 手」，方向完全相反。
4. **板块按代码前缀判，不是 ±10% 一刀切** —— 创业板/科创板 20%、北交所 30%、
   主板 ST 5%。用 9.7 筛选会得出「20% 板的票从来没封过板」。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

__all__ = [
    "BOARD_PCT_BSE",
    "BOARD_PCT_GROWTH",
    "BOARD_PCT_MAIN",
    "BOARD_PCT_ST",
    "LIMIT_BOARD_PREFIXES",
    "board_of",
    "board_pct_limit",
    "is_at_limit_up",
    "is_main_board",
    "is_st_name",
    "limit_up_price",
    "seal_strength",
]

#: 各板块涨跌幅（百分比）。数字即口径，改这里要同步改 ADR 0003 与本文档。
BOARD_PCT_MAIN: Final[float] = 10.0
BOARD_PCT_GROWTH: Final[float] = 20.0
BOARD_PCT_BSE: Final[float] = 30.0
BOARD_PCT_ST: Final[float] = 5.0

#: 沪深主板前缀。与 ``cpt/application/a_share_snapshot.py`` 的板块归类同组
#: （沪市 600/601/603/605，深市 000/001/002/003 —— 002 中小板已并入主板）。
LIMIT_BOARD_PREFIXES: Final[tuple[str, ...]] = (
    "600",
    "601",
    "603",
    "605",
    "000",
    "001",
    "002",
    "003",
)

#: 创业板（20%）。
_GROWTH_PREFIXES: Final[tuple[str, ...]] = ("300", "301", "688", "689")

#: 北交所（30%）。
#:
#: **920 是新增的号段**（2026 年北交所启用的 6 位统一代码），必须先判 ——
#: 它既不以 8/4 开头，也不在沪深主板前缀里，漏掉会掉进兜底的 ``main`` 按 10% 算。
#: 实测代价：``920296 N文峰`` 昨收 11.45，按 10% 算涨停价 12.60，按 30% 是 14.89 ——
#: 差 2.29 元，足以让「还差 2 块才封板」被误判成「已经封板」。
#:
#: 8 开头是普通股，4 开头是老三板；两者都是 30%。
#: ⚠️ 北交所的**新三板创新层**也在 8 开头，但那些不在 A 股涨幅榜里 —— 用榜单
#: 来源（``hs_a``）已经保证只看到 A 股，所以这里不需要再区分。
_BSE_PREFIXES: Final[tuple[str, ...]] = ("920", "8", "4")

#: 一手多少股。
SHARES_PER_LOT: Final[int] = 100


def is_st_name(name: str | None) -> bool:
    """名字里带 ST 就是 ST。**只能靠名字** —— 代码里没有任何 ST 信息。

    与 emotion-core 侧同名函数逐字等价（对账用例钉住）。
    """
    if not name:
        return False
    return "ST" in name.upper()


def is_main_board(code: str) -> bool:
    """是不是沪深主板。C3 的统计切片只做主板首板。"""
    return code.strip().startswith(LIMIT_BOARD_PREFIXES)


def board_of(code: str, *, name: str | None = None) -> str:
    """板块名（可读）：``main`` / ``growth`` / ``bse`` / ``st_main`` / ``st_growth``。

    与 ``a_share_snapshot.py`` 的 ``_board_label`` 不同：那边是给**人**看的中文
    标签（沪市主板/科创板），这里是给**判据**用的枚举。两者消费者不同，
    不合并（合并会让任一方改文案时动到另一方的行为）。
    """
    code = code.strip()
    st = is_st_name(name)
    if code.startswith(_GROWTH_PREFIXES):
        return "st_growth" if st else "growth"
    if code.startswith(_BSE_PREFIXES):
        return "bse"
    if st:
        return "st_main"
    return "main"


def board_pct_limit(code: str, *, name: str | None = None) -> float:
    """这只票的涨跌停幅度（百分比）。

    Args:
        code: 6 位纯代码（不带 ``sh``/``sz`` 前缀）。
        name: 可选，但**判断主板 ST 必须要它**。不传按「非 ST」算 —— 这是
            **有意的乐观**：漏判 ST 得出偏高的涨停价（判不出封板、少报），
            而不是偏低的（假封板、错报）。宁可少报不可错报。
    """
    board = board_of(code, name=name)
    if board == "st_main":
        return BOARD_PCT_ST
    if board in ("growth", "st_growth"):
        return BOARD_PCT_GROWTH
    if board == "bse":
        return BOARD_PCT_BSE
    return BOARD_PCT_MAIN


def limit_up_price(code: str, pre_close: float, *, name: str | None = None) -> float | None:
    """涨停价，四舍五入到分。

    Returns:
        涨停价；``pre_close <= 0`` / 缺失时返回 ``None``（停牌或无昨收，
        **不拍一个数**）。
    """
    if pre_close is None or pre_close <= 0:
        return None
    pct = Decimal(str(board_pct_limit(code, name=name))) / Decimal(100)
    raw = Decimal(str(pre_close)) * (Decimal(1) + pct)
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def is_at_limit_up(
    code: str,
    *,
    last: float | None,
    pre_close: float | None,
    name: str | None = None,
) -> bool | None:
    """此刻是不是涨停。

    Returns:
        ``True`` 在涨停价 / ``False`` 没到 / ``None`` **判不了**（缺价）。
        ``None`` 与 ``False`` 必须分开 —— 停牌是 ``None``，没涨停是 ``False``。
        混起来会让「今天没有一只封板」与「今天没采到数据」变成同一件事。
    """
    if last is None or pre_close is None:
        return None
    limit = limit_up_price(code, pre_close, name=name)
    if limit is None:
        return None
    # >= 而不是 >：实盘成交价可以正好等于四舍五入后的涨停价。
    return last + 1e-9 >= limit


def seal_strength(
    code: str,
    *,
    last: float | None,
    pre_close: float | None,
    bid1_vol: float | None,
    name: str | None = None,
) -> dict[str, object]:
    """把「封得牢不牢」拆成三个可读的量，**不只是一个布尔**。

    为什么不是 ``is_sealed() -> bool``：盘中要看的是**程度**（封单 3 万手 vs
    30 万手是两种局面），一个布尔把这件事抹掉了，下游只能各自再算一遍。

    Returns:
        ``{"at_limit": bool|None, "limit_price": float|None, "bid1_lots": float|None,
        "distance_pct": float|None}``

        - ``at_limit``：``None`` 表示判不了（缺价），不是「没封」。
        - ``limit_price``：涨停价；``None`` 表示算不出。
        - ``bid1_lots``：买一挂单**手数**（E3 契约里是股，这里换手方便人读）；
          ``None`` 表示没有这一档。
        - ``distance_pct``：现价距涨停价还差多少（百分比，``0`` 就是封了）；
          ``None`` 表示算不出。
    """
    at = is_at_limit_up(code, last=last, pre_close=pre_close, name=name)
    limit = limit_up_price(code, pre_close if pre_close and pre_close > 0 else 0.0, name=name)
    lots = None if bid1_vol is None else bid1_vol / SHARES_PER_LOT
    distance = None
    if last is not None and limit is not None and limit > 0:
        distance = (limit - last) / limit * 100.0
    return {
        "at_limit": at,
        "limit_price": limit,
        "bid1_lots": lots,
        "distance_pct": distance,
    }
