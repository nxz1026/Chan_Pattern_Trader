"""加密数据源：czsc ``ccxt_connector`` 所依赖的 ccxt 公开端点（R17）。

## 定位

现有主通道是自研的 ``cpt/adapters/binance_futures.py``（零第三方依赖，只打
Binance USDT-M 的 ``/fapi/v1/klines``）。本模块加的是**第二个加密通道**：
走 ccxt 的统一交易所接口，好处是换所/换市场（现货 vs 永续）只改一个字符串。

## 为什么是可选依赖

``ccxt`` 不在核心 ``dependencies``（该列表保持为空）。本模块的导入全部在函数
内部，未安装时抛 :class:`CcxtNotInstalledError`，由 ``source_registry`` 把它
登记成 ``unavailable`` 而不是让进程起不来。

## 与 czsc 的关系

czsc 自带 ``czsc/connectors/ccxt_connector.py``，但它 ① 顶层 ``import ccxt``
（缺依赖时连 czsc 都导入失败）② 依赖 ``loguru`` ③ 带 ``@czsc.disk_cache``
装饰器与 ``pandas`` DataFrame 返回。CPT 需要的是"请求 → CanonicalBar"的窄接口，
所以这里直接用 ccxt，**不**转手 czsc 的连接器（照抄它的 ``binanceusdm`` /
``binance`` 选所与代理约定即可）。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from cpt.domain.models import CanonicalBar

__all__ = [
    "DEFAULT_EXCHANGE",
    "EXCHANGE_IDS",
    "CcxtKlineClient",
    "CcxtNotInstalledError",
    "CcxtSourceError",
]

#: 交易所别名 → ccxt 的交易所 id。与 czsc 的"币安期货/币安现货"一一对应。
EXCHANGE_IDS: Final[Mapping[str, str]] = {
    "binanceusdm": "binanceusdm",
    "binance": "binance",
    "币安期货": "binanceusdm",
    "币安现货": "binance",
}
DEFAULT_EXCHANGE: Final[str] = "binanceusdm"
#: 各交易所支持的周期串（ccxt 用 1m/5m/1h/1d 这套）。
SUPPORTED_TIMEFRAMES: Final[tuple[str, ...]] = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
MAX_LIMIT: Final[int] = 1000


class CcxtNotInstalledError(RuntimeError):
    """未安装可选依赖 ccxt（``pip install -e ".[crypto]"``）。"""


class CcxtSourceError(RuntimeError):
    """ccxt 请求或响应解析失败。"""


def _import_ccxt() -> Any:
    try:
        import ccxt  # noqa: PLC0415 (延迟导入是设计的一部分)
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于运行环境
        raise CcxtNotInstalledError(
            '加密 ccxt 通道需要可选依赖 ccxt，请安装：pip install -e ".[crypto]"'
        ) from exc
    return ccxt


def _proxies_from_env() -> dict[str, str] | None:
    """沿用 czsc 连接器的代理约定：``USE_PROXY=1`` 时读 ``HTTP(S)_PROXY``。"""
    if os.getenv("USE_PROXY", "0") != "1":
        return None
    return {
        "http": os.getenv("HTTP_PROXY", "http://127.0.0.1:10808"),
        "https": os.getenv("HTTPS_PROXY", "http://127.0.0.1:10808"),
    }


def _as_float(value: object, *, field: str) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise CcxtSourceError(f"ccxt 字段 {field} 不是数字：{value!r}") from exc


def parse_ohlcv_rows(
    rows: Sequence[Sequence[Any]],
    *,
    interval_ms: int,
    now_ms: int,
) -> tuple[CanonicalBar, ...]:
    """把 ccxt 的 ``[[ts, o, h, l, c, v], ...]`` 转成 ``CanonicalBar``。

    ccxt 的 ``timestamp`` 是**该根 K 线的开盘时间（毫秒）**，与 CPT 的
    ``open_time`` 同义，所以不需要换算时区。``is_closed`` 由
    ``open_time + interval_ms <= now_ms`` 判定（最后一根未收盘）。
    """
    bars: list[CanonicalBar] = []
    for row in rows:
        if not isinstance(row, Sequence) or len(row) < 6:
            raise CcxtSourceError(f"ccxt OHLCV 行字段不足：{row!r}")
        open_time = int(_as_float(row[0], field="timestamp"))
        bars.append(
            CanonicalBar(
                open_time=open_time,
                open=_as_float(row[1], field="open"),
                high=_as_float(row[2], field="high"),
                low=_as_float(row[3], field="low"),
                close=_as_float(row[4], field="close"),
                volume=_as_float(row[5], field="volume"),
                close_time=open_time + interval_ms - 1,
                quote_volume=0.0,
                trade_count=0,
                taker_buy_base_volume=0.0,
                taker_buy_quote_volume=0.0,
                is_closed=open_time + interval_ms <= now_ms,
            )
        )
    bars.sort(key=lambda bar: bar.open_time)
    return tuple(bars)


class CcxtKlineClient:
    """ccxt K 线客户端（公开端点，无鉴权）。

    Args:
        exchange: 见 :data:`EXCHANGE_IDS`（``binanceusdm`` / ``binance`` / 中文别名）。
        timeout: 单次请求超时（毫秒，ccxt 自己的单位）。
        factory: 注入式交易所工厂 ``(exchange_id, config) -> exchange``，测试用。
        now_ms: 注入式时钟。
    """

    def __init__(
        self,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        timeout: int = 20_000,
        factory: Callable[[str, Mapping[str, Any]], Any] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        exchange_id = EXCHANGE_IDS.get(
            str(exchange).strip().lower(), EXCHANGE_IDS.get(str(exchange).strip())
        )
        if exchange_id is None:
            raise CcxtSourceError(
                f"不支持的交易所：{exchange!r}（可选 {sorted(set(EXCHANGE_IDS))}）"
            )
        self._exchange_id = exchange_id
        self._timeout = timeout
        self._factory = factory
        self._now_ms = now_ms

    @property
    def exchange_id(self) -> str:
        """ccxt 交易所 id。"""
        return self._exchange_id

    def _exchange(self) -> Any:
        config: dict[str, Any] = {"enableRateLimit": True, "timeout": self._timeout}
        proxies = _proxies_from_env()
        if proxies is not None:
            config["proxies"] = proxies
        if self._factory is not None:
            return self._factory(self._exchange_id, config)
        ccxt = _import_ccxt()
        return getattr(ccxt, self._exchange_id)(config)

    def fetch_klines(
        self,
        symbol: str,
        interval: str = "1h",
        *,
        limit: int = 200,
        interval_ms: int | None = None,
    ) -> tuple[CanonicalBar, ...]:
        """拉取一段 K 线（不做连续性校验）。

        Args:
            symbol: ccxt 统一符号，如 ``"BTC/USDT:USDT"``（永续）或 ``"BTC/USDT"``。
            interval: 见 :data:`SUPPORTED_TIMEFRAMES`。
            limit: 1..:data:`MAX_LIMIT`。
            interval_ms: 该周期的毫秒数，用于 ``is_closed``；``None`` 表示全部按已收盘处理。

        Raises:
            ValueError: 参数越界。
            CcxtNotInstalledError: 未安装 ccxt。
            CcxtSourceError: 请求失败或响应结构不符。
        """
        if not symbol:
            raise ValueError("symbol 不能为空")
        if interval not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"不支持的周期：{interval!r}（可选 {SUPPORTED_TIMEFRAMES}）")
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit 必须在 1..{MAX_LIMIT}，实际 {limit}")

        exchange = self._exchange()
        try:
            rows = exchange.fetch_ohlcv(symbol, timeframe=interval, limit=limit)
        except CcxtNotInstalledError:
            raise
        except Exception as exc:  # noqa: BLE001 — ccxt 的异常族很宽（NetworkError/ExchangeError/...）
            raise CcxtSourceError(
                f"ccxt fetch_ohlcv 失败 {self._exchange_id} {symbol}: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(rows, Sequence) or not rows:
            raise CcxtSourceError(f"ccxt 返回空 K 线：{self._exchange_id} {symbol}")
        now_ms = self._now_ms() if self._now_ms is not None else _system_now_ms()
        return parse_ohlcv_rows(rows, interval_ms=interval_ms or 0, now_ms=now_ms)

    def probe(self) -> dict[str, Any]:
        """轻量探活：``fetch_time()``（不打 K 线、不消耗行情权重）。

        Returns:
            ``{"exchange": id, "server_time_ms": int}``。
        """
        exchange = self._exchange()
        try:
            server_time = exchange.fetch_time()
        except CcxtNotInstalledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CcxtSourceError(
                f"ccxt fetch_time 失败 {self._exchange_id}: {type(exc).__name__}: {exc}"
            ) from exc
        return {"exchange": self._exchange_id, "server_time_ms": int(server_time)}


def _system_now_ms() -> int:
    import time  # noqa: PLC0415

    return int(time.time() * 1000)
