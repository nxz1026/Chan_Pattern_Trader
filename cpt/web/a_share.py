"""A 股 web 适配器（R16）。

``cpt.web.__main__`` 给加密 BTC/ETH 等提供 HTTP dashboard。
本模块**单独**为 A 股日线提供等价 HTTP 端点，复用
``cpt.web.app.make_handler`` 的所有路由（``/api/dashboard/snapshot`` /
``/api/dashboard/health`` 等），snapshot schema v2 100% 兼容。

## 数据流
1. ``AShareLocalClient.fetch_validated_klines(code, start_ms, end_ms)`` → 后复权
   ``CanonicalBar`` 列表；
2. ``cpt.application.replay.compute_domain_structures`` → 缠论结构（分型/笔/中枢，
   返回 dataclass，**不是** schema v1 payload）；
3. ``build_dashboard_snapshot_v2`` → 与加密侧完全相同的 dashboard snapshot。

## 与加密侧的区别
- **没有"实时轮询"**：A 股日线收盘后不再变 → snapshot 在 HTTP 请求里
  按需重建（不维护后台线程、不缓存）；
- **没有 ``select_symbol``**：切换代码靠重启进程 / 改 ``--code``；
- **没有"interval"**：日线固定 ``1d``。

## 用法
::

    python -m cpt.web.a_share --code 000002 --port 8011

    curl http://127.0.0.1:8011/api/dashboard/snapshot | jq .market
"""

from __future__ import annotations

import argparse
import logging
import time as _time
from datetime import UTC, datetime
from typing import Any

from cpt.adapters.a_share_local import AShareLocalClient
from cpt.adapters.backend_factory import BACKEND_CHOICES, DEFAULT_BACKEND, resolve_backend
from cpt.adapters.reference_chanlun import ChanlunBackend
from cpt.adapters.validators import validate_ashare_bars
from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.application.replay import compute_domain_structures
from cpt.domain.config import RulesConfig
from cpt.web.app import serve_snapshot

_LOG = logging.getLogger("cpt.web.a_share")

#: A 股日线固定 1d 间隔（24h）。与 ``BinanceFuturesClient.INTERVAL_MS["1d"]`` 同值。
_INTERVAL_MS: int = 24 * 3600 * 1000

#: A 股展示周期 = 30 天（30 根 K 线足够覆盖笔/中枢，又不至于慢）。
DEFAULT_WIDTH_K: int = 30


def build_ashare_snapshot(
    code: str,
    *,
    width_k: int = DEFAULT_WIDTH_K,
    client: AShareLocalClient | None = None,
    backend: ChanlunBackend | None = None,
) -> dict[str, Any]:
    """为 ``code`` 构造 dashboard snapshot（v2 schema，与加密侧同）。

    :param code: A 股 6 位裸码（如 ``"600519"``）。
    :param width_k: 最近多少根 K 线（默认 30）。
    :param client: 可选注入的 :class:`AShareLocalClient`。不传时 lazy 默认连接
        —— **这需要运行 venv 装 psycopg**。CPT 的 ``db`` extra 提供
        （``pip install -e ".[db]"``）。
    :param backend: 缠论后端；``None`` 时走 :func:`resolve_backend` 的 ``auto``
        档（装了 czsc 就用 czsc）。传 ``NativeChanlunBackend()`` 可做对照。
    """
    owns_client = client is None
    if owns_client:
        active_client: AShareLocalClient = AShareLocalClient()
    else:
        assert client is not None  # type assertion only — mypy 收窄
        active_client = client
    try:
        end_ms = int(datetime.now(UTC).timestamp() * 1000)
        # 多预留 60 根以保证缠论结构稳定
        start_ms = end_ms - (width_k + 60) * _INTERVAL_MS
        result = active_client.fetch_validated_klines(code, start_ms, end_ms)
        canonical = list(result.bars)
        if not canonical:
            return _empty_snapshot(code, "no_data")
    except Exception as exc:  # noqa: BLE001
        # DB 不可达 → 返回 degraded snapshot，**不静默成 OK**
        _LOG.warning("A 股 DB 拉取失败 %s: %s", code, exc)
        return _empty_snapshot(code, f"db_error:{type(exc).__name__}")
    finally:
        if owns_client:
            active_client.close()

    # A 股走专用校验（允许周末/节假日/停牌的日历缺口，见
    # cpt.adapters.validators.validate_ashare_bars）。
    # 不能用 replay_bars —— 它的连续性契约是加密市场假设，A 股会误报 DataGapError。
    try:
        validated = validate_ashare_bars(canonical, interval_ms=_INTERVAL_MS)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("A 股序列校验失败 %s: %s", code, exc)
        return _empty_snapshot(code, f"invalid_bars:{type(exc).__name__}")

    # 用 ``compute_domain_structures`` 拿 **dataclass** 结构对象（分型/笔/中枢）。
    # 不能改用 ``run_replay``：它返回 ``export_dataset`` 的 schema v1 payload，
    # 结构元素在 ``payload["data"]`` 下且已 ``asdict`` 成普通 dict —— 直接喂给
    # ``build_dashboard_snapshot_v2`` 会在 ``asdict(f)`` 处炸
    # ``TypeError: asdict() should be called on dataclass instances``；写成
    # ``payload.get("fractals")`` 更糟：静默 ``None`` → overlays 全空、K 线上
    # 一条笔都不画（实测两者都踩过）。
    # 走势类型（``trend_types``）是 CPT 自研、尚未接入 replay，保持空元组。
    active_backend = backend or resolve_backend(
        DEFAULT_BACKEND, min_bi_len=RulesConfig().min_bi_len
    )
    fractals, bis, zhongshus = compute_domain_structures(validated, RulesConfig(), active_backend)
    snapshot = build_dashboard_snapshot_v2(
        config=RulesConfig(),
        bars=validated,
        fractals=fractals,
        bis=bis,
        zhongshus=zhongshus,
        trend_types=(),
        mode="watch",
        status="confirmed",
        data_source="db_local",
        runtime={
            "data_source": "db_local",
            "symbol": code,
            "interval": "1d",
            "status": "confirmed",
            "interval_ms": _INTERVAL_MS,
            "as_of_ms": int(datetime.now(UTC).timestamp() * 1000),
        },
    )
    # 显式覆盖（v1 默认 BTCUSDT）— A 股代码在 market.symbol
    snapshot["market"]["symbol"] = code
    snapshot["market"]["kind"] = "a_share"
    snapshot["market"]["interval"] = "1d"
    return snapshot


