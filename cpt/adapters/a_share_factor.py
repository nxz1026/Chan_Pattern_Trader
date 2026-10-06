"""A 股复权因子**按需**获取（R17-3）。

## 为什么需要这个

`asel.ref_adjust_factor` 覆盖面很小（只覆盖热门池并集，全库 5,225 只里绝大多数没有），
原因是**全市场 backfill 从未执行** —— 只跑过增量模式（热门池 ∪ 连板 ∪ 新股 ∪
已有覆盖）。全市场批量被否决（5,225 只太多），改为**用户输入哪个代码就按需补哪个**。

## 原理

腾讯 ``fqkline`` 一次请求回吐**同源同对**的 raw 与 hfq 两个序列，
``hfq_factor = hfq_close / raw_close`` 因此**绝对自洽**（绝对值与 Wind/sina 不同
仅因起算点不同，不影响"避免假跳空"的目标）。需要 2 次请求（``bfq`` + ``hfq``）。

## 关键设计：不预测，只询问

腾讯是否提供某标的的 ``hfqday`` 是**逐标的**属性，**无法用代码前缀预测**：
实测 688111/688036 有 hfq 而 688981 没有，多数 301 有 hfq 而近期新股没有。
所以这里**不做任何板块判断** —— 拉一次，然后如实报告腾讯实际返回了什么。
（`docs/progress-log.md` R15-1 节记录了这条上先后犯过的两次错。）
"""

from __future__ import annotations

import logging
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from cpt.adapters.a_share_public import (
    MAX_DAILY_BARS,
    AShareAdjustUnsupportedError,
    ASharePublicError,
    TencentKlineClient,
    normalize_code,
)

__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_FACTOR_DAYS",
    "FACTOR_UNAVAILABLE_CATEGORIES",
    "MAX_FACTOR_RATIO_JUMP",
    "CategorizedFactorUnavailableError",
    "FactorEnsureResult",
    "FactorRow",
    "FactorUnavailableError",
    "OnDemandFactorFetcher",
    "SOURCE_TX",
    "FACTOR_RECOMPUTE_TABLE",
    "build_factor_rows",
    "codes_with_factors",
    "ensure_recompute_stage",
    "factor_from_actions",
    "factor_source_ref",
    "factor_unavailable_category",
    "fetch_factor_rows",
    "load_recent_closes",
    "save_recompute_factors",
    "upsert_factor_rows",
]

_LOG = logging.getLogger("cpt.adapters.a_share_factor")

#: ``asel.ref_adjust_factor.source`` 的取值。
#:
#: **必须与 ``scripts/factor_backfill.py`` 一致。** 2026-09-25 之前两处各写一个值
#: （本模块 ``tencent_fqkline``、脚本 ``tx:fqkline``），于是同一个腾讯接口在库里
#: 分裂成两个 ``source``（实测 7,200 行 vs 49,730 行）—— 任何 ``WHERE source=...``
#: 的查询都会漏掉八成数据。现统一取脚本那个（占多数）。
#:
#: **存量行已于 2026-09-25 迁移完毕**：``UPDATE ... WHERE source='tencent_fqkline'``
#: 命中 7,200 行，库里现在只有 ``tx:fqkline`` 一个值（56,930 行 / 101 只）。
#: 那 7,200 行属于 9 只票（000002/002119/002724/600000/600004/600006/600036/
#: 600519/601398），与原 92 只**零重叠**，故 UPDATE 不可能撞 ``(code, trade_date)``
#: 主键。改这个值时必须同时处置存量行。
SOURCE_TX: Final[str] = "tx:fqkline"
#: 腾讯单次上限 801 根，取 800 留边界（与 scripts/factor_backfill.py 一致）。
DEFAULT_FACTOR_DAYS: Final[int] = 800
#: 因子判脏的**容差带**（不是"跳变阈值"，见 :func:`_looks_degenerate` 的说明）。
#:
#: 判据是方向式的：当日 hfq 的日波动明显**盖过** raw 才判腾讯退化；真除权是
#: 反过来的（raw 假跳空、hfq 连续），所以不会被误伤。
#:
#: 0.15 的来历：扫 34 只票 × 800 根，**真除权日**的 raw/hfq 日收益落差实测都在
#: 10% 以内（002594 2025-07-29 是 raw -67% / hfq ±0，落差 67% 但**方向相反**）；
#: 而**脏数据日**的落差是 hfq 单独崩、raw 正常（300750 hfq -47.87% / raw +1.10%）。
#: 取 0.15 = 15% 是要卡在"A 股单日涨跌停 10/20%"之上，留出缓冲。
#:
#: 第一版曾用「因子比值跳变 > 15%」当判据，**已被实测证伪**：002594 真送转的
#: 比值是 3.0355（跳 203%），比任何脏数据都狠 —— 幅度判据必然在漏脏与误伤除权
#: 之间二选一。换成方向判据后才同时通过两组用例。
MAX_FACTOR_RATIO_JUMP: Final[float] = 0.15
#: 同一代码的重复拉取冷却（秒）：避免用户连点/轮询反复打腾讯。
DEFAULT_COOLDOWN_SECONDS: Final[float] = 600.0


