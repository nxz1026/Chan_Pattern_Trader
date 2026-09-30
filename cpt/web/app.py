"""Minimal standard-library HTTP adapter for a supplied Dashboard snapshot."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from cpt.adapters.binance_futures import resolve_interval_label
from cpt.application.dashboard_compare import compare_snapshots
from cpt.application.dashboard_export import slice_snapshot
from cpt.application.dashboard_levels import level_tree
from cpt.application.dashboard_multi_run import align_runs
from cpt.application.dashboard_runs import find_run, recent_runs, record_run, run_body
from cpt.application.dashboard_stats import signal_statistics
from cpt.application.dashboard_watchlist import watchlist_rows

_LOG = logging.getLogger("cpt.web.handler")

SnapshotProvider = Callable[[], dict[str, Any]]


class SnapshotSource(Protocol):
    """Read-only provider exposing a ``snapshot_payload`` method."""

    def snapshot_payload(self) -> dict[str, Any]: ...


@runtime_checkable
class MultiLevelSource(Protocol):
    """Read-only provider that can rebuild a snapshot for a given level."""

    def snapshot_for_level(self, level: int) -> dict[str, Any]: ...


@runtime_checkable
class InspectProvider(Protocol):
    """Read-only per-bar inspection contract for ``/api/dashboard/inspect``."""

    def inspect(self, bar_index: int) -> dict[str, Any]: ...


@runtime_checkable
class RangeSource(Protocol):
    """Read-only provider that can rebuild a snapshot for an explicit time window."""

    def snapshot_for_range(self, start_ms: int, end_ms: int) -> dict[str, Any]: ...


@runtime_checkable
class SelectableSource(Protocol):
    """Provider that supports hot-reloading the live symbol/interval.

    Implemented by ``_RealtimeProvider`` so the HTTP layer can route
    ``?symbol=`` / ``?interval_ms=`` requests to the live data stream
    rather than the snapshot's cached market.symbol field only.
    """

    def select_symbol(self, symbol: str, interval: str) -> None: ...

    def force_refresh(self) -> None: ...


def _with_run_index(payload: dict[str, Any]) -> dict[str, Any]:
    """在 **HTTP 响应层** 注入运行索引（R20 接线）。

    为什么不在 ``build_dashboard_snapshot_v2`` 里接：运行历史是**进程级状态**，
    放进去会让 snapshot 变成非确定性的 —— 同参数两次调用返回不等，直接打破
    ``tests/test_web_a_share.py::test_provider_caches_snapshot_within_ttl`` 守的
    缓存语义，也会让 ``dashboard_compare`` 的字段级 diff 永远有一处差异
    （``runs``）。这是接线过程中实测踩到的，不是推演。

    只有含 ``market`` 的完整 snapshot 才注入；``/api/dashboard/reproducibility``
    这类子字段响应保持原样。

    ``record_run`` 自带去重：realtime 模式 30s 内可能有十几次请求命中同一份
    缓存 snapshot，不去重会把「一次运行」记成十几条。
    """
    if "market" not in payload:
        return payload
    record_run(payload)
    enriched = dict(payload)
    enriched["runs"] = list(recent_runs())
    return enriched


#: diff 摘要里的内容指纹长度（hex 字符）。12 位足够区分，又不会把响应撑大。
_DIFF_HASH_CHARS = 12


def _diff_digest(value: Any) -> str:
    """序列内容指纹：同内容同摘要、跨进程稳定（内置 ``hash`` 带随机种子，不能用）。"""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:_DIFF_HASH_CHARS]


def _summarize_diff_value(name: str, value: Any) -> Any:
    """把 diff 里的序列型值降级成 ``{"__summary__": ..., "count": ..., "hash": ...}``。

    ``snapshot_diff`` 是**顶层**字段级 diff，而 ``overlays`` 是「dict 里套 4 个 list」——
    只判顶层是不是 list 会漏掉它，一份快照几百根 K 线的原始数组正是从这个口子漏进
    响应的（前端 ``JSON.stringify`` 会渲染成几兆巨串），所以这里必须递归。
    """
    if isinstance(value, (list, tuple)):
        return {"__summary__": name, "count": len(value), "hash": _diff_digest(value)}
    if isinstance(value, dict):
        return {key: _summarize_diff_value(f"{name}.{key}", item) for key, item in value.items()}
    return value


def _summarize_diff_entries(entries: Any) -> list[dict[str, Any]]:
    """把 ``snapshot_diff`` 的每条差异摘要化（理由见 :func:`_summarize_diff_value`）。"""
    summarized: list[dict[str, Any]] = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("field"))
        summarized.append(
            {
                "field": entry.get("field"),
                "left": _summarize_diff_value(name, entry.get("left")),
                "right": _summarize_diff_value(name, entry.get("right")),
            }
        )
    return summarized


def _signal_stats_payload(days: int, code: str | None) -> dict[str, Any]:
    """C5 信号事件统计。**降级不抛**：psycopg 缺失 / DB 不可达 / 表不存在都回 available=false。

    ``basis`` 是刻意自报的口径：这批统计是**信号状态跃迁事件**的分布，不是「当前若干
    只票的状态快照」。不自报的话前端只能猜，而两者数值完全不同。
    """
    payload: dict[str, Any] = {
        "schema_version": "dashboard_signal_stats.v1",
        "basis": "signal_event_transitions",
        "days": days,
    }
    try:
        # CI 只跑 ``pip install -e .``（不带 [db]），故连接层必须惰性导入。
        from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
        from cpt.application.signal_event_store import load_signal_events  # noqa: PLC0415

        client = AShareLocalClient()
        try:
            events = load_signal_events(client._get_conn(), days=days, code=code)  # noqa: SLF001
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 — 只读增强，DB 抖动不该让主视图 500
        _LOG.warning("signal stats unavailable: %s", exc)
        payload["available"] = False
        payload["reason"] = "signal_history_unavailable"
        return payload
    payload["available"] = True
    payload["stats"] = signal_statistics(list(events))
    return payload


def _latest_signal_statuses() -> dict[str, str]:
    """各 code 的最新信号状态；事件流不可用 → 空表（调用方补 ``"none"``）。

    返回空 dict 是**诚实**的缺省：``watchlist_rows`` 的 ``signal_status`` 缺省本就是
    ``"none"``，所以「没读到」不会伪装成别的结论，只是这一列失去信息量。
    """
    try:
        from cpt.adapters.a_share_local import AShareLocalClient  # noqa: PLC0415
        from cpt.application.signal_event_store import load_signal_events  # noqa: PLC0415

        client = AShareLocalClient()
        try:
            events = load_signal_events(client._get_conn(), days=30)  # noqa: SLF001
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("watchlist signal statuses unavailable: %s", exc)
        return {}
    latest: dict[str, str] = {}
    for event in events:  # 已按 transition_time 倒序 → 首次出现即最新
        event_code = event.get("code")
        if isinstance(event_code, str) and event_code not in latest:
            latest[event_code] = str(event.get("status", "none"))
    return latest


def _watchlist_payload() -> dict[str, Any]:
    """C6 候选池 → ``watchlist_rows`` 入参形状。**只做映射**，不新增计算口径。

    价格/涨跌幅来自本地库最近两根收盘价，信号状态来自信号事件流最新一条。任一环取不到
    就填诚实的缺省值（``None`` / ``"none"``），而不是 500 —— 一只票取不到价不该把整个
    列表搞挂。
    """
    from cpt.web import a_share_routes  # noqa: PLC0415 — 避免顶层拖入 psycopg

    schema = {"schema_version": "dashboard_watchlist.v1"}
    try:
        pool = a_share_routes.pool_payload()
    except Exception as exc:  # noqa: BLE001 — 池子不可用是正常降级
        _LOG.warning("watchlist pool unavailable: %s", exc)
        return {**schema, "available": False, "reason": "pool_unavailable"}
    items = pool.get("items") or []
    # 池子全空 + 记了 db_error ⇒ 是「库连不上」而不是「今天确实没票」。
    if pool.get("db_error") and not items:
        return {**schema, "available": False, "reason": "pool_unavailable"}

    statuses = _latest_signal_statuses()
    markets: list[dict[str, Any]] = []
    for item in items:
        code = item.get("code")
        closes = a_share_routes.recent_closes(code) if isinstance(code, str) and code else None
        prev_close = closes[0] if closes else None
        last_close = closes[1] if closes else None
        change_pct: float | None = None
        if prev_close:  # 0 或 None 都算取不到：除以 0 会炸，且 0 价本身无意义
            if last_close is not None:
                change_pct = (last_close - prev_close) / prev_close * 100
        markets.append(
            {
                "symbol": code,
                "last_price": last_close,
                "change_pct": change_pct,
                "signal_status": statuses.get(code, "none") if isinstance(code, str) else "none",
                "available": bool(item.get("drawable")),
            }
        )
    return {
        **schema,
        "available": True,
        "as_of": pool.get("as_of"),
        "rows": list(watchlist_rows(markets)),
    }


def make_handler(
    provider: SnapshotProvider | SnapshotSource,
) -> type[BaseHTTPRequestHandler]:
    """Create a read-only handler bound to a thread-safe snapshot provider.

    The provider must use independent repository/connection state per request when
    backed by SQLite; this adapter does not serialize concurrent calls for it.

    If ``provider`` additionally exposes ``inspect(bar_index)``, the
    ``/api/dashboard/inspect`` route is enabled for B3 per-bar inspection.
    """

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path)
            query = parse_qs(path.query)
            # A 股路由**不依赖**加密 provider，必须在 ``provider()`` 之前分发：
            # 否则 (a) 每次 A 股请求都白建一次加密快照，(b) 加密侧不可达时这里
            # 会先 send_error(500)，A 股路由永远走不到。
            if path.path.startswith("/api/dashboard/a-share/"):
                self._handle_a_share_get(path.path, query)
                return
            try:
                if callable(provider) and not hasattr(provider, "snapshot_payload"):
                    snapshot = provider()
                else:
                    snapshot = provider.snapshot_payload()
            except Exception:  # noqa: BLE001
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "snapshot unavailable")
                return
            if path.path == "/api/dashboard/health":
                # 有真实健康能力的 provider（realtime）优先：避免静态 "ok": true
                # 掩盖上游不可达或降级。没有该能力的 provider 退回静态只读声明。
                health_fn = getattr(provider, "health", None)
                if callable(health_fn):
                    try:
                        payload: dict[str, Any] = dict(health_fn())
                    except Exception as exc:  # noqa: BLE001 — 健康检查本身失败也要能返回
                        _LOG.warning("provider.health() failed: %s", exc)
                        payload = {
                            "ok": False,
                            "read_only": True,
                            "degraded": True,
                            "last_error": f"health_probe_failed:{type(exc).__name__}",
                        }
                else:
                    payload = {"ok": True, "read_only": True, "degraded": False}
            elif path.path == "/api/dashboard/snapshot":
                payload = dict(snapshot)
                market = dict(payload.get("market", {}))
                runtime = dict(payload.get("runtime", {}))
                # 当 provider 支持 hot-reload（realtime 模式）时，把 ?symbol= / ?interval_ms=
                # 透传给底层 provider，让下一次响应已经是新交易对的数据，
                # 而不是只改 payload 字段、K 线仍为旧交易对。
                # demo / fixture 模式 provider 没有 select_symbol，按下面 fallback 走。
                provider_switched = False
                provider_error: str | None = None
                if query.get("symbol") and isinstance(provider, SelectableSource):
                    requested_symbol = query["symbol"][0]
                    requested_interval = runtime.get("interval") or "1h"
                    if query.get("interval_ms"):
                        try:
                            interval_ms = int(query["interval_ms"][0])
                        except ValueError:
                            self.send_error(HTTPStatus.BAD_REQUEST, "interval_ms must be int")
                            return
                        requested_interval = resolve_interval_label(interval_ms)
                    try:
                        provider.select_symbol(requested_symbol, requested_interval)
                        provider.force_refresh()
                        provider_switched = True
                    except Exception as exc:  # noqa: BLE001
                        provider_error = f"symbol_switch_failed:{exc}"
                        _LOG.warning("provider.select_symbol failed: %s", exc)
                    else:
                        # 强制刷新后重读 snapshot（已经是新交易对）
                        snapshot = provider.snapshot_payload()
                        payload = dict(snapshot)
                        market = dict(payload.get("market", {}))
                        runtime = dict(payload.get("runtime", {}))
                if not provider_switched:
                    if query.get("symbol"):
                        market["symbol"] = query["symbol"][0]
                        runtime["symbol"] = query["symbol"][0]
                    if query.get("interval_ms"):
                        try:
                            market["interval_ms"] = int(query["interval_ms"][0])
                        except ValueError:
                            self.send_error(HTTPStatus.BAD_REQUEST, "interval_ms must be int")
                            return
                payload["market"] = market
                payload["runtime"] = runtime
                if provider_error:
                    payload["provider_warnings"] = [provider_error]
                range_applied = False
                if query.get("start_ms") or query.get("end_ms"):
                    raw_start = (query.get("start_ms") or [""])[0]
                    raw_end = (query.get("end_ms") or [""])[0]
                    try:
                        start_ms = int(raw_start)
                        end_ms = int(raw_end)
                    except ValueError:
                        self.send_error(HTTPStatus.BAD_REQUEST, "start_ms/end_ms must be integers")
                        return
                    if start_ms >= end_ms:
                        self.send_error(
                            HTTPStatus.BAD_REQUEST, "start_ms must be earlier than end_ms"
                        )
                        return
                    if not isinstance(provider, RangeSource):
                        payload = {"available": False, "reason": "range_unavailable_in_mode"}
                        return self._write_json(payload)
                    try:
                        payload = provider.snapshot_for_range(start_ms, end_ms)
                    except Exception:  # noqa: BLE001
                        self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "range rebuild failed")
                        return
                    payload = dict(payload)
                    payload["market"] = dict(payload.get("market", {}))
                    payload["runtime"] = dict(payload.get("runtime", {}))
                    range_applied = True
                if (
                    query.get("level")
                    and not range_applied
                    and isinstance(provider, MultiLevelSource)
                ):
                    try:
                        level = int(query["level"][0])
                    except ValueError:
                        self.send_error(HTTPStatus.BAD_REQUEST, "level must be an integer")
                        return
                    try:
                        payload = provider.snapshot_for_level(level)
                    except Exception:  # noqa: BLE001
                        self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "level rebuild failed")
                        return
                    payload = dict(payload)
                    payload["market"] = dict(payload.get("market", {}))
                    payload["runtime"] = dict(payload.get("runtime", {}))
                    payload["market"]["symbol"] = query.get(
                        "symbol", [payload["market"].get("symbol", "")]
                    )[0] or payload["market"].get("symbol")
                    payload["runtime"]["symbol"] = payload["runtime"]["symbol"]
                    if query.get("interval_ms"):
                        payload["market"]["interval_ms"] = int(query["interval_ms"][0])
            elif path.path == "/api/dashboard/reproducibility":
                payload = snapshot.get("reproducibility", {})
            elif path.path == "/api/dashboard/parity":
                payload = snapshot.get("parity", {})
            elif path.path == "/api/dashboard/runs":
                # R20：snapshot 本体的 ``runs`` 恒为 []（领域层必须保持「同输入同
                # 输出」），运行历史走进程内环形缓冲。理由见 _with_run_index。
                payload = {"runs": list(recent_runs())}
            elif path.path == "/api/dashboard/market-24h":
                payload = snapshot.get("market_24h", {"available": False, "reason": "unavailable"})
            elif path.path == "/api/dashboard/engine-state":
                payload = snapshot.get("engine_state", {})
            elif path.path == "/api/dashboard/inspect":
                bar_index_raw = (query.get("bar_index") or [""])[0]
                try:
                    bar_index = int(bar_index_raw)
                except ValueError:
                    self.send_error(HTTPStatus.BAD_REQUEST, "bar_index must be an integer")
                    return
                if not isinstance(provider, InspectProvider):
                    payload = {"available": False, "reason": "inspect_unavailable_in_mode"}
                    return self._write_json(payload)
                try:
                    payload = provider.inspect(bar_index)
                except IndexError as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                except Exception:  # noqa: BLE001
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "inspect failed")
                    return
            elif path.path == "/api/dashboard/sources":
                # R17 数据源能力注册表 + 探活。默认**不**碰消耗额度的源（Wind）：
                # 一次探测就是一次真实额度，必须显式 ?include_quota=1 才允许。
                # 探活结果有 60s 进程内缓存，?refresh=1 强制重探。
                from cpt.adapters.source_registry import (  # noqa: PLC0415
                    capabilities_payload,
                    clear_cache,
                )

                include_quota = (query.get("include_quota") or ["0"])[0] not in {"0", "false", ""}
                if (query.get("refresh") or ["0"])[0] not in {"0", "false", ""}:
                    clear_cache()
                markets_raw = (query.get("markets") or [""])[0]
                markets = tuple(part for part in markets_raw.split(",") if part) or None
                payload = capabilities_payload(include_quota=include_quota, markets=markets)
            elif path.path == "/api/dashboard/signal-radar":
                # Phase N1：信号雷达 —— 只读聚合当前 snapshot 的信号数据。
                # 不新增计算口径，仅把 snapshot 中已有的 signal / signal_first_sell
                # 翻译成前端友好的扁平结构，含状态中文标签与新鲜度。
                from time import time as _now  # noqa: PLC0415

                def _radar_entry(
                    signal: dict[str, Any] | None,
                    code: str,
                    name: str,
                    market: str,
                    source_label: str = "",
                ) -> dict[str, Any] | None:
                    if not signal:
                        return None
                    status = signal.get("status", "none")
                    signal_type = signal.get("signal_type", "first_buy")
                    # 取最晚的时间戳作为 signal_time
                    times = [
                        signal.get("alert_time"),
                        signal.get("candidate_time"),
                        signal.get("confirmed_time"),
                        signal.get("invalidated_time"),
                    ]
                    valid_times = [t for t in times if t is not None]
                    signal_time = max(valid_times) if valid_times else None
                    now_ms = int(_now() * 1000)
                    freshness_ms = now_ms - signal_time if signal_time else None
                    return {
                        "code": code,
                        "name": name,
                        "market": market,
                        "signal_type": signal_type,
                        "status": status,
                        "level": signal.get("level"),
                        "signal_time": signal_time,
                        "freshness_ms": freshness_ms,
                        "price": signal.get("price"),
                        "divergence_status": signal.get("divergence_status"),
                        "source_label": source_label,
                    }

                market_label = (snapshot.get("market", {}) or {}).get("symbol", "")
                market_name = (snapshot.get("market", {}) or {}).get("name", market_label)
                market_type = (
                    "a_share" if market_label and not market_label.endswith("USDT") else "crypto"
                )

                entries = []
                sig = snapshot.get("summary", {}).get("signal") if snapshot.get("summary") else None
                if sig:
                    entry = _radar_entry(sig, market_label, market_name, market_type)
                    if entry:
                        entries.append(entry)
                sig_sell = (
                    snapshot.get("summary", {}).get("signal_first_sell")
                    if snapshot.get("summary")
                    else None
                )
                if sig_sell:
                    entry = _radar_entry(sig_sell, market_label, market_name, market_type, "一卖")
                    if entry:
                        entries.append(entry)

                payload = {
                    "available": True,
                    "signals": entries,
                    "disclaimer": (
                        "本页面为只读结构分析与学习工具，所有「信号/状态」均为缠论结构术语，"
                        "不构成投资建议、要约或任何买卖/持仓建议。市场有风险，投资须谨慎。"
                    ),
                }
            elif path.path == "/api/dashboard/export":
                # Phase 6 P2：时间范围切片导出。范围参数是**必填**的 —— 导出默认全量
                # 会给出几百根 K 线的响应，而调用方要的从来是某个可研究的区间。
                raw_start = (query.get("start_ms") or [""])[0]
                raw_end = (query.get("end_ms") or [""])[0]
                try:
                    start_ms = int(raw_start)
                    end_ms = int(raw_end)
                except ValueError:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "invalid_range", "start_ms/end_ms 必须是整数"
                    )
                    return
                if start_ms >= end_ms:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "invalid_range", "start_ms 必须早于 end_ms"
                    )
                    return
                if not snapshot:
                    payload = {
                        "schema_version": "dashboard_export.v1",
                        "available": False,
                        "reason": "snapshot_unavailable",
                    }
                else:
                    sliced = slice_snapshot(dict(snapshot), start_ms, end_ms)
                    payload = {
                        "schema_version": "dashboard_export.v1",
                        "available": True,
                        "slice": {"start_ms": start_ms, "end_ms": end_ms},
                        "candle_count": len(sliced.get("candles", [])),
                        "snapshot": sliced,
                    }
            elif path.path == "/api/dashboard/levels":
                # Phase 5 P1：级别递归树。吃的是**带 level 键的结构序列**，
                # 活路径上就是 overlays 的四类结构（asdict 后含 level）。
                raw_overlays = snapshot.get("overlays")
                overlays: dict[str, Any] = raw_overlays if isinstance(raw_overlays, dict) else {}
                structures: list[dict[str, Any]] = []
                for key in ("fractals", "bis", "zhongshus", "trend_types"):
                    chunk = overlays.get(key) if isinstance(overlays, dict) else None
                    if isinstance(chunk, list):
                        structures.extend(chunk)
                if not structures:
                    payload = {
                        "schema_version": "dashboard_levels.v1",
                        "available": False,
                        "reason": "overlays_unavailable",
                    }
                else:
                    payload = {
                        "schema_version": "dashboard_levels.v1",
                        "available": True,
                        "levels": list(level_tree(structures)),
                    }
            elif path.path == "/api/dashboard/compare":
                left_id = (query.get("left") or [""])[0].strip()
                right_id = (query.get("right") or [""])[0].strip()
                if not left_id or not right_id:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST,
                        "missing_run_id",
                        "必须同时提供 left 与 right 两个 run_id",
                    )
                    return
                left_body = run_body(left_id)
                right_body = run_body(right_id)
                if left_body is None or right_body is None:
                    payload = {
                        "schema_version": "dashboard_compare.v1",
                        "available": False,
                        "reason": "run_body_unavailable",
                    }
                else:
                    compared = compare_snapshots(left_body, right_body)
                    # 摘要化：``differences`` 里绝不允许出现整段 candles/overlays
                    # 原始数组（几百根 K 线的字符串会让前端渲染卡死）。
                    compared["differences"] = _summarize_diff_entries(compared.get("differences"))
                    # 索引行里的 dataset_hash/run_id 比本体里的更权威：本体可能被摘要/
                    # 裁剪过，而 fixture 模式的 runtime 根本不带 run_id（索引行由
                    # `runtime.run_id or dataset_hash` 推导出来），不回填就会是 null。
                    for side, run_id in (("left", left_id), ("right", right_id)):
                        index_row = find_run(run_id) or {}
                        compared[f"{side}_run_id"] = index_row.get("run_id") or run_id
                        if index_row.get("dataset_hash") is not None:
                            compared[f"{side}_dataset_hash"] = index_row["dataset_hash"]
                    payload = {
                        "schema_version": "dashboard_compare.v1",
                        "available": True,
                        **compared,
                    }
            elif path.path == "/api/dashboard/multi-run":
                raw_ids = (query.get("run_ids") or [""])[0]
                run_ids = [part.strip() for part in raw_ids.split(",") if part.strip()]
                if not 2 <= len(run_ids) <= 5:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_run_ids",
                        "run_ids 需为 2..5 个逗号分隔的 run_id",
                    )
                    return
                bodies = [
                    body for body in (run_body(run_id) for run_id in run_ids) if body is not None
                ]
                if not bodies:
                    payload = {
                        "schema_version": "dashboard_multi_run.v1",
                        "available": False,
                        "reason": "run_body_unavailable",
                    }
                else:
                    payload = {
                        "schema_version": "dashboard_multi_run.v1",
                        "available": True,
                        **align_runs(bodies),
                    }
            elif path.path == "/api/dashboard/signal-stats":
                days_raw = (query.get("days") or ["30"])[0]
                try:
                    days = int(days_raw)
                except ValueError:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "invalid_days", "days 必须是整数"
                    )
                    return
                code_filter = (query.get("code") or [""])[0].strip() or None
                payload = _signal_stats_payload(days, code_filter)
            elif path.path == "/api/dashboard/watchlist":
                payload = _watchlist_payload()
            elif path.path == "/api/canvas/wbt":
                # 画布 D（R16-5）：服务端用 wbt 的 HtmlReportBuilder 渲染报告片段。
                # 客户端必须传可视窗口（start_ms/end_ms），否则只画窗口的 A/B/C
                # 与画全量的 D 计数不相等，"四画布一致"的验收无从谈起。
                window: tuple[int, int] | None = None
                if query.get("start_ms") and query.get("end_ms"):
                    try:
                        window = (int(query["start_ms"][0]), int(query["end_ms"][0]))
                    except ValueError:
                        self.send_error(HTTPStatus.BAD_REQUEST, "start_ms/end_ms must be integers")
                        return
                # 延迟导入：wbt/pandas/plotly 是可选依赖，demo/加密路径不该被迫加载。
                from cpt.application.canvas_wbt import build_canvas_d_payload  # noqa: PLC0415

                # A 股模式（R17-3）：本路由是**服务端**取数的，默认拿 provider 的
                # 加密快照。客户端画布 D 必须带上 code，否则会出现 A/B/C 画 A 股、
                # D 画 BTCUSDT 的**跨市场错配** —— 实测 579 根加密 K 线 vs 123 根
                # A 股 K 线同时出现在一屏上。
                canvas_code = (query.get("code") or [""])[0].strip()
                if canvas_code:
                    from cpt.web import a_share_routes  # noqa: PLC0415

                    try:
                        snapshot = a_share_routes.snapshot_payload(canvas_code)
                    except a_share_routes.InvalidCodeError as exc:
                        self._write_json_error(HTTPStatus.BAD_REQUEST, "invalid_code", str(exc))
                        return
                payload = build_canvas_d_payload(snapshot, window=window)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._write_json(_with_run_index(payload))

        # ---------------------------------------------------------- A 股（R17-3）

        def _handle_a_share_get(self, path: str, query: dict[str, list[str]]) -> None:
            """A 股三条只读路由：snapshot / pool / watchlist。"""
            from cpt.web import a_share_routes  # noqa: PLC0415 — 避免顶层拖入 psycopg

            if path == "/api/dashboard/a-share/snapshot":
                code = (query.get("code") or [""])[0].strip()
                if not code:
                    self._write_json_error(
                        HTTPStatus.BAD_REQUEST, "code_required", "缺少 code 参数"
                    )
                    return
                width_raw = (query.get("width_k") or [""])[0]
                width_k = a_share_routes.DEFAULT_WIDTH_K
                if width_raw:
                    try:
                        width_k = int(width_raw)
                    except ValueError:
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST, "width_k_not_int", "width_k 必须是整数"
                        )
                        return
                    if not 5 <= width_k <= 2000:
                        self._write_json_error(
                            HTTPStatus.BAD_REQUEST,
                            "width_k_out_of_range",
                            f"width_k 超出范围 5..2000：{width_k}",
                        )
                        return
                try:
                    payload = a_share_routes.snapshot_payload(code, width_k=width_k)
                except a_share_routes.InvalidCodeError as exc:
                    # 代码格式非法回 400（JSON 体，见 _write_json_error 的注释：
                    # 中文消息不能走 send_error）。
                    self._write_json_error(HTTPStatus.BAD_REQUEST, "invalid_code", str(exc))
                    return
                self._write_json(_with_run_index(payload))
                return
            if path == "/api/dashboard/a-share/pool":
                self._write_json(a_share_routes.pool_payload())
                return
            if path == "/api/dashboard/a-share/watchlist":
                self._write_json(a_share_routes.watchlist_payload())
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def _handle_a_share_write(self, method: str) -> bool:
            """A 股自选的写操作；返回 False 表示这不是 A 股路由。"""
            path = urlsplit(self.path)
            if path.path != "/api/dashboard/a-share/watchlist":
                return False
            # 审计 M1：写接口无鉴权（单用户看板经 nginx 暴露）。至少要求
            # ``Content-Type: application/json``——HTML 表单 / 简单请求只能发
            # form-urlencoded / text/plain，而声明 json 的跨站 fetch 会触发
            # CORS 预检并失败。不引入鉴权系统，仅抬高跨站触发门槛。
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type.lower():
                # 415（不是 400）：缺/错的 Content-Type 是"媒体类型不受支持"，
                # 与 code 非法（400 invalid_code）区分开，客户端能精确归因。
                self._write_json_error(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "content_type_required",
                    "写接口要求 Content-Type: application/json",
                )
                return True
            from cpt.web import a_share_routes  # noqa: PLC0415

            query = parse_qs(path.query)
            code = (query.get("code") or [""])[0].strip()
            if not code:
                self._write_json_error(HTTPStatus.BAD_REQUEST, "code_required", "缺少 code 参数")
                return True
            try:
                if method == "POST":
                    payload = a_share_routes.watchlist_add(code)
                else:
                    payload = a_share_routes.watchlist_remove(code)
            except a_share_routes.InvalidCodeError as exc:
                self._write_json_error(HTTPStatus.BAD_REQUEST, "invalid_code", str(exc))
                return True
            self._write_json(payload)
            return True

        def _write_json(self, payload: dict[str, Any]) -> None:
            self._write_json_status(HTTPStatus.OK, payload)

        def _write_json_error(self, status: HTTPStatus, code: str, message: str) -> None:
            """以 JSON 体返回错误。

            **不要用 ``send_error`` 传非 ASCII 文本**：``BaseHTTPRequestHandler``
            把 message 写进 HTTP 状态行，而状态行只能 latin-1 编码 —— 中文消息会
            抛 ``UnicodeEncodeError`` 并**直接断开连接**，浏览器侧只看到网络错误
            （实测踩到：``code=abc`` 的 400 变成了 RemoteDisconnected）。
            """
            self._write_json_status(status, {"error": {"code": code, "message": message}})

        def _write_json_status(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            try:
                encoded = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError):
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "payload is not JSON-safe")
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:  # noqa: N802
            if self._handle_a_share_write("POST"):
                return
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

        def do_DELETE(self) -> None:  # noqa: N802
            if self._handle_a_share_write("DELETE"):
                return
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

        def log_message(self, format: str, *args: object) -> None:
            return

    return DashboardHandler


def serve_snapshot(
    provider: SnapshotProvider | SnapshotSource,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """Build a server; caller owns lifecycle and must call ``server_close``."""
    return ThreadingHTTPServer((host, port), make_handler(provider))