def _empty_snapshot(code: str, reason: str) -> dict[str, Any]:
    """DB 真空时的占位 snapshot（前端可识别为"无数据"）。"""
    return {
        "schema_version": "dashboard.v2",
        "market": {
            "symbol": code,
            "kind": "a_share",
            "interval_ms": _INTERVAL_MS,
            "bar_count": 0,
        },
        "candles": [],
        # 形状必须与真实路径一致（dict of 4 keys），否则前端在
        # degraded 分支会崩（实测踩到：空快照曾返回 []）
        "overlays": {"fractals": [], "bis": [], "zhongshus": [], "trend_types": []},
        "signal": None,  # 真实路径无信号时也是 None（保持一致，见 _market/_overlays 注释）
        "events": [],
        "data_quality": {"degraded": True, "reason": reason},
        "runtime": {
            "as_of_ms": int(datetime.now(UTC).timestamp() * 1000),
            "interval_ms": _INTERVAL_MS,
            "interval": "1d",
            "degraded": True,
            "degraded_reason": reason,
            "data_source": "db_local",
            "status": "empty",
        },
        "config": RulesConfig().to_dict(),
    }


class AShareSnapshotProvider:
    """``SnapshotProvider`` 实现（见 ``cpt.web.app`` 的 ``SnapshotProvider`` Protocol）。"""

    def __init__(
        self,
        code: str,
        *,
        width_k: int = DEFAULT_WIDTH_K,
        backend: ChanlunBackend | None = None,
    ) -> None:
        self._code = code
        self._width_k = width_k
        # 后端实例**只建一次**：czsc 后端每次构造都要走版本校验 + 导入，
        # 而 provider 是长驻对象（60s TTL 缓存），不该每轮重建。
        self._backend: ChanlunBackend = backend or resolve_backend(
            DEFAULT_BACKEND, min_bi_len=RulesConfig().min_bi_len
        )
        self._cached: dict[str, Any] | None = None
        self._cached_at: float = 0.0
        self._cache_ttl_s: float = 60.0  # A 股日线 60s 缓存足够

    def snapshot_payload(self) -> dict[str, Any]:
        now = _time.time()
        if self._cached is None or (now - self._cached_at) > self._cache_ttl_s:
            self._cached = build_ashare_snapshot(
                self._code, width_k=self._width_k, backend=self._backend
            )
            self._cached_at = now
        return dict(self._cached)


def _serve(
    code: str, host: str, port: int, width_k: int, backend: ChanlunBackend | None = None
) -> None:
    from http.server import HTTPServer

    provider = AShareSnapshotProvider(code, width_k=width_k, backend=backend)
    handler_cls = serve_snapshot.__wrapped__ if hasattr(serve_snapshot, "__wrapped__") else None
    # ``serve_snapshot`` 是 ``cpt.web.app`` 的模块级函数；直接构造 handler 类
    from cpt.web.app import make_handler

    handler_cls = make_handler(provider)
    httpd = HTTPServer((host, port), handler_cls)
    _LOG.info("A 股 dashboard 启动 http://%s:%d code=%s", host, port, code)
    httpd.serve_forever()


def main() -> None:

    parser = argparse.ArgumentParser(description="A 股 Dashboard HTTP 入口")
    parser.add_argument("--code", required=True, help="A 股 6 位裸码（如 000002 / 600519）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--width-k", type=int, default=DEFAULT_WIDTH_K, help="最近多少根 K 线")
    parser.add_argument(
        "--backend",
        choices=BACKEND_CHOICES,
        default=DEFAULT_BACKEND,
        help="缠论后端：auto（默认）/ czsc / native",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _serve(args.code, args.host, args.port, args.width_k, resolve_backend(args.backend))


if __name__ == "__main__":
    main()
