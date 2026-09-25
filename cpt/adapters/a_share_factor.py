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
    "FactorEnsureResult",
    "FactorRow",
    "FactorUnavailableError",
    "OnDemandFactorFetcher",
    "SOURCE_TX",
    "build_factor_rows",
    "factor_source_ref",
    "fetch_factor_rows",
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
#: 同一代码的重复拉取冷却（秒）：避免用户连点/轮询反复打腾讯。
DEFAULT_COOLDOWN_SECONDS: Final[float] = 600.0


class FactorUnavailableError(RuntimeError):
    """按需拉因子失败（腾讯无该标的后复权 / 网络失败 / 无重叠交易日）。"""


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


def build_factor_rows(
    code: str,
    raw_bars: Sequence[Any],
    hfq_bars: Sequence[Any],
) -> tuple[FactorRow, ...]:
    """由 raw / hfq 两个序列算出因子行（纯函数，便于测试）。

    只保留**两个序列都有**的交易日 —— 因子必须同源同对，缺一边就不能算。
    """
    raw = _close_map(raw_bars)
    hfq = _close_map(hfq_bars)
    rows: list[FactorRow] = []
    for trade_date in sorted(set(raw) & set(hfq)):
        raw_close = raw[trade_date]
        if raw_close <= 0:
            continue
        rows.append(
            FactorRow(
                code=code,
                trade_date=trade_date,
                hfq_factor=hfq[trade_date] / raw_close,
                source_ref=factor_source_ref(trade_date),
            )
        )
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
        raise FactorUnavailableError(f"unsupported:{exc}") from exc
    except ASharePublicError as exc:
        raise FactorUnavailableError(f"network:{exc}") from exc

    rows = build_factor_rows(bare, raw_bars, hfq_bars)
    if not rows:
        raise FactorUnavailableError(
            f"no_overlap:raw={len(raw_bars)} hfq={len(hfq_bars)}（两个序列无共同交易日）"
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
            reason, _, detail = str(exc).partition(":")
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