class FactorUnavailableError(RuntimeError):
    """按需拉因子失败（腾讯无该标的后复权 / 网络失败 / 无重叠交易日）。

    只需要「拉不到因子」这个事实时用它；要区分**为什么**拉不到，用
    :class:`CategorizedFactorUnavailableError`（带 ``category`` 属性）。
    """


class CategorizedFactorUnavailableError(FactorUnavailableError):
    """带**分类**的 :class:`FactorUnavailableError`。

    为什么不让调用方去 ``str(exc).partition(":")``：
    :meth:`OnDemandFactorFetcher.ensure` 原来就是这么分的，而 detail 本身含冒号
    （腾讯回显的键名、``raw=800 hfq=0`` …）⇒ **第一个冒号之后的内容全被丢掉**，
    截断后的结论还会被 ``_unsupported`` **永久缓存**。一个被截断且永不再问腾讯的
    诊断信息，比没有更难查。

    Args:
        category: 失败分类，取值 :data:`FACTOR_UNAVAILABLE_CATEGORIES` 之一。
        detail: 人类可读的细节（原样保留，不做任何截断）。

    ``str(exc)`` 仍是 ``"{category}:{detail}"``（分类在前），所以日志与既有断言
    的可读性不变 —— 变的只是**判定方式**：读属性，不是切字符串。
    """

    def __init__(self, category: str, detail: str = "") -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"{category}:{detail}" if detail else category)


#: :class:`CategorizedFactorUnavailableError.category` 的三个取值
#: （= :meth:`OnDemandFactorFetcher.ensure` 回给前端的 ``reason``）。
FACTOR_UNAVAILABLE_CATEGORIES: Final[tuple[str, ...]] = ("unsupported", "network", "no_overlap")


def factor_unavailable_category(exc: BaseException) -> tuple[str, str]:
    """从异常里取 ``(category, detail)``；**按属性**判定，不解析 message。

    没有 ``category`` 属性的（例如外部代码直接 ``FactorUnavailableError("...")``）
    退回到旧的 ``"类别:细节"`` 切分口径，并在这里说明它只是兼容路径 ——
    真正的抛点（:func:`fetch_factor_rows`）一律走结构化异常。
    """
    category = getattr(exc, "category", None)
    if isinstance(category, str) and category:
        return category, str(getattr(exc, "detail", "") or "")
    reason, _, detail = str(exc).partition(":")
    return reason, detail


def factor_source_ref(trade_date: str) -> str:
    """``source_ref`` 的**唯一**构造处。

    按需路径与 backfill 脚本都必须用它 —— 否则同一行数据会带两种溯源串，按
    ``source_ref`` 反查就查不全（这正是 ``source`` 字段刚踩过的坑）。
    """
    return f"web.ifzq.gtimg.cn fqkline day/hfq {trade_date}"


