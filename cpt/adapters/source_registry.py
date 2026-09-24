"""数据源能力注册表与探活（R17）。

## 为什么要有这个东西

R15/R16 的踩坑都是同一类：**某个数据源悄悄不可用，链路静默降级**。
- A 股快照的 `overlays` 恒为空（replay 载荷嵌套）—— 没报错；
- R14 的 czsc 后端从未接进生产（两个 web 入口硬编码 native）—— 没报错；
- A 股 98.2% 的代码没有复权因子 —— 只在取数时才暴露。

所以 R17 的第一件事不是"再加一个源"，而是**把每个源的能力、依赖、额度代价
和当前健康状态变成可查询的一等对象**，并暴露成 ``/api/dashboard/sources``。

## 契约

- :data:`SOURCES` 是**声明**（静态、可单测、不需要网络）；
- :func:`probe_source` 是**实测**（可能发网络请求，有超时，失败不抛异常而是
  返回 ``status="unavailable"`` + ``detail``）；
- **Wind 默认不探测**：一次探测就消耗一次真实额度（``quota="wind"``），
  必须显式 ``include_quota=True`` 才会碰它 —— 默认路径绝不花配额。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any, Final

__all__ = [
    "PROBE_CACHE_TTL_SECONDS",
    "SOURCES",
    "ProbeResult",
    "SourceCapability",
    "capabilities_payload",
    "probe_all",
    "probe_source",
]

#: 探活结果缓存时间（秒）—— 避免每次刷新看板都打一遍所有上游。
PROBE_CACHE_TTL_SECONDS: Final[float] = 60.0


@dataclass(frozen=True)
class SourceCapability:
    """一个数据源的静态声明。

    Attributes:
        id: 稳定标识（出现在 API 与审计 JSON 里）。
        label: 中文名。
        markets: 覆盖的市场（``crypto`` / ``a_share``）。
        role: ``primary``（主通道）/ ``fallback``（兜底）/ ``local``（本地库）。
        provides: 提供什么（``kline`` / ``quote`` / ``adjust_factor`` / ``pool``）。
        requires: 需要的可选依赖（空元组表示零依赖）。
        quota: ``none`` 或 ``wind``（探测/取数会消耗万得额度）。
        note: 一句话说明（踩坑/限制）。
    """

    id: str
    label: str
    markets: tuple[str, ...]
    role: str
    provides: tuple[str, ...]
    requires: tuple[str, ...] = ()
    quota: str = "none"
    note: str = ""


#: 全部数据源声明。**顺序即优先级**（同市场内靠前者优先）。
SOURCES: Final[tuple[SourceCapability, ...]] = (
    SourceCapability(
        id="binance_futures",
        label="Binance USDT-M（自研 REST）",
        markets=("crypto",),
        role="primary",
        provides=("kline",),
        note="零第三方依赖，只打 /fapi/v1/klines；CPT 实时看板的默认通道",
    ),
    SourceCapability(
        id="ccxt",
        label="ccxt 统一交易所接口",
        markets=("crypto",),
        role="fallback",
        provides=("kline",),
        requires=("ccxt",),
        note="换所/换市场（现货↔永续）只改一个字符串；未装 ccxt 时登记为 unavailable",
    ),
    SourceCapability(
        id="wind",
        label="Wind 万得（CLI 主通道）",
        markets=("a_share",),
        role="primary",
        provides=("kline", "adjust_factor", "pool", "calendar"),
        requires=("wind_cli",),
        quota="wind",
        note="A 股主通道；**每次调用消耗真实额度**，默认不探测",
    ),
    SourceCapability(
        id="tencent_kline",
        label="腾讯 fqkline（后复权日线）",
        markets=("a_share",),
        role="fallback",
        provides=("kline",),
        note="免鉴权兜底；字段顺序是 [日期,开,收,高,低,量]，复权口径取 hfq 与本地库对齐",
    ),
    SourceCapability(
        id="sina_quote",
        label="新浪 hq.sinajs.cn（快照）",
        markets=("a_share",),
        role="fallback",
        provides=("quote",),
        note="只提供最新报价，不提供 K 线；需要 Referer 头",
    ),
    SourceCapability(
        id="a_share_local",
        label="本地 PostgreSQL（daily_bar × ref_adjust_factor）",
        markets=("a_share",),
        role="local",
        provides=("kline",),
        requires=("psycopg",),
        note=(
            "价格是**不复权**原始价（实测 == 腾讯 bfq：600519 2025-07-01/07-15/2026-01-05 "
            "三处 1405.100/1411.000/1426.000 全等，而同点 qfq 为 1353.119/1359.019/1397.976）；"
            "后复权由 raw × asel.hfq_factor 现算，但该表只有 94/5225 只 ⇒ 其余代码取不到因子"
        ),
    ),
)

#: 已知不可用/不稳定的源（留档，避免每轮重复侦察）。
KNOWN_DEAD_ENDPOINTS: Final[Mapping[str, str]] = {
    "push2.eastmoney.com": "本机（大阪）实测 502，东财接口对海外 IP 不稳定",
    "pytdx.get_security_bars": "4 台服务器全部返回 0 行（实测）",
    "yahoo_finance": "曾 429 限流，仅偶尔可用，不作为兜底依赖",
}


@dataclass(frozen=True)
class ProbeResult:
    """一次探活的结果。"""

    source_id: str
    status: str  # ok | degraded | unavailable | skipped
    latency_ms: float
    detail: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)


def _source_by_id(source_id: str) -> SourceCapability:
    for source in SOURCES:
        if source.id == source_id:
            return source
    raise KeyError(f"未知数据源：{source_id!r}")


def _timed(call: Callable[[], Mapping[str, Any]]) -> tuple[str, float, str, dict[str, Any]]:
    """跑一个探针，把"成功/失败"折叠成 ProbeResult 的四个字段（异常不外抛）。"""
    started = time.monotonic()
    try:
        evidence = dict(call())
    except Exception as exc:  # noqa: BLE001 — 探活的全部意义就是把异常变成状态
        latency = (time.monotonic() - started) * 1000
        return "unavailable", latency, f"{type(exc).__name__}: {exc}", {}
    latency = (time.monotonic() - started) * 1000
    status = str(evidence.pop("status", "ok"))
    detail = str(evidence.pop("detail", ""))
    return status, latency, detail, evidence


def _probe_binance_futures() -> Mapping[str, Any]:
    from cpt.adapters.binance_futures import BinanceFuturesClient  # noqa: PLC0415

    client = BinanceFuturesClient(timeout=10.0)
    bars = client.fetch_klines("BTCUSDT", "1h", limit=2)
    return {"status": "ok", "bars": len(bars), "last_open_time": bars[-1].open_time}


def _probe_ccxt() -> Mapping[str, Any]:
    from cpt.adapters.ccxt_source import CcxtKlineClient, CcxtNotInstalledError  # noqa: PLC0415

    try:
        client = CcxtKlineClient()
    except CcxtNotInstalledError:
        raise
    evidence = client.probe()
    return {"status": "ok", **evidence}


def _probe_wind(*, enabled: bool) -> Mapping[str, Any]:
    from cpt.adapters.wind_source import WindSourceClient, WindUnavailableError  # noqa: PLC0415

    if not enabled:
        return {
            "status": "skipped",
            "detail": "quota_not_authorized（Wind 探测消耗真实额度，需显式开启）",
        }
    try:
        client = WindSourceClient()
    except WindUnavailableError as exc:
        return {"status": "unavailable", "detail": str(exc)}
    return client.probe()


def _probe_tencent_kline() -> Mapping[str, Any]:
    from cpt.adapters.a_share_public import TencentKlineClient  # noqa: PLC0415

    bars = TencentKlineClient().fetch_daily_bars("600519", limit=3)
    return {
        "status": "ok",
        "bars": len(bars),
        "last_open_time": bars[-1].open_time,
        "last_close": bars[-1].close,
        "adjust": "hfq",
    }


def _probe_sina_quote() -> Mapping[str, Any]:
    from cpt.adapters.a_share_public import SinaQuoteClient  # noqa: PLC0415

    quote = SinaQuoteClient().fetch_quote("600519")
    return {"status": "ok", "last": quote["last"], "date": quote["date"], "time": quote["time"]}


def _probe_a_share_local() -> Mapping[str, Any]:
    from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415

    # AShareLocalClient 构造时不连库（lazy），这里显式取一次连接做最轻的探活。
    client = AShareLocalClient()
    with client._get_conn().cursor() as cur:  # noqa: SLF001 — 探活就是要碰真连接
        cur.execute("SELECT count(*) FROM public.daily_bar")
        total = int(cur.fetchone()[0])
        cur.execute("SELECT count(*) FROM asel.ref_adjust_factor")
        factors = int(cur.fetchone()[0])
        # 覆盖率 + **缺整天检测**：只报总行数会让"少了一整个交易日"完全隐形。
        # 实测 public.daily_bar 缺 2026-09-22（周二）全天 —— 腾讯与 Wind 两个
        # 独立通道都确认当天有成交（成交量 24573 手 / 2457294 股，吻合到 0.006%）。
        cur.execute(
            """
            SELECT date, count(*) FROM public.daily_bar
            WHERE date >= current_date - interval '45 days'
            GROUP BY date ORDER BY date
            """
        )
        per_day = [(row[0], int(row[1])) for row in cur.fetchall()]
    gaps: list[str] = []
    if per_day:
        counts = sorted(count for _, count in per_day)
        median = counts[len(counts) // 2]
        have = {day for day, _ in per_day}
        first, last = per_day[0][0], per_day[-1][0]
        cursor_day = first
        while cursor_day <= last:
            # 工作日缺整天 = 可疑；节假日不在此列（本地没有交易日历，所以只报
            # "工作日无数据"，由人判断是否为节假日）。
            if cursor_day.weekday() < 5 and cursor_day not in have:
                gaps.append(cursor_day.isoformat())
            cursor_day += timedelta(days=1)
        low_coverage = [
            {"date": day.isoformat(), "bars": count}
            for day, count in per_day
            if count < median * 0.9
        ]
    else:
        median = 0
        low_coverage = []
    return {
        "status": "degraded" if gaps else "ok",
        "daily_bar_rows": total,
        "adjust_factor_rows": factors,
        "latest_date": per_day[-1][0].isoformat() if per_day else None,
        "median_bars_per_day": median,
        "missing_weekdays": gaps,
        "low_coverage_days": low_coverage,
        "detail": f"缺 {len(gaps)} 个工作日整天：{', '.join(gaps)}" if gaps else "",
    }


_PROBES: Final[Mapping[str, Callable[[bool], Mapping[str, Any]]]] = {
    "binance_futures": lambda _q: _probe_binance_futures(),
    "ccxt": lambda _q: _probe_ccxt(),
    "wind": lambda q: _probe_wind(enabled=q),
    "tencent_kline": lambda _q: _probe_tencent_kline(),
    "sina_quote": lambda _q: _probe_sina_quote(),
    "a_share_local": lambda _q: _probe_a_share_local(),
}

_CACHE: dict[str, tuple[float, ProbeResult]] = {}


def probe_source(
    source_id: str,
    *,
    include_quota: bool = False,
    use_cache: bool = True,
    cache_ttl: float = PROBE_CACHE_TTL_SECONDS,
) -> ProbeResult:
    """探活一个源；**任何失败都折叠成状态**，不抛异常。

    Args:
        source_id: :data:`SOURCES` 里的 id。
        include_quota: 是否允许消耗配额的探测（目前只有 Wind 受影响）。
        use_cache: 命中缓存则直接返回（默认开）。
        cache_ttl: 缓存有效期（秒）。

    Raises:
        KeyError: 未知 source_id（这是编程错误，不该被折叠成状态）。
    """
    capability = _source_by_id(source_id)
    now = time.monotonic()
    cache_key = f"{source_id}:{include_quota}"
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached is not None and now - cached[0] < cache_ttl:
            return cached[1]

    probe = _PROBES[source_id]
    status, latency, detail, evidence = _timed(lambda: probe(include_quota))
    if (
        capability.quota != "none" and not include_quota and status == "ok"
    ):  # pragma: no cover - 防御性
        status = "skipped"
        detail = "quota_not_authorized"
    result = ProbeResult(
        source_id=source_id,
        status=status,
        latency_ms=round(latency, 1),
        detail=detail,
        evidence=evidence,
    )
    if use_cache:
        _CACHE[cache_key] = (now, result)
    return result


def probe_all(
    *,
    include_quota: bool = False,
    use_cache: bool = True,
    markets: tuple[str, ...] | None = None,
) -> list[ProbeResult]:
    """探活全部（或指定市场的）数据源，顺序与 :data:`SOURCES` 一致。"""
    return [
        probe_source(source.id, include_quota=include_quota, use_cache=use_cache)
        for source in SOURCES
        if markets is None or set(source.markets) & set(markets)
    ]


def capabilities_payload(
    *,
    include_quota: bool = False,
    use_cache: bool = True,
    markets: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """``/api/dashboard/sources`` 的响应体。"""
    results = {
        result.source_id: result
        for result in probe_all(include_quota=include_quota, use_cache=use_cache, markets=markets)
    }
    sources = []
    for source in SOURCES:
        if markets is not None and not (set(source.markets) & set(markets)):
            continue
        result = results.get(source.id)
        entry = asdict(source)
        entry["markets"] = list(source.markets)
        entry["provides"] = list(source.provides)
        entry["requires"] = list(source.requires)
        entry["probe"] = asdict(result) if result is not None else None
        sources.append(entry)
    return {
        "schema_version": "sources.v1",
        "include_quota": include_quota,
        "known_dead_endpoints": dict(KNOWN_DEAD_ENDPOINTS),
        "sources": sources,
    }


def clear_cache() -> None:
    """清空探活缓存（测试与运维用）。"""
    _CACHE.clear()
