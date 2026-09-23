"""Local Dashboard HTTP service entry point.

Supports three modes:

* ``demo`` — empty schema-complete snapshot, no network or engine access.
* ``fixture`` — runs a bundled CPT replay fixture through the native backend
  and serves the resulting snapshot.  Useful for local UI smoke without any
  upstream connectivity.
* ``realtime`` — wires ``BinanceFuturesClient`` + the native backend, polls
  the upstream REST endpoints, and serves a fresh snapshot every interval.
  Defaults to a thread-unsafe single client; the ``provider`` is rebuilt per
  HTTP request from independent state, see ``cpt.web.app``.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import threading
import time as _time
from collections.abc import Mapping, Sequence
from typing import Any

from cpt.adapters.binance_futures import (
    MAX_KLINES_LIMIT,
    BinanceFuturesClient,
    resolve_interval_ms,
)
from cpt.adapters.native_chanlun import NativeChanlunBackend
from cpt.adapters.reference_chanlun import (
    ReferenceChanlunConfig,
    map_bi,
    map_fractal,
    map_zhongshu,
)
from cpt.application.dashboard_alerts import alert_transition
from cpt.application.dashboard_config_compare import compare_configs
from cpt.application.dashboard_inspector import inspect_bar
from cpt.application.dashboard_market import normalize_24h
from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.application.multi_level import build_multi_level, structures_for_level
from cpt.application.replay import replay_bars
from cpt.domain.config import RulesConfig
from cpt.domain.models import Bi, CanonicalBar, Fractal, TrendType, ZhongShu
from cpt.domain.trend_type import classify_trend
from cpt.web.app import serve_snapshot

_LOG = logging.getLogger("cpt.web")


def demo_snapshot(symbol: str, interval: str) -> dict[str, Any]:
    """Build an empty but schema-complete offline snapshot for local deployment."""
    config = RulesConfig()
    snapshot = build_dashboard_snapshot_v2(
        config,
        (),
        mode="watch",
        status="empty",
        data_source="fixture",
        market_24h={"available": False, "reason": "demo_mode_no_upstream"},
        runtime={
            "data_source": "fixture",
            "symbol": symbol,
            "interval": interval,
            "status": "empty",
        },
    )
    snapshot["market"]["symbol"] = symbol
    snapshot["market"]["interval"] = interval
    snapshot["runtime"]["symbol"] = symbol
    snapshot["runtime"]["interval"] = interval
    return snapshot


def fixture_snapshot(symbol: str, interval: str, *, limit: int = 600) -> _FixtureProvider:
    """Run a synthetic 1m fixture through the native backend and project a snapshot.

    Returns a fixture provider (callable for the snapshot payload plus an
    ``inspect`` method for R2 per-bar inspection). The provider keeps the
    canonical domain objects so ``/api/dashboard/inspect`` can rebuild
    containment provenance via :func:`trace_containment`.
    """
    return fixture_provider(symbol, interval, limit=limit)


def _synthetic_bar(*, index: int, interval_ms: int, symbol: str) -> Any:
    """Deterministic offline bar used for ``fixture`` mode UI smoke."""
    from cpt.domain.models import make_canonical_bar

    open_time = 1_700_000_000_000 + index * interval_ms
    wave = 100 + 10 * math.sin(index / 6.0)
    return make_canonical_bar(
        open_time=open_time,
        open=wave,
        high=wave + 1.5,
        low=wave - 1.5,
        close=wave + (0.5 if index % 2 else -0.5),
        close_time=open_time + interval_ms - 1,
        volume=1.0,
        quote_volume=wave,
        trade_count=10,
        is_closed=True,
    )


def _reference_config(config: RulesConfig) -> ReferenceChanlunConfig:
    return ReferenceChanlunConfig(
        use_fx_qy_middle=config.fx_qy_middle,
        use_fx_qj_ck=config.fx_qj_ck,
        use_bi_type_new=config.bi_type_new,
        zs_wzgx=config.zs_wzgx,
        macd_fast=config.macd_fast,
        macd_slow=config.macd_slow,
        macd_signal=config.macd_signal,
    )


def _compute_domain_structures(
    bars: tuple[CanonicalBar, ...],
    config: RulesConfig,
    backend: NativeChanlunBackend,
) -> tuple[tuple[Fractal, ...], tuple[Bi, ...], tuple[ZhongShu, ...]]:
    """Resolve domain objects from a native backend pass for live inspection."""
    ref_config = _reference_config(config)
    primary_level = config.levels[0]
    result = backend.compute_structures(list(bars), ref_config)
    fractals = tuple(
        map_fractal(
            fx,
            level=primary_level,
            source_ids=(f"b:{fx.bar_index}",),
            bars=bars,
        )
        for fx in result.fx_list
    )
    bis = tuple(
        map_bi(
            bi,
            level=primary_level,
            source_ids=(f"fx:{bi.start_bar}", f"fx:{bi.end_bar}"),
            bars=bars,
        )
        for bi in result.bi_list
    )
    zhongshus = tuple(
        map_zhongshu(
            zs,
            level=primary_level,
            source_ids=tuple(f"bi:{i}" for i in zs.bi_indices),
            bars=bars,
        )
        for zs in result.zs_list
    )
    return fractals, bis, zhongshus


def _compute_trend_types(
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    config: RulesConfig,
) -> tuple[TrendType, ...]:
    """按主级别对笔/中枢分类走势类型；失败只降级本叠加层，绝不向上抛。

    走势类型是叠加层而非主图数据，分类异常必须退化成空元组——否则一个
    走势类型边界问题会连带打掉整张快照。
    """
    try:
        return classify_trend(bis, zhongshus, level=config.levels[0])
    except (ValueError, TypeError) as exc:
        _LOG.warning("trend type classification failed: %s", exc)
        return ()


def _snapshot_from_bars(
    config: RulesConfig,
    backend: NativeChanlunBackend,
    bars: tuple[CanonicalBar, ...],
    *,
    symbol: str,
    interval: str,
    data_source: str,
    status: str = "confirmed",
) -> dict[str, Any]:
    """把一段 K 线跑成完整 v2 快照（主叠加层 + 多级别 + 走势类型）。

    供「按时间范围查询」这类一次性重建使用：与轮询路径同源（同一 backend、
    同一守卫），但读的是调用方给定的 bars，不碰 provider 的实时状态。
    """
    fractals, bis, zhongshus = _compute_domain_structures(bars, config, backend)
    try:
        multi = build_multi_level(bars, config, backend, levels=tuple(config.levels))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("multi-level build failed for range snapshot: %s", exc)
        multi = _fallback_multi_level(config, fractals, bis, zhongshus)
    if not multi.get(config.levels[0], {}).get("fractals"):
        multi = _fallback_multi_level(config, fractals, bis, zhongshus)
    snapshot = build_dashboard_snapshot_v2(
        config,
        bars,
        fractals=fractals,
        bis=bis,
        zhongshus=zhongshus,
        trend_types=_compute_trend_types(bis, zhongshus, config),
        mode="research",
        status=status,
        data_source=data_source,
        multi_level=_format_multi_level(multi),
        config_compare=_compare_with_default(config),
        runtime={
            "data_source": data_source,
            "symbol": symbol,
            "interval": interval,
            "buffer_size": len(bars),
            "window_size": len(bars),
            "status": status,
        },
    )
    snapshot["market"]["symbol"] = symbol
    snapshot["market"]["interval_ms"] = resolve_interval_ms(interval)
    snapshot["runtime"]["symbol"] = symbol
    snapshot["runtime"]["interval"] = interval
    return snapshot


class _FixtureProvider:
    """Static snapshot+inspect provider for ``fixture`` mode."""

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> None:
        self._symbol = symbol
        self._interval = interval
        self._config = RulesConfig()
        self._backend = NativeChanlunBackend()
        self._interval_ms = resolve_interval_ms(interval)
        self._bars: tuple[CanonicalBar, ...] = tuple(
            _synthetic_bar(index=index, interval_ms=self._interval_ms, symbol=symbol)
            for index in range(limit)
        )
        self._snapshot: dict[str, Any] = self._build_snapshot()

    def snapshot_payload(self) -> dict[str, Any]:
        return dict(self._snapshot)

    def snapshot_for_range(self, start_ms: int, end_ms: int) -> dict[str, Any]:
        """按 [start_ms, end_ms] 从合成序列切出窗口并重建快照。"""
        window = tuple(bar for bar in self._bars if start_ms <= bar.open_time <= end_ms)
        if len(window) < 3:
            empty = demo_snapshot(self._symbol, self._interval)
            empty["range"] = {
                "available": False,
                "reason": "range_too_short",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "bar_count": len(window),
            }
            return empty
        snapshot = _snapshot_from_bars(
            self._config,
            self._backend,
            window,
            symbol=self._symbol,
            interval=self._interval,
            data_source="fixture_history",
        )
        snapshot["range"] = {
            "available": True,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "bar_count": len(window),
        }
        return snapshot

    def snapshot_for_level(self, level: int) -> dict[str, Any]:
        try:
            multi = build_multi_level(
                self._bars, self._config, self._backend, levels=tuple(self._config.levels)
            )
        except Exception:
            multi = {lv: {"fractals": (), "bis": (), "zhongshus": ()} for lv in self._config.levels}
        fractals, bis, zhongshus = structures_for_level(multi, level)
        rebuilt = build_dashboard_snapshot_v2(
            self._config,
            self._bars,
            fractals=fractals,
            bis=bis,
            zhongshus=zhongshus,
            mode="research",
            status="confirmed",
            data_source="native_fixture",
            multi_level=_format_multi_level(multi),
            config_compare=_compare_with_default(self._config),
        )
        rebuilt["market"]["symbol"] = self._symbol
        rebuilt["market"]["interval"] = self._interval_ms
        rebuilt["runtime"]["symbol"] = self._symbol
        rebuilt["runtime"]["interval"] = self._interval
        rebuilt["reproducibility"] = self._snapshot.get("reproducibility", {})
        rebuilt["selected_level"] = level
        return rebuilt

    def inspect(self, bar_index: int) -> dict[str, Any]:
        fractals, bis, zhongshus = _compute_domain_structures(
            self._bars, self._config, self._backend
        )
        return inspect_bar(self._bars, fractals, bis, zhongshus, bar_index)

    def _build_snapshot(self) -> dict[str, Any]:
        try:
            # 走一遍真实回放入口以复用其数据守卫（去重/递增/缺口/OHLC 校验）。
            replay_bars(self._bars, config=self._config, backend=self._backend)
        except Exception:  # noqa: BLE001 — never let fixture mode crash the server
            return demo_snapshot(self._symbol, self._interval)
        fractals, bis, zhongshus = _compute_domain_structures(
            self._bars, self._config, self._backend
        )
        multi = self._compute_multi_level()
        if not multi.get(self._config.levels[0], {}).get("fractals"):
            # 多级别递归失败（或退化）时，主级别仍使用真实结构。
            multi = _fallback_multi_level(self._config, fractals, bis, zhongshus)
        snapshot = build_dashboard_snapshot_v2(
            self._config,
            self._bars,
            fractals=fractals,
            bis=bis,
            zhongshus=zhongshus,
            trend_types=_compute_trend_types(bis, zhongshus, self._config),
            mode="watch",
            status="confirmed",
            data_source="native_fixture",
            market_24h={"available": False, "reason": "fixture_mode_no_upstream"},
            multi_level=_format_multi_level(multi),
            config_compare=_compare_with_default(self._config),
            runtime={
                "data_source": "native_fixture",
                "symbol": self._symbol,
                "interval": self._interval,
                "buffer_size": len(self._bars),
                "window_size": len(self._bars),
                "status": "confirmed",
            },
        )
        snapshot["market"]["symbol"] = self._symbol
        snapshot["market"]["interval"] = self._interval_ms
        snapshot["runtime"]["symbol"] = self._symbol
        snapshot["runtime"]["interval"] = self._interval
        snapshot["runtime"]["buffer_size"] = len(self._bars)
        snapshot["runtime"]["window_size"] = len(self._bars)
        return snapshot

    def _compute_multi_level(self) -> dict[int, dict[str, tuple[Any, ...]]]:
        try:
            return build_multi_level(
                self._bars,
                self._config,
                self._backend,
                levels=tuple(self._config.levels),
            )
        except Exception as exc:  # noqa: BLE001 — 多级别失败只降级多级别，不阻断主流程
            _LOG.warning("multi-level build failed in fixture mode: %s", exc)
            return {
                level: {"fractals": (), "bis": (), "zhongshus": ()} for level in self._config.levels
            }


def _fallback_multi_level(
    config: RulesConfig,
    fractals: tuple[Fractal, ...],
    bis: tuple[Bi, ...],
    zhongshus: tuple[ZhongShu, ...],
) -> dict[int, dict[str, tuple[Any, ...]]]:
    """多级别递归不可用时的退路：主级别保留真实结构，其余级别留空。

    绝不允许"高级别失败"连坐主级别——那会让看板显示零结构，与真实数据矛盾。
    """
    primary = config.levels[0]
    return {
        level: (
            {"fractals": fractals, "bis": bis, "zhongshus": zhongshus}
            if level == primary
            else {"fractals": (), "bis": (), "zhongshus": ()}
        )
        for level in config.levels
    }


def _compare_with_default(config: RulesConfig) -> dict[str, Any]:
    default = RulesConfig()
    differences = compare_configs(default, config)
    return {
        "available": True,
        "differences": tuple(differences),
        "baseline": "default_rules_config()",
    }


def _format_multi_level(
    multi: dict[int, dict[str, tuple[Any, ...]]],
) -> dict[str, Any]:
    levels: dict[str, dict[str, int]] = {}
    for level, entry in sorted(multi.items()):
        levels[str(level)] = {
            "fractals": len(entry["fractals"]),
            "bis": len(entry["bis"]),
            "zhongshus": len(entry["zhongshus"]),
        }
    return {
        "available": True,
        "levels": levels,
        "primary_level": min(multi.keys()) if multi else None,
    }


def fixture_provider(symbol: str, interval: str, *, limit: int = 600) -> _FixtureProvider:
    return _FixtureProvider(symbol=symbol, interval=interval, limit=limit)


class _RealtimeProvider:
    """Thread-safe poll-driven snapshot builder fed by Binance REST.

    Holds the most recent canonical bars so ``/api/dashboard/inspect`` can rebuild
    domain structures for the B3 trace_containment wiring. Tracks signal status
    transitions across polls and exposes ``alerts`` in the snapshot for
    browser-side notification/audio triggers.
    """

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
        poll_seconds: float,
    ) -> None:
        self._symbol = symbol
        self._interval = interval
        self._limit = limit
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = demo_snapshot(symbol, interval)
        self._bars: tuple[CanonicalBar, ...] = ()
        self._last_signal_status: str = "none"
        self._last_alert_at: float = 0.0
        self._stop = threading.Event()
        self._client = BinanceFuturesClient()
        self._config = RulesConfig()
        self._backend = NativeChanlunBackend()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cpt-realtime")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot_payload(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def snapshot_for_range(self, start_ms: int, end_ms: int) -> dict[str, Any]:
        """按 [start_ms, end_ms] 向上游拉取历史 K 线并重建快照（只读、无副作用）。"""
        try:
            bars = self._client.fetch_validated_klines(
                self._symbol,
                self._interval,
                start_time=start_ms,
                end_time=end_ms,
                limit=MAX_KLINES_LIMIT,
            )
        except Exception as exc:  # noqa: BLE001 — 上游不可用时如实降级，不伪造数据
            _LOG.warning("historical range fetch failed: %s", exc)
            empty = demo_snapshot(self._symbol, self._interval)
            empty["range"] = {
                "available": False,
                "reason": "upstream_range_unavailable",
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
            return empty
        if len(bars) < 3:
            empty = demo_snapshot(self._symbol, self._interval)
            empty["range"] = {
                "available": False,
                "reason": "range_too_short",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "bar_count": len(bars),
            }
            return empty
        snapshot = _snapshot_from_bars(
            self._config,
            self._backend,
            tuple(bars),
            symbol=self._symbol,
            interval=self._interval,
            data_source="binance_history",
        )
        snapshot["market_24h"] = self._safe_24h()
        snapshot["range"] = {
            "available": True,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "bar_count": len(bars),
        }
        return snapshot

    def inspect(self, bar_index: int) -> dict[str, Any]:
        with self._lock:
            bars = self._bars
        if not bars:
            raise IndexError("inspect unavailable: no bars yet")
        fractals, bis, zhongshus = _compute_domain_structures(bars, self._config, self._backend)
        return inspect_bar(bars, fractals, bis, zhongshus, bar_index)

    def snapshot_for_level(self, level: int) -> dict[str, Any]:
        with self._lock:
            bars = self._bars
            base = dict(self._snapshot)
        if not bars:
            return base
        try:
            multi = build_multi_level(
                bars, self._config, self._backend, levels=tuple(self._config.levels)
            )
        except Exception:
            multi = {lv: {"fractals": (), "bis": (), "zhongshus": ()} for lv in self._config.levels}
        fractals, bis, zhongshus = structures_for_level(multi, level)
        rebuilt = build_dashboard_snapshot_v2(
            self._config,
            bars,
            fractals=fractals,
            bis=bis,
            zhongshus=zhongshus,
            mode="research",
            status="confirmed",
            data_source="binance_realtime",
            multi_level=_format_multi_level(multi),
            config_compare=_compare_with_default(self._config),
        )
        for key in ("market", "runtime", "reproducibility", "alerts", "market_24h"):
            if key in base:
                rebuilt[key] = base[key]
        rebuilt["selected_level"] = level
        return rebuilt

    def _run(self) -> None:
        while not self._stop.wait(self._poll_seconds):
            try:
                fresh, fresh_bars = self._poll_once()
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("realtime poll failed: %s", exc)
                continue
            with self._lock:
                self._snapshot = fresh
                self._bars = fresh_bars

    def _poll_once(self) -> tuple[dict[str, Any], tuple[CanonicalBar, ...]]:
        try:
            bars = self._client.fetch_validated_klines(
                self._symbol, self._interval, limit=self._limit
            )
        except Exception:
            return demo_snapshot(self._symbol, self._interval), ()
        try:
            # 复用真实回放入口的数据守卫（去重/递增/缺口/OHLC 校验）。
            replay_bars(bars, config=self._config, backend=self._backend)
        except Exception:
            return demo_snapshot(self._symbol, self._interval), ()
        market_24h = self._safe_24h()
        # 主叠加层独立计算：即使多级别递归失败，主图也必须有真实结构。
        fractals, bis, zhongshus = _compute_domain_structures(
            tuple(bars), self._config, self._backend
        )
        try:
            multi = build_multi_level(
                tuple(bars), self._config, self._backend, levels=tuple(self._config.levels)
            )
        except Exception as exc:  # noqa: BLE001 — 多级别失败只降级多级别，不阻断已算好的主结构
            _LOG.warning("multi-level build failed in realtime mode: %s", exc)
            multi = _fallback_multi_level(self._config, fractals, bis, zhongshus)
        snapshot = build_dashboard_snapshot_v2(
            self._config,
            bars,
            fractals=fractals,
            bis=bis,
            zhongshus=zhongshus,
            trend_types=_compute_trend_types(bis, zhongshus, self._config),
            mode="watch",
            status="confirmed",
            data_source="binance_realtime",
            market_24h=market_24h,
            multi_level=_format_multi_level(multi),
            config_compare=_compare_with_default(self._config),
            runtime={
                "data_source": "binance_realtime",
                "symbol": self._symbol,
                "interval": self._interval,
                "buffer_size": len(bars),
                "window_size": len(bars),
                "status": "confirmed",
                "stale": False,
            },
        )
        snapshot["market"]["symbol"] = self._symbol
        snapshot["market"]["interval_ms"] = resolve_interval_ms(self._interval)
        snapshot["runtime"]["symbol"] = self._symbol
        snapshot["runtime"]["interval"] = self._interval
        snapshot["runtime"]["buffer_size"] = len(bars)
        snapshot["runtime"]["window_size"] = len(bars)
        snapshot["alerts"] = self._compute_alerts(snapshot)
        return snapshot, tuple(bars)

    def _safe_24h(self) -> dict[str, Any]:
        try:
            ticker = self._client.fetch_24h_ticker(self._symbol)
        except Exception:  # noqa: BLE001 — keep API alive on transient upstream errors
            return {"available": False, "reason": "upstream_ticker_unavailable"}
        return normalize_24h(ticker)

    def _compute_alerts(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        transition = alert_transition({"signal": {"status": self._last_signal_status}}, snapshot)
        self._last_signal_status = transition["current_status"]
        alerts: list[dict[str, Any]] = []
        if transition["triggered"]:
            self._last_alert_at = _time.monotonic()
            alerts.append(
                {
                    "kind": "signal_transition",
                    "status": transition["current_status"],
                    "previous_status": transition["previous_status"],
                    "reason": transition["reason"],
                    "at": self._last_alert_at,
                }
            )
        return alerts


def realtime_snapshot(
    symbol: str, interval: str, *, limit: int = 600, poll_seconds: float = 30.0
) -> _RealtimeProvider:
    return _RealtimeProvider(
        symbol=symbol, interval=interval, limit=limit, poll_seconds=poll_seconds
    )


def _query_value(query: Mapping[str, str], key: str, fallback: str) -> str:
    return query.get(key, fallback) or fallback


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CPT read-only Dashboard API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--mode",
        choices=("demo", "fixture", "realtime"),
        default="demo",
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1h")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=30.0,
        help="Polling interval for realtime mode.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=600,
        help="K-line limit per upstream request (realtime mode).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    provider_callable: object
    if args.mode == "demo":
        snapshot = demo_snapshot(args.symbol, args.interval)

        def provider_callable() -> dict[str, Any]:
            return snapshot

    elif args.mode == "fixture":
        fixture = fixture_snapshot(args.symbol, args.interval, limit=args.limit)
        provider_callable = fixture
    else:
        rt_provider = realtime_snapshot(
            args.symbol,
            args.interval,
            limit=args.limit,
            poll_seconds=args.poll_seconds,
        )
        provider_callable = rt_provider
    server = serve_snapshot(provider_callable, host=args.host, port=args.port)
    print(
        f"CPT Dashboard API listening on http://{args.host}:{server.server_port} "
        f"(mode={args.mode})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
