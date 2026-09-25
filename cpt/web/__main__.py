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
from collections.abc import Sequence
from typing import Any

from cpt.adapters.backend_factory import BACKEND_CHOICES, DEFAULT_BACKEND, resolve_backend
from cpt.adapters.binance_futures import (
    MAX_KLINES_LIMIT,
    BinanceFuturesClient,
    resolve_interval_ms,
)
from cpt.adapters.reference_chanlun import ChanlunBackend
from cpt.application.dashboard_alerts import alert_transition
from cpt.application.dashboard_config_compare import compare_configs
from cpt.application.dashboard_inspector import inspect_bar
from cpt.application.dashboard_market import normalize_24h
from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.application.multi_level import build_multi_level, structures_for_level
from cpt.application.replay import compute_domain_structures, replay_bars
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
            "generated_at": _time.time() * 1000,
        },
    )
    snapshot["market"]["symbol"] = symbol
    snapshot["market"]["interval"] = interval
    snapshot["runtime"]["symbol"] = symbol
    snapshot["runtime"]["interval"] = interval
    return snapshot


def fixture_snapshot(
    symbol: str,
    interval: str,
    *,
    limit: int = 600,
    backend: ChanlunBackend | None = None,
) -> _FixtureProvider:
    """Run a synthetic 1m fixture through the native backend and project a snapshot.

    Returns a fixture provider (callable for the snapshot payload plus an
    ``inspect`` method for R2 per-bar inspection). The provider keeps the
    canonical domain objects so ``/api/dashboard/inspect`` can rebuild
    containment provenance via :func:`trace_containment`.
    """
    return fixture_provider(symbol, interval, limit=limit, backend=backend)


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


def _compute_domain_structures(
    bars: tuple[CanonicalBar, ...],
    config: RulesConfig,
    backend: ChanlunBackend,
) -> tuple[tuple[Fractal, ...], tuple[Bi, ...], tuple[ZhongShu, ...]]:
    """Resolve domain objects from a native backend pass for live inspection.

    实现已上移到 :func:`cpt.application.replay.compute_domain_structures`
    （R16-3）——A 股 web 适配器需要同一份 dataclass 映射，不能再各写一份。
    本包装保留原签名，避免改动 5 处调用点。
    """
    return compute_domain_structures(bars, config, backend)


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
    backend: ChanlunBackend,
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
            "generated_at": _time.time() * 1000,
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
        backend: ChanlunBackend | None = None,
    ) -> None:
        self._symbol = symbol
        self._interval = interval
        self._limit = limit
        self._config = RulesConfig()
        # R16-4：后端由 ``--backend`` 决定（默认 auto = 装了 czsc 就用 czsc）。
        self._backend: ChanlunBackend = backend or resolve_backend(
            DEFAULT_BACKEND, min_bi_len=self._config.min_bi_len
        )
        self._interval_ms = resolve_interval_ms(interval)
        self._lock = threading.Lock()
        self._bars: tuple[CanonicalBar, ...] = tuple(
            _synthetic_bar(index=index, interval_ms=self._interval_ms, symbol=symbol)
            for index in range(limit)
        )
        self._snapshot: dict[str, Any] = self._build_snapshot()

    def select_symbol(self, symbol: str, interval: str) -> None:
        """Switch the synthetic series to a new symbol/interval.

        Unlike ``_RealtimeProvider`` there is no upstream fetch and no thread;
        we just rebuild the synthetic bars + snapshot under the lock. The HTTP
        layer's ``force_refresh`` call has no work to do because
        ``snapshot_payload`` already returns the freshly-built snapshot.
        """
        if self._symbol == symbol and self._interval == interval:
            return
        interval_ms = resolve_interval_ms(interval)
        bars = tuple(
            _synthetic_bar(index=index, interval_ms=interval_ms, symbol=symbol)
            for index in range(self._limit)
        )
        with self._lock:
            self._symbol = symbol
            self._interval = interval
            self._interval_ms = interval_ms
            self._bars = bars
            self._snapshot = self._build_snapshot()

    def snapshot_payload(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def snapshot_for_range(self, start_ms: int, end_ms: int) -> dict[str, Any]:
        """按 [start_ms, end_ms] 从合成序列切出窗口并重建快照。"""
        with self._lock:
            symbol = self._symbol
            interval = self._interval
            bars = self._bars
        window = tuple(bar for bar in bars if start_ms <= bar.open_time <= end_ms)
        if len(window) < 3:
            empty = demo_snapshot(symbol, interval)
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
            symbol=symbol,
            interval=interval,
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
        with self._lock:
            bars = self._bars
            symbol = self._symbol
            interval = self._interval
            interval_ms = self._interval_ms
            reproducibility = self._snapshot.get("reproducibility", {})
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
            data_source="native_fixture",
            multi_level=_format_multi_level(multi),
            config_compare=_compare_with_default(self._config),
        )
        rebuilt["market"]["symbol"] = symbol
        rebuilt["market"]["interval"] = interval_ms
        rebuilt["runtime"]["symbol"] = symbol
        rebuilt["runtime"]["interval"] = interval
        rebuilt["reproducibility"] = reproducibility
        rebuilt["selected_level"] = level
        return rebuilt

    def inspect(self, bar_index: int) -> dict[str, Any]:
        with self._lock:
            bars = self._bars
        fractals, bis, zhongshus = _compute_domain_structures(bars, self._config, self._backend)
        return inspect_bar(bars, fractals, bis, zhongshus, bar_index)

    def _build_snapshot(self) -> dict[str, Any]:
        # 调用者负责持锁（__init__ / select_symbol 内）
        with self._lock:
            bars = self._bars
            symbol = self._symbol
            interval = self._interval
            interval_ms = self._interval_ms
            config = self._config
            backend = self._backend
        try:
            # 走一遍真实回放入口以复用其数据守卫（去重/递增/缺口/OHLC 校验）。
            replay_bars(bars, config=config, backend=backend)
        except Exception:  # noqa: BLE001 — never let fixture mode crash the server
            return demo_snapshot(symbol, interval)
        fractals, bis, zhongshus = _compute_domain_structures(bars, config, backend)
        multi = self._compute_multi_level()
        if not multi.get(config.levels[0], {}).get("fractals"):
            # 多级别递归失败（或退化）时，主级别仍使用真实结构。
            multi = _fallback_multi_level(config, fractals, bis, zhongshus)
        snapshot = build_dashboard_snapshot_v2(
            config,
            bars,
            fractals=fractals,
            bis=bis,
            zhongshus=zhongshus,
            trend_types=_compute_trend_types(bis, zhongshus, config),
            mode="watch",
            status="confirmed",
            data_source="native_fixture",
            market_24h={"available": False, "reason": "fixture_mode_no_upstream"},
            multi_level=_format_multi_level(multi),
            config_compare=_compare_with_default(config),
            runtime={
                "data_source": "native_fixture",
                "symbol": symbol,
                "interval": interval,
                "buffer_size": len(bars),
                "window_size": len(bars),
                "status": "confirmed",
                "generated_at": _time.time() * 1000,
            },
        )
        snapshot["market"]["symbol"] = symbol
        snapshot["market"]["interval"] = interval_ms
        snapshot["runtime"]["symbol"] = symbol
        snapshot["runtime"]["interval"] = interval
        snapshot["runtime"]["buffer_size"] = len(bars)
        snapshot["runtime"]["window_size"] = len(bars)
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


