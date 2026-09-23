"""Binance USDT-M 永续合约 K 线 HTTP 适配层（纯标准库，零第三方依赖）。

本模块是 CPT 与 Binance Futures REST（``GET /fapi/v1/klines``）之间唯一的
IO 边界：把交易所返回的**二维数组**翻译成不可变的领域对象
:class:`~cpt.domain.models.CanonicalBar`。``urllib`` 之外不引入任何包，
不导入 ``cpt.domain`` 以外的上层模块。

字段映射（``docs/rules.md`` §8.5，Binance 数组下标固定 12 列）：

===== ============================ ===============================
下标  Binance 字段                  映射到
===== ============================ ===============================
0     ``openTime``（ms）            ``CanonicalBar.open_time``
1     ``open``                     ``CanonicalBar.open``
2     ``high``                     ``CanonicalBar.high``
3     ``low``                      ``CanonicalBar.low``
4     ``close``                    ``CanonicalBar.close``
5     ``volume``（base）            ``CanonicalBar.volume``
6     ``closeTime``（ms）           ``CanonicalBar.close_time``
7     ``quoteAssetVolume``         ``CanonicalBar.quote_volume``
8     ``numberOfTrades``           ``CanonicalBar.trade_count``
9     ``takerBuyBaseAssetVolume``  ``CanonicalBar.taker_buy_base_volume``
10    ``takerBuyQuoteAssetVolume`` ``CanonicalBar.taker_buy_quote_volume``
11    忽略列（文档标注 ``ignore``） 不映射
===== ============================ ===============================

设计约束：

* **可注入 IO**：网络调用经 ``opener(url, timeout) -> bytes``。默认实现是
  ``urllib.request.urlopen`` 的薄包装；测试注入假 opener 即可**完全不触网**
  （本模块自测禁止真实网络请求）。
* **可注入时钟**：``is_closed`` 由 ``close_time <= now_ms`` 判定；``now_ms``
  是可注入的 ``Callable[[], int]``，测试无需等待真实时间流逝。
* **失败即异常**：HTTP 层错误、非 JSON 响应、字段缺失/类型错误一律抛
  :class:`BinanceDataError`，绝不返回半截数据。
* **校验分层**：``fetch_klines`` 只管字段映射；连续性/缺口校验交给
  :mod:`cpt.adapters.validators`（``fetch_validated_klines`` 组合二者），
  避免在适配层重复实现已冻结的规则。

用法（真实调用）：

.. code-block:: python

    from cpt.adapters.binance_futures import BinanceFuturesClient

    client = BinanceFuturesClient()
    bars = client.fetch_validated_klines("BTCUSDT", "5m", limit=1500)
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final

from cpt.adapters.validators import validate_canonical_bars
from cpt.domain.models import CanonicalBar

__all__ = [
    "BINANCE_FAPI_BASE_URL",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "INTERVAL_MS",
    "KLINES_PATH",
    "MAX_KLINES_LIMIT",
    "BinanceDataError",
    "BinanceFuturesClient",
    "Opener",
    "resolve_interval_ms",
]

#: Binance USDT-M 合约 REST 根地址。
BINANCE_FAPI_BASE_URL: Final[str] = "https://fapi.binance.com"

#: K 线端点路径（``GET``，query 参数：symbol/interval/startTime/endTime/limit）。
KLINES_PATH: Final[str] = "/fapi/v1/klines"

#: 单次请求允许的最大 K 线根数（Binance 硬上限，实测与文档一致）。
MAX_KLINES_LIMIT: Final[int] = 1500

#: 默认 HTTP 超时（秒）。
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 10.0

#: 固定长度周期的毫秒数。**不含** ``1M``：自然月长度不固定，无法给出唯一毫秒
#: 周期，调用方须显式传 ``interval_ms``（见 :func:`resolve_interval_ms`）。
INTERVAL_MS: Final[Mapping[str, int]] = MappingProxyType(
    {
        "1s": 1_000,
        "1m": 60_000,
        "3m": 180_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "2h": 7_200_000,
        "4h": 14_400_000,
        "6h": 21_600_000,
        "8h": 28_800_000,
        "12h": 43_200_000,
        "1d": 86_400_000,
        "3d": 259_200_000,
        "1w": 604_800_000,
    }
)

#: 注入式 HTTP 取值器：(完整 URL, 超时秒) -> 原始响应体字节（UTF-8 JSON）。
Opener = Callable[[str, float], bytes]

#: Binance 数组列下标（名义长度 12，第 11 列为 ``ignore``）。
_OPEN_TIME_INDEX: Final[int] = 0
_OPEN_INDEX: Final[int] = 1
_HIGH_INDEX: Final[int] = 2
_LOW_INDEX: Final[int] = 3
_CLOSE_INDEX: Final[int] = 4
_VOLUME_INDEX: Final[int] = 5
_CLOSE_TIME_INDEX: Final[int] = 6
_QUOTE_VOLUME_INDEX: Final[int] = 7
_TRADE_COUNT_INDEX: Final[int] = 8
_TAKER_BUY_BASE_INDEX: Final[int] = 9
_TAKER_BUY_QUOTE_INDEX: Final[int] = 10

#: 一行 K 线至少要有 11 列（第 12 列 ``ignore`` 不参与映射，缺列即视为损坏）。
_MIN_ROW_LENGTH: Final[int] = 11

#: 响应体读取上限，防止异常端点把内存打满（正常 1500 根 ≈ 250 KiB）。
_MAX_RESPONSE_BYTES: Final[int] = 16 * 1024 * 1024


class BinanceDataError(RuntimeError):
    """Binance 数据获取/解析失败。

    触发场景（全部为本异常，便于调用方一处兜底）：

    * HTTP 层失败：DNS/连接/超时（``OSError``）或非 2xx 状态码
      （``urllib.error.HTTPError``）；
    * 响应不是合法 JSON，或 Binance 以 ``{"code": ..., "msg": ...}``
      形式回报业务错误（例如 ``-1121 Invalid symbol``）；
    * 响应结构不是 K 线二维数组，或某行长度不足 11、字段无法转为数字、
      数值非法（``CanonicalBar`` 自身值域校验拒绝，如 ``high < low``）。

    与之相对，**参数错误**（``symbol``/``interval`` 为空、``limit`` 越界、
    时间区间倒置）抛 ``ValueError``：那是调用方的编程错误，不是数据问题。
    """


def resolve_interval_ms(interval: str) -> int:
    """把 Binance 周期字符串解析为固定毫秒数。

    Args:
        interval: 形如 ``"5m"`` / ``"1h"`` / ``"1d"`` 的 Binance 周期串。

    Returns:
        该周期的毫秒数。

    Raises:
        ValueError: ``interval`` 不在 :data:`INTERVAL_MS` 中（含无固定长度的
            ``"1M"``）。调用方需对 ``"1M"`` 等周期显式传 ``interval_ms``。
    """
    try:
        return INTERVAL_MS[interval]
    except KeyError as exc:
        supported = ", ".join(sorted(INTERVAL_MS))
        raise ValueError(
            f"不支持的 interval={interval!r}; 支持固定长度周期: {supported}; "
            "区间不固定（如 '1M'）请显式传 interval_ms"
        ) from exc


def _urlopen_bytes(url: str, timeout: float) -> bytes:
    """默认 opener：``urllib`` 取回响应体字节。

    只做「取字节」一件事，状态码判定交给 ``urlopen`` 自身的 ``HTTPError``。
    """
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        data: bytes = response.read(_MAX_RESPONSE_BYTES)
        return data


def _default_now_ms() -> int:
    """默认时钟：当前 Unix 毫秒。"""
    return int(time.time() * 1000)


def _build_query(
    symbol: str,
    interval: str,
    start_time: int | None,
    end_time: int | None,
    limit: int,
) -> dict[str, object]:
    """校验请求参数并构造 query 参数表。

    Raises:
        ValueError: ``symbol``/``interval`` 为空、``limit`` 不在
            ``1..MAX_KLINES_LIMIT``、时间为负或 ``start_time >= end_time``。
    """
    if not symbol:
        raise ValueError("symbol 不能为空")
    if not interval:
        raise ValueError("interval 不能为空")
    if limit < 1 or limit > MAX_KLINES_LIMIT:
        raise ValueError(f"limit 必须在 1..{MAX_KLINES_LIMIT}, 实测 {limit}")
    for name, value in (("start_time", start_time), ("end_time", end_time)):
        if value is not None and value < 0:
            raise ValueError(f"{name} 必须为非负 Unix 毫秒, 实测 {value}")
    if start_time is not None and end_time is not None and start_time >= end_time:
        raise ValueError(f"start_time({start_time}) 必须严格小于 end_time({end_time})")
    params: dict[str, object] = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_time is not None:
        params["startTime"] = start_time
    if end_time is not None:
        params["endTime"] = end_time
    return params


def _as_float(value: object, *, field: str, index: int) -> float:
    """把 Binance 的字符串/数值列转为有限 ``float``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BinanceDataError(f"K线[{index}] 字段 {field} 期望数值, 实测 {value!r}")
    try:
        result = float(value)
    except ValueError as exc:
        raise BinanceDataError(f"K线[{index}] 字段 {field} 无法解析为数值: {value!r}") from exc
    if not math.isfinite(result):
        raise BinanceDataError(f"K线[{index}] 字段 {field} 非有限数值: {value!r}")
    return result