@dataclass(frozen=True)
class FactorRow:
    """一行因子（``asel.ref_adjust_factor`` 的一行）。

    ``source`` / ``source_ref`` 给默认值，是为了让只关心 code/date/factor 的调用方
    与测试能继续三参数构造；写库时两者都会落盘。
    """

    code: str
    trade_date: str  # ISO 'YYYY-MM-DD'
    hfq_factor: float
    source: str = SOURCE_TX
    source_ref: str | None = None


@dataclass(frozen=True)
class FactorEnsureResult:
    """一次按需拉取的结果（给上层写进快照供审计）。"""

    code: str
    fetched: bool
    rows: int
    reason: str | None = None  # 失败原因；成功为 None
    detail: str | None = None


def _date_of(open_ms: int) -> str:
    return datetime.fromtimestamp(open_ms / 1000, tz=UTC).date().isoformat()


def _close_map(bars: Sequence[Any]) -> dict[str, float]:
    return {_date_of(bar.open_time): float(bar.close) for bar in bars}


def _looks_degenerate(raw_ret: float, hfq_ret: float, tol: float) -> bool:
    """腾讯 hfq 序列该根退化了吗（判据：谁在假跳空）。

    ## 为什么不能比「因子比值跳变幅度」

    第一版判据是 ``factor[i] / factor[i-1]`` 偏离 1 超过阈值。实测**直接证伪**了它：
    002594 在 2025-07-29 真送转，因子比值 **3.0355（跳 203%）**；而脏数据里
    幅度最大的也才 0.32（跳 68%）。真除权比脏数据跳得更狠 —— 两者在幅度上
    **重叠**，任何单一阈值都必然在「漏脏」和「误伤除权」之间二选一。

    ## 正确判据：方向

    两种事件在**方向**上完全相反，这是它们唯一可靠的区分点：

    - **真除权**：raw（不复权成交价）出现**假跳空**，hfq（后复权）被调成连续。
      实测 002594 2025-07-29 就是 ``raw -67%`` 而 ``hfq ±0``。
    - **腾讯退化**：raw 正常，hfq 那一根**退化成 raw 的值**或乱成量级错误。
      实测 300750 ``raw +1.10%`` / ``hfq -47.87%``；000002 ``raw +3.92%`` /
      ``hfq -62.29%``（因子从 1.0 跳到 115.08）。

    所以判据是 **hfq 的日波动是否明显盖过 raw**：盖过就是腾讯坏了；反过来
    （raw 跳得更狠，或两者同步）都是有效数据 —— 同步暴跌当天两边一起跌，
    因子照旧正确。

    Args:
        raw_ret: 当日 raw 收盘日收益。
        hfq_ret: 当日 hfq 收盘日收益。
        tol: 容差带（默认 :data:`MAX_FACTOR_RATIO_JUMP`）。它不是"跳变阈值"，
            而是"两边差多少还算是同一件事"的容忍度。

    宁可漏判也不误伤：除权因子被丢掉会造成假跳空（比脏因子危害大得多，
    用户会把它当真实行情），而脏因子只影响一天且会留告警。
    """
    if abs(hfq_ret) <= tol:
        return False  # hfq 平稳 —— 典型就是真除权（raw 假跳空、hfq 连续）
    return abs(hfq_ret) > abs(raw_ret) + tol


