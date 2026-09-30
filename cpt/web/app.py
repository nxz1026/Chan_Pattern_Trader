"""Minimal standard-library HTTP adapter for a supplied Dashboard snapshot."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from cpt.adapters.binance_futures import resolve_interval_label
from cpt.application.dashboard_runs import recent_runs, record_run

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

                def _radar_entry(signal, code, name, market, source_label=""):
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
                market_type = "a_share" if market_label and not market_label.endswith("USDT") else "crypto"

                entries = []
                sig = snapshot.get("summary", {}).get("signal") if snapshot.get("summary") else None
                if sig:
                    entry = _radar_entry(sig, market_label, market_name, market_type)
                    if entry:
                        entries.append(entry)
                sig_sell = snapshot.get("summary", {}).get("signal_first_sell") if snapshot.get("summary") else None
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
            # 审计 M1：写接口至少校验 Content-Type，提高跨站触发门槛。
            # 不引入完整鉴权（看板是单用户内网应用），但拒绝非 JSON 的写请求。
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type.lower():
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
