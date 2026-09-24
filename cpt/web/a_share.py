"""A 股 web 适配器（R16）。

``cpt.web.__main__`` 给加密 BTC/ETH 等提供 HTTP dashboard。
本模块**单独**为 A 股日线提供等价 HTTP 端点，复用
``cpt.web.app.make_handler`` 的所有路由（``/api/dashboard/snapshot`` /
``/api/dashboard/health`` 等），snapshot schema v2 100% 兼容。

## 数据流（R17-3 起构造逻辑已迁到 ``cpt.application.a_share_snapshot``）

本模块现在只是**进程外壳**：解析 ``--code/--port`` 并把
``AShareSnapshotProvider`` 交给 ``cpt.web.app.serve_snapshot``。构造逻辑放在
application 层，因为主看板的 ``/api/dashboard/a-share/snapshot`` 路由要复用同一份
实现（留在 web 层会让两个 web 模块互相 import）。

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
from typing import Any

from cpt.adapters.backend_factory import BACKEND_CHOICES, DEFAULT_BACKEND, resolve_backend
from cpt.adapters.reference_chanlun import ChanlunBackend
from cpt.application.a_share_snapshot import (
    DEFAULT_WIDTH_K,
    build_ashare_snapshot,
)
from cpt.domain.config import RulesConfig
from cpt.web.app import serve_snapshot

_LOG = logging.getLogger("cpt.web.a_share")


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