def _as_int(value: object, *, field: str, index: int) -> int:
    """把 Binance 的字符串/整数列转为 ``int``（带小数的 float 视为损坏）。"""
    if isinstance(value, bool):
        raise BinanceDataError(f"K线[{index}] 字段 {field} 期望整数, 实测 {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise BinanceDataError(f"K线[{index}] 字段 {field} 期望整数, 实测 {value!r}")
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise BinanceDataError(f"K线[{index}] 字段 {field} 无法解析为整数: {value!r}") from exc
    raise BinanceDataError(f"K线[{index}] 字段 {field} 期望整数, 实测 {value!r}")


def _parse_row(row: object, index: int, now_ms: int) -> CanonicalBar:
    """把一行 Binance K 线数组映射为 :class:`CanonicalBar`。

    Raises:
        BinanceDataError: 行不是数组、列数不足 11、字段类型非法，或
            ``CanonicalBar`` 自身值域校验（OHLC/时间字段）失败。
    """
    if not isinstance(row, (list, tuple)):
        raise BinanceDataError(f"K线[{index}] 期望数组, 实测 {type(row).__name__}")
    if len(row) < _MIN_ROW_LENGTH:
        raise BinanceDataError(f"K线[{index}] 至少需要 {_MIN_ROW_LENGTH} 列, 实测 {len(row)}")
    open_time = _as_int(row[_OPEN_TIME_INDEX], field="openTime", index=index)
    close_time = _as_int(row[_CLOSE_TIME_INDEX], field="closeTime", index=index)
    try:
        return CanonicalBar(
            open_time=open_time,
            open=_as_float(row[_OPEN_INDEX], field="open", index=index),
            high=_as_float(row[_HIGH_INDEX], field="high", index=index),
            low=_as_float(row[_LOW_INDEX], field="low", index=index),
            close=_as_float(row[_CLOSE_INDEX], field="close", index=index),
            volume=_as_float(row[_VOLUME_INDEX], field="volume", index=index),
            close_time=close_time,
            quote_volume=_as_float(row[_QUOTE_VOLUME_INDEX], field="quoteVolume", index=index),
            trade_count=_as_int(row[_TRADE_COUNT_INDEX], field="numberOfTrades", index=index),
            taker_buy_base_volume=_as_float(
                row[_TAKER_BUY_BASE_INDEX], field="takerBuyBaseAssetVolume", index=index
            ),
            taker_buy_quote_volume=_as_float(
                row[_TAKER_BUY_QUOTE_INDEX], field="takerBuyQuoteAssetVolume", index=index
            ),
            is_closed=close_time <= now_ms,
        )
    except ValueError as exc:
        raise BinanceDataError(f"K线[{index}] 字段非法: {exc}") from exc


def _parse_klines(payload: object, now_ms: int) -> tuple[CanonicalBar, ...]:
    """解析 ``/fapi/v1/klines`` 响应体为 ``CanonicalBar`` 元组。

    Raises:
        BinanceDataError: 响应为 Binance 错误对象、顶层不是数组，或任一行解析失败。
    """
    if isinstance(payload, Mapping):
        code = payload.get("code")
        msg = payload.get("msg")
        raise BinanceDataError(f"Binance 返回业务错误: code={code!r} msg={msg!r}")
    if not isinstance(payload, list):
        raise BinanceDataError(f"响应期望 K 线数组, 实测 {type(payload).__name__}")
    return tuple(_parse_row(row, index, now_ms) for index, row in enumerate(payload))


class BinanceFuturesClient:
    """Binance USDT-M 永续 K 线客户端（窄接口：两个读方法）。

    该类只做「请求 → 解析 → 领域对象」的一次转换，不持有状态、不做重试、
    不做缓存、不落盘：重试/分页/持久化属于上层（``cpt.application`` /
    ``cpt.storage``）的职责。

    Args:
        base_url: REST 根地址，默认 :data:`BINANCE_FAPI_BASE_URL`；测试可指向
            本地假端点（真实网络测试被禁止，见模块 docstring）。
        timeout: 单次 HTTP 超时（秒）。
        opener: 注入式取值器 ``(url, timeout) -> bytes``；默认 ``urlopen``。
        now_ms: 注入式时钟 ``() -> int``（Unix 毫秒），决定 ``is_closed``；
            默认系统时间。
    """

    def __init__(
        self,
        *,
        base_url: str = BINANCE_FAPI_BASE_URL,
        timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        opener: Opener | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener: Opener = opener if opener is not None else _urlopen_bytes
        self._now_ms: Callable[[], int] = now_ms if now_ms is not None else _default_now_ms

    @property
    def base_url(self) -> str:
        """当前使用的 REST 根地址。"""
        return self._base_url

    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = MAX_KLINES_LIMIT,
    ) -> tuple[CanonicalBar, ...]:
        """拉取并解析一段 K 线（**不做**连续性/缺口校验）。

        Args:
            symbol: 交易对，如 ``"BTCUSDT"``。
            interval: Binance 周期串，如 ``"5m"``。
            start_time: 起始 ``open_time``（Unix 毫秒，闭区间），``None`` 不限。
            end_time: 结束时刻（Unix 毫秒），``None`` 不限。
            limit: 返回根数上限，必须在 ``1..1500``。

        Returns:
            按响应顺序排列的 ``CanonicalBar`` 元组；每根的 ``is_closed`` 由
            ``close_time <= now_ms`` 判定。

        Raises:
            ValueError: ``symbol``/``interval`` 为空、``limit`` 越界、时间为负或
                ``start_time >= end_time``。
            BinanceDataError: HTTP/JSON/字段任一环节失败。
        """
        params = _build_query(symbol, interval, start_time, end_time, limit)
        payload = self._get_json(KLINES_PATH, params)
        return _parse_klines(payload, self._now_ms())

    def fetch_validated_klines(
        self,
        symbol: str,
        interval: str,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = MAX_KLINES_LIMIT,
        *,
        interval_ms: int | None = None,
    ) -> tuple[CanonicalBar, ...]:
        """``fetch_klines`` + :func:`~cpt.adapters.validators.validate_canonical_bars`。

        用于「结构计算前的守卫」：确认这一批数据严格递增、无缺口后再交给
        分型/笔/中枢算法。

        Args:
            symbol: 交易对。
            interval: Binance 周期串；``interval_ms`` 为 ``None`` 时用它解析周期。
            start_time: 起始 ``open_time``（Unix 毫秒），``None`` 不限。
            end_time: 结束时刻（Unix 毫秒），``None`` 不限。
            limit: 返回根数上限，必须在 ``1..1500``。
            interval_ms: 校验用契约周期（毫秒）；``"1M"`` 等无固定长度的周期
                必须显式传入。

        Returns:
            去重、严格递增、周期连续的 ``CanonicalBar`` 元组。

        Raises:
            ValueError: 参数非法，或 ``interval`` 无法解析为固定毫秒周期。
            BinanceDataError: HTTP/JSON/字段任一环节失败。
            DataValidationError: 数据自身不合法（乱序、冲突重复、OHLC 非法等）。
            DataGapError: 序列存在缺口。
        """
        ms = resolve_interval_ms(interval) if interval_ms is None else interval_ms
        return validate_canonical_bars(
            self.fetch_klines(symbol, interval, start_time, end_time, limit),
            ms,
        )

    def _get_json(self, path: str, params: Mapping[str, object]) -> Any:
        """发起 GET 并返回解析后的 JSON（HTTP 状态码与 JSON 语法都严格判定）。"""
        url = f"{self._base_url}{path}?{urllib.parse.urlencode(params)}"
        raw = self._open(url)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = raw[:200]
            raise BinanceDataError(f"响应不是合法 UTF-8 JSON: {preview!r}") from exc

    def _open(self, url: str) -> bytes:
        """调用注入的 opener，把底层网络/HTTP 失败统一转成 :class:`BinanceDataError`。"""
        try:
            return self._opener(url, self._timeout)
        except urllib.error.HTTPError as exc:
            raise BinanceDataError(
                f"HTTP {exc.code} {exc.reason} 请求失败: {url} {_http_error_detail(exc)}"
            ) from exc
        except OSError as exc:  # URLError / 超时 / DNS / 连接被拒
            raise BinanceDataError(f"网络请求失败: {url} {exc}") from exc


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """尽力从错误响应体里提取 Binance 的 ``code``/``msg``（读取失败时降级为空串）。"""
    try:
        body = exc.read()
    except OSError:
        return ""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"body={body[:200]!r}"
    if isinstance(parsed, Mapping):
        return f"code={parsed.get('code')!r} msg={parsed.get('msg')!r}"
    return f"body={body[:200]!r}"