def build_factor_rows(
    code: str,
    raw_bars: Sequence[Any],
    hfq_bars: Sequence[Any],
    *,
    max_ratio_jump: float = MAX_FACTOR_RATIO_JUMP,
) -> tuple[FactorRow, ...]:
    """由 raw / hfq 两个序列算出因子行（纯函数，便于测试）。

    只保留**两个序列都有**的交易日 —— 因子必须同源同对，缺一边就不能算。

    ## 脏数据防护

    腾讯的 hfq 序列会**整根退化**，而且形态不唯一（2026-09-30 实测扫 40 只票
    15 只中招）：

    - 退化 A：hfq 那根的 o/h/l/c/v 与 raw **逐字相同**（300750、688111）；
    - 退化 B：hfq 那根既不等于 raw，比值也乱成量级错误（000002 变成 115.08、
      600276 变成 29.46、300059 变成 32.18）。

    退化 A 用「两序列逐字段比对」能抓，但抓不到 B。统一交给
    :func:`_looks_degenerate`：只要当日 hfq 的波动明显盖过 raw 就判脏，丢该日
    并 :func:`_LOG.warning` 告警（含代码、日期、两侧日收益）。

    跳变只在**相邻交易日**之间判（按日期升序遍历重叠日期），不跨缺口回看：跳空
    停牌复牌时，raw 与 hfq 会同步跳变，落差在容差带内，不会被误判。
    """
    raw = _close_map(raw_bars)
    hfq = _close_map(hfq_bars)
    rows: list[FactorRow] = []
    prev_raw = 0.0
    prev_hfq = 0.0
    prev_date: str | None = None
    for trade_date in sorted(set(raw) & set(hfq)):
        raw_close = raw[trade_date]
        if raw_close <= 0:
            continue
        factor = hfq[trade_date] / raw_close
        if prev_date is not None and prev_raw > 0 and prev_hfq > 0:
            raw_ret = raw_close / prev_raw - 1.0
            hfq_ret = hfq[trade_date] / prev_hfq - 1.0
            if _looks_degenerate(raw_ret, hfq_ret, max_ratio_jump):
                _LOG.warning(
                    "因子判脏 %s %s：raw 日收益 %+.2f%% vs hfq 日收益 %+.2f%%"
                    "（hfq 波动盖过 raw，判腾讯该根退化；容差 %.0f%%）—— 跳过",
                    code,
                    trade_date,
                    raw_ret * 100,
                    hfq_ret * 100,
                    max_ratio_jump * 100,
                )
                # 不推进锚点：脏日的比值不可信，锚点必须留在最后一个可信日，
                # 否则下一个好日会拿脏值当基准、被连坐判脏。
                continue
        rows.append(
            FactorRow(
                code=code,
                trade_date=trade_date,
                hfq_factor=factor,
                source_ref=factor_source_ref(trade_date),
            )
        )
        prev_raw, prev_hfq, prev_date = raw_close, hfq[trade_date], trade_date
    return tuple(rows)


def fetch_factor_rows(
    code: str,
    *,
    days: int = DEFAULT_FACTOR_DAYS,
    timeout: float | None = None,
    opener: Callable[..., bytes] | None = None,
) -> tuple[FactorRow, ...]:
    """从腾讯拉 raw + hfq 并算出因子行。

    Args:
        code: 6 位裸码（``600519``）或带后缀（``600519.SH``）。
        days: 回看根数，1..:data:`MAX_DAILY_BARS`。
        timeout: 单次 HTTP 超时（秒）；``None`` 用客户端默认。
        opener: 注入式取值器，测试用。

    Raises:
        FactorUnavailableError: 腾讯无该标的后复权、网络失败、或两个序列无重叠日。
    """
    if not 1 <= days <= MAX_DAILY_BARS:
        raise ValueError(f"days 必须在 1..{MAX_DAILY_BARS}，实际 {days}")
    bare = normalize_code(code)[2:]  # 'sh600519' -> '600519'
    kwargs: dict[str, Any] = {"opener": opener} if opener is not None else {}
    if timeout is not None:
        kwargs["timeout"] = timeout

    raw_bars = TencentKlineClient(adjust="bfq", **kwargs).fetch_daily_bars(bare, limit=days)
    try:
        hfq_bars = TencentKlineClient(adjust="hfq", **kwargs).fetch_daily_bars(bare, limit=days)
    except AShareAdjustUnsupportedError as exc:
        # 逐标的属性，不是板块属性 —— 原样带上腾讯实际返回的键名，便于事后核对。
        raise CategorizedFactorUnavailableError("unsupported", str(exc)) from exc
    except ASharePublicError as exc:
        raise CategorizedFactorUnavailableError("network", str(exc)) from exc

    rows = build_factor_rows(bare, raw_bars, hfq_bars)
    if not rows:
        raise CategorizedFactorUnavailableError(
            "no_overlap", f"raw={len(raw_bars)} hfq={len(hfq_bars)}（两个序列无共同交易日）"
        )
    return rows


