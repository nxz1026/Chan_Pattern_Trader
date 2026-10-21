"""盘中快照采集（ADR 0003 D4 的 CPT 侧实现）。**尚未接进任何生产路径。**

## 状态：故意不接线

用户 2026-10-21 指示：分钟/盘口数据与盘中分析的**代码先写起来，但不接进项目**。
所以本模块：没有 systemd 单元、没有 cron、没有路由、没有表。

它是可运行的 —— ``scripts/intraday_probe.py`` 能让它跑一会儿并把结果打到终端
（或写一份 JSON 到 ``/tmp``）。但**没有任何东西会自己调用它**。
要接线，得先满足 ADR 0003 的 D10（CPT 稳定 + 口径拍板），那是另一件事。

## 采什么、为什么

ADR 0003 的主循环每 30 秒做一次：

1. 拉涨幅榜（1 请求，100 只）→ 筛出接近涨停的候选；
2. 对候选批量取五档（1 请求，≤50 只）→ 得到封单量。

两件事本模块都不新写 —— 五档走 ``SinaQuoteClient.fetch_quotes``（R82 已有），
本模块只补**涨幅榜**那一半（CPT 原先没有，因为日线路径不需要）。

## 三条口径（都是被实测教出来的）

1. **涨幅榜按 ``changepercent`` 排序，但筛选用板块阈值，不用 9.7**。
   实测第一行就是 ``bj920296 N文峰`` 涨幅 **217%**（北交所新股）——
   按 9.7 一刀切会把 20%/30% 板的票在「还差一点点」时全都漏掉，
   而把新股/退市整理的全捞进来。
2. **榜单里的 ``settlement`` 是昨收**（不是 ``pre_close`` 字段名）。新浪把这个
   字段叫 settlement（结算价），实际就是前收盘 —— 用它才能算涨停价。
3. **``code`` 字段才是 6 位纯代码**，``symbol`` 是带前缀的（``sh600519``）。
   E3 契约要求 6 位，所以取 ``code``，不要取 ``symbol``。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Final

from cpt.domain.intraday_rules import (
    is_main_board,
    is_st_name,
    limit_up_price,
    seal_strength,
)

__all__ = [
    "INTRADAY_RANK_URL",
    "IntradayCollector",
    "IntradayError",
    "RANK_PAGE_SIZE",
    "fetch_limit_rank",
    "pick_candidates",
]

#: 涨幅榜接口。``node=hs_a`` = 沪深 A 股（不含北交所）；要含北交所得换 ``hs_a`` 之外的 node。
#: ⚠️ 实测 ``hs_a`` 里仍然出现了 ``bj920296``（2026-10-21）—— 接口并没有严格按 node 过滤。
#: 所以**不要依赖 node 做市场过滤**，要按代码前缀自己筛（见 ``pick_candidates``）。
INTRADAY_RANK_URL: Final[str] = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)

#: 一页多少只。100 是接口的实际上限，再多会被截断（实测）。
RANK_PAGE_SIZE: Final[int] = 100

#: 候选筛选余量（百分点）。距涨停这么多以内就算「接近」，交给五档细看。
#: 为什么不是 0：封板前一瞬间的报价常常还在涨停价下方 0.1~0.3 元，
#: 卡死 0 会漏掉刚好在往板上打的那些。
_CANDIDATE_MARGIN_PCT: Final[float] = 0.5

_HEADERS: Final[dict[str, str]] = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0",
}

_RANK_TIMEOUT: Final[float] = 10.0


class IntradayError(RuntimeError):
    """盘中采集失败。**不返回部分结果** —— 见 E3 契约第 4 条。"""


def _get_rank_page(
    *, num: int = RANK_PAGE_SIZE, timeout: float = _RANK_TIMEOUT
) -> list[dict[str, Any]]:
    """拉一页涨幅榜（降序）。返回原始 dict 列表。"""
    url = f"{INTRADAY_RANK_URL}?page=1&num={num}&sort=changepercent&asc=0&node=hs_a"
    try:
        req = urllib.request.Request(url, headers=_HEADERS)  # noqa: S310 - 固定 https 域名
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = resp.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        raise IntradayError(f"涨幅榜取不到：{exc}") from exc
    body = body.strip()
    if not body or body in ("null", "[]"):
        # 空榜单有两种可能：非交易时段 / 接口变了。都不许当「今天没有涨停」。
        raise IntradayError("涨幅榜返回空（非交易时段，或接口变了）")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise IntradayError(f"涨幅榜不是 JSON：{body[:120]!r}") from exc
    if not isinstance(payload, list):
        raise IntradayError(f"涨幅榜顶层不是数组：{type(payload).__name__}")
    return [row for row in payload if isinstance(row, dict)]


def fetch_limit_rank(
    *, num: int = RANK_PAGE_SIZE, timeout: float = _RANK_TIMEOUT
) -> tuple[dict[str, Any], ...]:
    """涨幅榜（已规整成可判据的形状）。

    Returns:
        每行：``{"code","name","last","prev_close","chg_pct","symbol"}``。
        ``code`` 是 6 位纯代码（E3 契约）。拿不到完整字段的行**整行丢弃**并继续 ——
        榜单里偶尔有停牌行（``trade=0``），它们没有价格，留着只会造出假候选。

    Raises:
        IntradayError: 接口不可达 / 返回不是数组 / 整页都不可用。
    """
    rows = _get_rank_page(num=num, timeout=timeout)
    out: list[dict[str, Any]] = []
    for row in rows:
        code = str(row.get("code") or "").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        try:
            last = float(row.get("trade") or 0)
            prev_close = float(row.get("settlement") or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0 or prev_close <= 0:
            # 停牌行：trade=0。不是错误，只是没有可判的数据。
            continue
        out.append(
            {
                "code": code,
                "symbol": str(row.get("symbol") or ""),
                "name": str(row.get("name") or ""),
                "last": last,
                "prev_close": prev_close,
                "chg_pct": (last / prev_close - 1.0) * 100.0,
            }
        )
    if not out:
        raise IntradayError(f"涨幅榜 {len(rows)} 行里没有一行可用")
    return tuple(out)


def pick_candidates(
    rank: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    limit: int = 40,
    main_board_only: bool = False,
) -> list[dict[str, Any]]:
    """从涨幅榜里挑出「接近涨停」候选。

    Args:
        rank: :func:`fetch_limit_rank` 的输出。
        limit: 最多返回多少（对应单轮五档请求数上限）。
        main_board_only: 只留主板。C3 的统计切片只做主板首板；**分析用 True，
            采集用 False**（数据多采一点不亏，判据再筛）。

    Returns:
        按「距涨停由近到远」排序的候选，每行额外带 ``limit_price`` /
        ``distance_pct`` / ``board``。

    口径：
        - **不用 9.7 筛**：按每只票自己的板块阈值算涨停价，距涨停在
          ``[0, _CANDIDATE_MARGIN_PCT]`` 才**是**候选。这样 20%/30% 板不会漏。
        - **负距离不是候选**：``distance < 0`` 说明现价高于涨停价，正常交易
          里不可能 —— 实测 ``920296 N文峰`` 涨 217%（北交所新股，首日无涨跌幅
          限制），按 30% 算涨停价 14.89 而现价 36.30，距离 −144%。
          第一版只写了 ``distance > margin`` 就 ``continue``，于是 **−144% 落在
          阈值内被当成「最接近涨停」排在第一** —— 用涨幅排序会犯的错，
          换成距离排序照样能以另一种形式犯。
        - **ST 也留**：ST 是 5% 板，封板形态和主板 10% 不同，但「不采」才是
          信息丢失。判据侧按 ``board`` 字段自己决定要不要。
        - 排序用**距离**不用涨幅：涨幅榜降序里，217% 的新股排第一，
          而它对「封板」这件事毫无意义。
    """
    picked: list[dict[str, Any]] = []
    for row in rank:
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")
        if main_board_only and not is_main_board(code):
            continue
        limit_price = limit_up_price(code, row.get("prev_close") or 0.0, name=name)
        if limit_price is None:
            continue
        last = row.get("last")
        if last is None:
            continue
        distance = (limit_price - float(last)) / limit_price * 100.0
        # 只收「低于涨停价、且差得不多」的。``distance < 0`` 意味着现价**高于**
        # 涨停价 —— 正常交易里不可能，只可能是新股首日（无涨跌幅限制，如实测的
        # 920296 涨 217%）或数据异常。这两种都不是「接近封板」的候选。
        if distance < 0 or distance > _CANDIDATE_MARGIN_PCT:
            continue
        picked.append(
            {
                **row,
                "limit_price": limit_price,
                "distance_pct": distance,
                "board": (
                    "st" if is_st_name(name) else ("main" if is_main_board(code) else "other")
                ),
            }
        )
    # 由近到远。同距离时按代码排，保证可复现。
    picked.sort(key=lambda r: (abs(r["distance_pct"]), r["code"]))
    return picked[:limit]


class IntradayCollector:
    """一轮采集 = 涨幅榜 → 候选 → 五档 → 判定。**不落库**。

    落库（``intraday_quote`` / ``trigger_log`` 表）按 ADR D10 不属于本轮：
    建表容易，**删表难**，而且表结构该由真正消费它的那一侧（emotion-core）
    拍板。本类只负责「采到、算对、说清楚」。
    """

    def __init__(self, *, quote_client: Any = None, rank_timeout: float = _RANK_TIMEOUT) -> None:
        self._rank_timeout = rank_timeout
        if quote_client is None:
            # 惰性 import：本模块在 domain 判据上应当能离线导入，
            # 只有真正要采的时候才拉 adapter。
            from cpt.adapters.a_share_public import SinaQuoteClient

            quote_client = SinaQuoteClient(timeout=10.0)
        self._quotes = quote_client

    def scan(self, *, main_board_only: bool = False, limit: int = 40) -> dict[str, Any]:
        """跑一轮。返回 ``{"rank_size", "candidates", "quotes", "sealed", "errors"}``。

        任何一步失败都**抛异常**，不返回半个结果（E3 契约第 4 条）：
        静默返回一半会让「今天没触发」与「今天没采到」变成同一件事。
        """
        rank = fetch_limit_rank(timeout=self._rank_timeout)
        candidates = pick_candidates(rank, limit=limit, main_board_only=main_board_only)
        if not candidates:
            return {
                "rank_size": len(rank),
                "candidates": [],
                "quotes": {},
                "sealed": [],
                "errors": [],
            }

        codes = [c["code"] for c in candidates]
        quotes = self._quotes.fetch_quotes(codes)

        sealed: list[dict[str, Any]] = []
        for cand in candidates:
            quote = quotes.get(cand["code"])
            if quote is None:
                continue
            strength = seal_strength(
                cand["code"],
                last=quote.get("last"),
                pre_close=quote.get("prev_close"),
                bid1_vol=quote.get("bid1_vol"),
                name=quote.get("name") or cand.get("name"),
            )
            if strength["at_limit"] is True:
                sealed.append(
                    {
                        "code": cand["code"],
                        "name": cand.get("name"),
                        "distance_pct": strength["distance_pct"],
                        "bid1_lots": strength["bid1_lots"],
                        "limit_price": strength["limit_price"],
                    }
                )
        sealed.sort(key=lambda r: -(r["bid1_lots"] or 0))
        return {
            "rank_size": len(rank),
            "candidates": candidates,
            "quotes": quotes,
            "sealed": sealed,
            "errors": [],
        }