def fixture_provider(
    symbol: str,
    interval: str,
    *,
    limit: int = 600,
    backend: ChanlunBackend | None = None,
) -> _FixtureProvider:
    return _FixtureProvider(symbol=symbol, interval=interval, limit=limit, backend=backend)


class _RealtimeProvider:
    """Thread-safe poll-driven snapshot builder fed by Binance REST.

    Holds the most recent canonical bars so ``/api/dashboard/inspect`` can rebuild
    domain structures for the B3 trace_containment wiring. Tracks signal status
    transitions across polls and exposes ``alerts`` in the snapshot for
    browser-side notification/audio triggers.

    Supports hot-reloading ``symbol`` / ``interval`` via :meth:`select_symbol`.
    The HTTP handler calls this whenever a request carries ``?symbol=`` or
    ``?interval_ms=`` so the front-end symbol picker takes effect immediately
    rather than waiting for the next 30s poll. The select call blocks ≤1 poll
    cycle to give the caller a fresh snapshot to return; the background poll
    thread remains alive and continues polling the new symbol.
    """

    def __init__(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
        poll_seconds: float,
        backend: ChanlunBackend | None = None,
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
        # 上游健康状态：/api/dashboard/health 直接读这些字段，避免「永远 ok」
        # 的静态健康端点掩盖上游不可达 / 静默降级。
        self._last_poll_ok: bool = False
        self._last_poll_error: str | None = "no_poll_yet"
        self._last_poll_at: float = 0.0
        self._consecutive_failures: int = 0
        self._last_success_at: float = 0.0
        self._stop = threading.Event()
        # Set when select_symbol() flips symbol/interval; the poll loop
        # clears cached bars + last-signal state at the start of the next poll.
        self._switch_event = threading.Event()
        self._client = BinanceFuturesClient()
        self._config = RulesConfig()
        # R16-4：同 _FixtureProvider，后端走 ``--backend``（默认 auto）。
        self._backend: ChanlunBackend = backend or resolve_backend(
            DEFAULT_BACKEND, min_bi_len=self._config.min_bi_len
        )
        self._thread = threading.Thread(target=self._run, daemon=True, name="cpt-realtime")
        self._thread.start()

    def select_symbol(self, symbol: str, interval: str) -> None:
        """Switch the live symbol/interval on the next poll cycle.

        No-op if the requested pair matches the current one. When different,
        flip atomically under ``_lock`` and trigger the background thread to
        rebuild the snapshot for the new pair on its next iteration. Callers
        that need an immediate fresh snapshot for the new pair should invoke
        ``force_refresh()`` (HTTP layer does this so the first response after
        a switch is already on the new symbol).
        """
        with self._lock:
            if self._symbol == symbol and self._interval == interval:
                return
            self._symbol = symbol
            self._interval = interval
            self._bars = ()
            self._snapshot = demo_snapshot(symbol, interval)
            self._last_signal_status = "none"
            self._last_alert_at = 0.0
        self._switch_event.set()

    def force_refresh(self) -> None:
        """Run a single poll cycle synchronously and publish the result.

        Used by the HTTP layer so the first response after ``select_symbol``
        already reflects the new symbol rather than the stale 30s-old
        snapshot. Safe to call concurrently with the background poller — the
        poll body reads ``self._symbol`` under the lock that ``_run`` itself
        doesn't take (it only mutates ``_snapshot`` / ``_bars`` under the
        lock), so the new thread just races to publish last.
        """
        fresh, fresh_bars = self._poll_once()
        with self._lock:
            self._snapshot = fresh
            self._bars = fresh_bars

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

    def _degraded_snapshot(self, symbol: str, interval: str, reason: str) -> dict[str, Any]:
        """上游不可用时的降级快照：空结构，但**显式标记降级原因**。

        原先这里直接 ``return demo_snapshot(...)``，前端只看到「图表保持空占位」，
        无法区分「上游挂了」和「本来就没有数据」。现在把原因写进 runtime，
        由 /api/dashboard/health 与前端状态区共同呈现。
        """
        snapshot = demo_snapshot(symbol, interval)
        runtime = dict(snapshot.get("runtime", {}))
        runtime["degraded"] = True
        runtime["degraded_reason"] = reason
        runtime["generated_at"] = _time.time() * 1000
        snapshot["runtime"] = runtime
        return snapshot

    def _record_poll(self, *, ok: bool, error: str | None) -> None:
        with self._lock:
            self._last_poll_ok = ok
            self._last_poll_error = None if ok else (error or "unknown_error")
            self._last_poll_at = _time.time()
            if ok:
                self._consecutive_failures = 0
                self._last_success_at = self._last_poll_at
            else:
                self._consecutive_failures += 1

    def health(self) -> dict[str, Any]:
        """真实健康状态（供 /api/dashboard/health 使用）。

        ``ok`` 只在最近一次轮询成功时为 true；``degraded`` 表示当前展示的是
        降级快照。``stale`` 表示最近一次成功已超过 2 个轮询周期。
        """
        with self._lock:
            last_poll_ok = self._last_poll_ok
            last_poll_error = self._last_poll_error
            last_poll_at = self._last_poll_at
            last_success_at = self._last_success_at
            failures = self._consecutive_failures
            symbol = self._symbol
            interval = self._interval
            snapshot = self._snapshot
        runtime = snapshot.get("runtime", {}) if isinstance(snapshot, dict) else {}
        degraded = bool(runtime.get("degraded", False)) or not last_poll_ok
        stale = bool(
            last_success_at
            and (self._poll_seconds * 2) > 0
            and (_time.time() - last_success_at) > (self._poll_seconds * 2)
        )
        return {
            "ok": bool(last_poll_ok),
            "read_only": True,
            "degraded": degraded,
            "stale": stale,
            "symbol": symbol,
            "interval": interval,
            "poll_seconds": self._poll_seconds,
            "last_poll_at": last_poll_at or None,
            "last_success_at": last_success_at or None,
            "consecutive_failures": failures,
            "last_error": last_poll_error,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            # 优先响应 select_symbol：切完立刻拉新数据，而不是等满 30s。
            triggered = self._switch_event.wait(self._poll_seconds)
            if self._stop.is_set():
                return
            if triggered:
                self._switch_event.clear()
            try:
                fresh, fresh_bars = self._poll_once()
            except Exception as exc:  # noqa: BLE001 — 轮询整体失败：记录健康状态，不静默吞掉
                _LOG.warning("realtime poll failed: %s", exc)
                reason = f"poll_failed:{type(exc).__name__}"
                self._record_poll(ok=False, error=reason)
                with self._lock:
                    symbol = self._symbol
                    interval = self._interval
                with self._lock:
                    self._snapshot = self._degraded_snapshot(symbol, interval, reason)
                    self._bars = ()
                continue
            with self._lock:
                self._snapshot = fresh
                self._bars = fresh_bars

    def _poll_once(self) -> tuple[dict[str, Any], tuple[CanonicalBar, ...]]:
        # 在锁内快照当前 symbol/interval，避免 fetch 期间被 select_symbol 改写
        # 导致 bars 与 market_24h 来自不同交易对。
        with self._lock:
            target_symbol = self._symbol
            target_interval = self._interval
        try:
            bars = self._client.fetch_validated_klines(
                target_symbol, target_interval, limit=self._limit
            )
        except Exception as exc:  # noqa: BLE001 — 上游不可达：降级但必须留痕
            _LOG.warning("upstream klines fetch failed for %s: %s", target_symbol, exc)
            reason = f"upstream_fetch_failed:{type(exc).__name__}"
            self._record_poll(ok=False, error=reason)
            return self._degraded_snapshot(target_symbol, target_interval, reason), ()
        try:
            # 复用真实回放入口的数据守卫（去重/递增/缺口/OHLC 校验）。
            replay_bars(bars, config=self._config, backend=self._backend)
        except Exception as exc:  # noqa: BLE001 — 数据守卫拒绝：同样降级留痕
            _LOG.warning("realtime bars rejected by guard for %s: %s", target_symbol, exc)
            reason = f"data_guard_rejected:{type(exc).__name__}"
            self._record_poll(ok=False, error=reason)
            return self._degraded_snapshot(target_symbol, target_interval, reason), ()
        self._record_poll(ok=True, error=None)
        market_24h = self._safe_24h_for(target_symbol)
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
                "symbol": target_symbol,
                "interval": target_interval,
                "buffer_size": len(bars),
                "window_size": len(bars),
                "status": "confirmed",
                "stale": False,
                "generated_at": _time.time() * 1000,
            },
        )
        snapshot["market"]["symbol"] = target_symbol
        snapshot["market"]["interval_ms"] = resolve_interval_ms(target_interval)
        snapshot["runtime"]["symbol"] = target_symbol
        snapshot["runtime"]["interval"] = target_interval
        snapshot["runtime"]["buffer_size"] = len(bars)
        snapshot["runtime"]["window_size"] = len(bars)
        snapshot["alerts"] = self._compute_alerts(snapshot)
        return snapshot, tuple(bars)

    def _safe_24h(self) -> dict[str, Any]:
        with self._lock:
            sym = self._symbol
        return self._safe_24h_for(sym)

    def _safe_24h_for(self, symbol: str) -> dict[str, Any]:
        try:
            ticker = self._client.fetch_24h_ticker(symbol)
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
    symbol: str,
    interval: str,
    *,
    limit: int = 600,
    poll_seconds: float = 30.0,
    backend: ChanlunBackend | None = None,
) -> _RealtimeProvider:
    return _RealtimeProvider(
        symbol=symbol,
        interval=interval,
        limit=limit,
        poll_seconds=poll_seconds,
        backend=backend,
    )


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
    parser.add_argument(
        "--backend",
        choices=BACKEND_CHOICES,
        default=DEFAULT_BACKEND,
        help=(
            "缠论后端：auto=装了 czsc 就用 czsc（默认）；czsc=强制 czsc"
            "（未安装即报错）；native=CPT 自研（回放/对照）。"
        ),
    )
    return parser


class _DemoProvider:
    """Empty-snapshot provider for ``demo`` mode with hot-reload support.

    The demo snapshot is schema-complete but contains no bars; switching the
    symbol/interval only re-tags the payload fields. Implemented as a small
    class (rather than a bare closure) so the HTTP ``SelectableSource`` duck
    type can drive it like the other providers.
    """

    def __init__(self, symbol: str, interval: str) -> None:
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = demo_snapshot(symbol, interval)

    def select_symbol(self, symbol: str, interval: str) -> None:
        with self._lock:
            self._snapshot = demo_snapshot(symbol, interval)

    def snapshot_payload(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    provider_callable: object
    if args.mode == "demo":
        provider_callable = _DemoProvider(args.symbol, args.interval)
    elif args.mode == "fixture":
        fixture = fixture_snapshot(
            args.symbol, args.interval, limit=args.limit, backend=resolve_backend(args.backend)
        )
        provider_callable = fixture
    else:
        rt_provider = realtime_snapshot(
            args.symbol,
            args.interval,
            limit=args.limit,
            poll_seconds=args.poll_seconds,
            backend=resolve_backend(args.backend),
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