class OnDemandFactorFetcher:
    """带冷却的按需因子拉取器（**进程内**冷却，够用且零依赖）。

    冷却的意义：前端下拉/手输可能被连点，或者用户来回切代码。没有冷却就是每点一次
    打腾讯 2 次请求。

    Args:
        cooldown_seconds: 同一代码的重复拉取冷却；``0`` 表示不冷却。
        days: 每次拉多少根。
        clock: 注入式时钟（测试用）。
        fetch: 注入式取数函数（测试用）。
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        days: int = DEFAULT_FACTOR_DAYS,
        clock: Callable[[], float] = _time.monotonic,
        fetch: Callable[[str, int], tuple[FactorRow, ...]] | None = None,
    ) -> None:
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._days = days
        self._clock = clock
        self._fetch = fetch if fetch is not None else self._default_fetch
        self._last_attempt: dict[str, float] = {}
        # 「腾讯没有该标的 hfqday」是**永久**结论（同一进程内不会再变），单独记下来：
        #   · 语义正确 —— 用户每次都看到稳定的 `unsupported`，而不是一会儿
        #     `unsupported`、一会儿 `cooldown`（实测踩到，审计都因此不可重复）；
        #   · 省请求 —— 不再对注定失败的标的反复问腾讯。
        # 只对**可重试**的失败（网络/落库）才用冷却。
        self._unsupported: dict[str, str] = {}

    def _default_fetch(self, code: str, days: int) -> tuple[FactorRow, ...]:
        return fetch_factor_rows(code, days=days)

    def _cooling_down(self, code: str) -> bool:
        if self._cooldown <= 0:
            return False
        last = self._last_attempt.get(code)
        return last is not None and (self._clock() - last) < self._cooldown

    def ensure(self, code: str) -> FactorEnsureResult:
        """尝试为 ``code`` 补齐因子（**不抛异常**，失败折叠进结果）。"""
        bare = normalize_code(code)[2:]
        known = self._unsupported.get(bare)
        if known is not None:
            # 已知永久不支持：直接复用结论，不再问腾讯、也不受冷却影响。
            return FactorEnsureResult(
                code=bare, fetched=False, rows=0, reason="unsupported", detail=known
            )
        if self._cooling_down(bare):
            return FactorEnsureResult(
                code=bare, fetched=False, rows=0, reason="cooldown", detail=None
            )
        self._last_attempt[bare] = self._clock()
        try:
            rows = self._fetch(bare, self._days)
        except FactorUnavailableError as exc:
            # 按**属性**取分类，不再 ``str(exc).partition(":")`` 切字符串 ——
            # detail 里本来就有冒号，切第一个会把腾讯回显的键名 / 根数明细截断，
            # 而下面 ``self._unsupported[bare] = detail`` 把它**永久缓存**了。
            reason, detail = factor_unavailable_category(exc)
            _LOG.info("按需取因子失败 %s: %s", bare, exc)
            if reason == "unsupported":
                self._unsupported[bare] = detail
            return FactorEnsureResult(
                code=bare, fetched=False, rows=0, reason=reason or "unavailable", detail=detail
            )
        except Exception as exc:  # noqa: BLE001 — 任何意外都不能让看板 500
            _LOG.warning("按需取因子异常 %s: %s", bare, exc)
            return FactorEnsureResult(
                code=bare,
                fetched=False,
                rows=0,
                reason="error",
                detail=f"{type(exc).__name__}: {exc}",
            )
        if not rows:
            return FactorEnsureResult(code=bare, fetched=False, rows=0, reason="empty")
        return FactorEnsureResult(code=bare, fetched=True, rows=len(rows))


def upsert_factor_rows(
    conn: Any,
    rows: Sequence[FactorRow],
    *,
    source_url: str | None = None,
    dry_run: bool = False,
) -> int:
    """幂等写入 ``asel.ref_adjust_factor``（主键 ``(code, trade_date)``）。

    用 ``executemany``：单只票 800 行，逐行 ``execute`` 会慢到不可用（实测）。

    ``dry_run=True`` 只算行数、不碰 DB —— ``scripts/factor_backfill.py --dry-run``
    依赖这个能力。2026-09-25 之前脚本自带一份 9 列实现（多写 ``source_ref``、支持
    dry-run），而本函数只有 8 列且无 dry-run，两份实现已经漂移；现在这里是唯一实现。
    """
    if not rows:
        return 0
    now = datetime.now(UTC)
    payload = [
        (
            row.code,
            row.trade_date,
            row.hfq_factor,
            row.source,
            source_url,
            row.source_ref,
            now,  # as_of
            now,  # available_at
            now,  # fetched_at
        )
        for row in rows
    ]
    if dry_run:
        return len(payload)
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO asel.ref_adjust_factor
                 (code, trade_date, hfq_factor, source, source_url, source_ref,
                  as_of, available_at, fetched_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (code, trade_date) DO UPDATE SET
                 hfq_factor=EXCLUDED.hfq_factor,
                 source=EXCLUDED.source,
                 source_url=EXCLUDED.source_url,
                 source_ref=EXCLUDED.source_ref,
                 fetched_at=EXCLUDED.fetched_at""",
            payload,
        )
    conn.commit()
    return len(payload)


# --------------------------------------------------------------------------- #
# R37：按 Wind 公司行动重算后复权因子（**只写暂存表**，见 factor_recompute.py）
# --------------------------------------------------------------------------- #

#: 暂存表名。**生产表 ``asel.ref_adjust_factor`` 一个字节都不碰** ——
#: 切换是人工决定（看报告 → 决定），脚本绝不自动切。
FACTOR_RECOMPUTE_TABLE: Final[str] = "asel.ref_adjust_factor_v2"

_RECOMPUTE_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {FACTOR_RECOMPUTE_TABLE} (
    code        text        NOT NULL,
    trade_date  date        NOT NULL,
    hfq_factor  numeric     NOT NULL,
    basis       text        NOT NULL,
    source      text        NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (code, trade_date)
)
"""


def ensure_recompute_stage(conn: Any) -> None:
    """建暂存表（幂等）。"""
    with conn.cursor() as cur:
        cur.execute(_RECOMPUTE_DDL)
    conn.commit()


def load_recent_closes(conn: Any, code: str, *, limit: int = 800) -> tuple[tuple[int, float], ...]:
    """``(open_time_ms, close)`` **升序**，取最近 ``limit`` 根不复权 K 线。

    ⚠️ ``public.daily_bar`` **没有** ``open_time`` 列 —— 交易日的 ``open_time``
    是由 ``date`` 推出来的（UTC 当日零点毫秒），口径与
    :func:`cpt.adapters.a_share_local.fetch_validated_klines` 装配 ``CanonicalBar``
    时那一行 ``open_ms`` 完全一致。第一版这里直接 ``SELECT open_time``，
    在真机上当场炸 ``column "open_time" does not exist``。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT date, close FROM public.daily_bar WHERE code=%s ORDER BY date DESC LIMIT %s",
            (code, limit),
        )
        rows = cur.fetchall()
    out: list[tuple[int, float]] = []
    for day, close in rows:
        open_ms = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
        out.append((open_ms, float(close)))
    return tuple(sorted(out))


def codes_with_factors(conn: Any) -> tuple[str, ...]:
    """已有因子行的裸码（升序）—— 重算的候选全集。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT code FROM asel.ref_adjust_factor WHERE source IS NOT NULL")
        return tuple(sorted({str(r[0]).split(".")[0] for r in cur.fetchall()}))


def hot_pool_codes(conn: Any, *, limit: int = 60) -> tuple[str, ...]:
    """热门池（``public.hot_rank`` 最新一期）与策略源最新一期的裸码。

    重算要按额度分几天，所以**顺序有意义**：先跑正在被看的票。
    """
    out: list[str] = []
    with conn.cursor() as cur:
        cur.execute("SELECT max(date) FROM public.hot_rank")
        row = cur.fetchone()
        if row and row[0]:
            cur.execute("SELECT code FROM public.hot_rank WHERE date=%s", (row[0],))
            out.extend(str(r[0]).split(".")[0] for r in cur.fetchall())
        cur.execute("SELECT max(trade_date) FROM public.strategy_signal")
        row = cur.fetchone()
        if row and row[0]:
            cur.execute("SELECT code FROM public.strategy_signal WHERE trade_date=%s", (row[0],))
            out.extend(str(r[0]).split(".")[0] for r in cur.fetchall())
    seen: list[str] = []
    for c in out:
        if c and c not in seen:
            seen.append(c)
    return tuple(seen[:limit])


def save_recompute_factors(conn: Any, rows: Sequence[tuple[str, str, float, str, str]]) -> int:
    """写暂存表并 commit。``rows`` = ``(code, trade_date, factor, basis, source)``。"""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO {FACTOR_RECOMPUTE_TABLE}
                 (code, trade_date, hfq_factor, basis, source)
                 VALUES (%s,%s,%s,%s,%s)
                 ON CONFLICT (code, trade_date) DO UPDATE
                 SET hfq_factor=EXCLUDED.hfq_factor,
                     basis=EXCLUDED.basis, computed_at=now()""",
            rows,
        )
    conn.commit()
    return len(rows)


def factor_from_actions(
    actions: Sequence[Any],
    *,
    prev_closes: dict[str, float],
) -> dict[str, float]:
    """由公司行动按**定义**算后复权因子 → ``{除权日: 该日起生效的累计乘子}``。

    公式（从最新一次除权往前乘）::

        f = Π  (1 + 送转比例) / (1 − 每股派息 / 除权前收盘)

    ## 为什么不能继续用供应商的后复权序列相除

    现在的因子就是 ``腾讯后复权收盘 ÷ 库里不复权收盘`` 的**逐日比值**，而那个比值
    本身不是常数（R36 实测：600519 在 125 个交易日里相对极差 6.25%，000001 在
    20 天里 2.57%）。**拿一条带漂移的曲线当因子，等于把漂移固化下来。**
    公司行动是离散事件，事件才是真值。

    :param actions: :class:`~cpt.adapters.wind_source.CorporateAction` 序列。
    :param prev_closes: ``{除权日: 除权前收盘}``；取不到的那次会被跳过
        （**宁可少一个台阶，也不用猜的收盘价**）。
    :returns: 只含**除权日**上的因子（分段常数的分段点）。
    """
    out: dict[str, float] = {}
    acc = 1.0
    for act in sorted(actions, key=lambda a: a.ex_date, reverse=True):
        # 税前优先、税后回落：Wind 的列集合**按标的而变**（实测 600036 只给"税后"），
        # 少了任一种都不能让那一次除权凭空消失。
        cash = getattr(act, "cash_pre_tax", None)
        if cash is None:
            cash = getattr(act, "cash_after_tax", None)
        prev = prev_closes.get(act.ex_date)
        ratio = float(getattr(act, "share_ratio", 0.0) or 0.0)
        if cash is None or prev is None or prev <= 0:
            if ratio:
                acc *= 1.0 + ratio
                out[act.ex_date] = acc
            continue
        drop = float(cash) / prev
        if not 0 <= drop < 1:  # 派息不可能超过除权前收盘，否则是数据错
            _LOG.warning("跳过异常除权 %s：派息 %s / 前收 %s", act.ex_date, cash, prev)
            continue
        acc *= (1.0 + ratio) / (1.0 - drop)
        out[act.ex_date] = acc
    return out
