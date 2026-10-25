"""A 股公开源兜底适配器（R17）：腾讯日线（后复权）+ 新浪快照。

## 为什么需要它

本机出网 IP 在大阪（Oracle Cloud）。R17 侦察实测（每个源 1 次请求）：

| 源 | 结果 |
|---|---|
| `hq.sinajs.cn`（新浪快照） | **200** |
| `qt.gtimg.cn`（腾讯快照） | **200** |
| `web.ifzq.gtimg.cn`（腾讯复权日线） | **200** |
| `push2.eastmoney.com`（东财） | **502** |
| Yahoo `600519.SS` | 200（曾 429） |
| pytdx `get_security_bars` | 4 台服务器全 **0 行** |

⇒ 主通道仍是 Wind（额度受限），**腾讯日线是唯一可用的免鉴权 K 线兜底**，
新浪只用于"最新报价"级别的探活。

## 三个必须写进代码的坑

1. **腾讯 `fqkline` 的字段顺序是 `[日期, 开, 收, 高, 低, 量]`** —— 不是 OHLC！
   实测 `["2026-09-24","8838.081","8764.863","8872.523","8731.378","31239.000"]`
   对应 开 8838.081 / 收 8764.863 / 高 8872.523 / 低 8731.378（当天跌，开>收）。
   按 OHLC 顺序解析会得到"最高价 < 收盘价"的坏数据，且**不会报错**。
2. **复权口径必须与本地库一致**：`cpt/adapters/a_share_local.py` 用的是
   `public.daily_bar × asel.ref_adjust_factor` 的**后复权**价，所以这里请求
   `hfq`（实测同一天后复权 8838.081 vs 不复权 1250.010，因子 ≈ 7.07）。
   口径不一致会让"兜底源"和"主源"画出的笔完全不同。
3. **时间字段口径**：与本地库一致 —— `open_time = 当日 00:00 UTC`、
   `close_time = open_time + 86400000 - 1`、`is_closed=True`。

模块**只做**「请求 → 解析 → CanonicalBar」，不重试、不缓存、不落盘（与
``cpt/adapters/binance_futures.py`` 同款窄接口）。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote

from cpt.domain.models import CanonicalBar

__all__ = [
    "AShareAdjustUnsupportedError",
    "ASharePublicError",
    "DAILY_INTERVAL_MS",
    "SINA_BATCH_LIMIT",
    "SINA_QUOTE_URL",
    "TENCENT_KLINE_URL",
    "SinaQuoteClient",
    "TencentKlineClient",
    "normalize_code",
    "parse_sina_quotes",
]

DAILY_INTERVAL_MS: Final[int] = 86_400_000

#: 新浪一次 ``list=`` 最多带多少只（R82）。实测 3 只、30 只都正常返回；
#: 定 50 是为了「一次请求覆盖一个概念板块或一个自定义池」，同时留出余量：
#: URL 长度 = 前缀 + 8×n，50 只约 430 字符，离各家网关的 2K~8K 上限很远。
#: 超过这个数由 ``fetch_quotes`` 自己切块，**不会**让调用方拿到半截结果。
SINA_BATCH_LIMIT: Final[int] = 50

#: 新浪快照 ``hq_str_<symbol>="..."`` 一行一只；用 ``finditer`` 而不是
#: ``re.search`` 是为了批量响应里能逐行取出（单只响应只有一行，行为不变）。
_SINA_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r'var\s+hq_str_(?P<symbol>[a-z]{2}\d{6})="(?P<body>[^"]*)"'
)

#: 五档在 34 字段里的下标（R82 实测 2026-10-21）：
#: ``[10]=买一量 [11]=买一价 ... [18]=买五量 [19]=买五价``、
#: ``[20]=卖一量 [21]=卖一价 ... [28]=卖五量 [29]=卖五价``。
#: **量与价的先后是反直觉的**（先量后价），照 ``[10]=价`` 解析会得到
#: 「买一价 200 元、买一量 1309.590 手」这种自洽但错误的数 —— 所以下面
#: 按 ``(量, 价)`` 成对取值，并把两档都写进 docstring。
_SINA_BID_LEVELS: Final[int] = 5
_SINA_ASK_LEVELS: Final[int] = 5
TENCENT_KLINE_URL: Final[str] = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_QUOTE_URL: Final[str] = "https://hq.sinajs.cn/list="
DEFAULT_HTTP_TIMEOUT_SECONDS: Final[float] = 12.0
#: 腾讯 fqkline 单次返回上限（实测 800 行仍可用，取 800 留安全边界）。
MAX_DAILY_BARS: Final[int] = 800
#: 复权口径 → 响应里的数组键名。``bfq``（不复权）的键是 ``day``。
_ADJUST_KEYS: Final[Mapping[str, str]] = {
    "hfq": "hfqday",
    "qfq": "qfqday",
    "bfq": "day",
}

Opener = Callable[[str, float, Mapping[str, str] | None], bytes]

_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<market>sh|sz|bj)?(?P<digits>\d{6})$")


class ASharePublicError(RuntimeError):
    """公开源不可用 / 响应格式不符（不静默降级，由调用方决定兜底）。"""


class AShareAdjustUnsupportedError(ASharePublicError):
    """腾讯对该**标的**不提供请求的复权序列（HTTP 200 但无 ``hfqday`` 键）。

    单独成类是因为这是**逐标的**属性、不是板块属性，且**无法预测**：实测
    688111/688036 有 hfq 而 688981（中芯国际，800 根 raw）没有；多数 301 有 hfq
    而近期新股没有。唯一可靠的做法是问一次、然后如实报告 —— 见
    ``docs/progress-log.md`` R15-1 节：这里先后错过两次（先记成"501"，再记成
    "按板块不支持"），任何前缀规则都会重犯。
    """


def normalize_code(code: str) -> str:
    """把 ``600519`` / ``600519.SH`` / ``sh600519`` 统一成腾讯要的 ``sh600519``。

    交易所前缀按首位数字推断（沪 6/9/5，深 0/3/2/1，北 4/8/92/43/83/87/88）。
    """
    raw = str(code).strip().lower()
    if "." in raw:
        body, _, suffix = raw.partition(".")
        suffix = suffix.lower()
        if suffix not in {"sh", "sz", "bj"}:
            # 未知后缀一律拒绝：静默丢掉 `.XX` 会去拉另一家交易所的同名代码。
            raise ASharePublicError(f"无法识别的交易所后缀：{code!r}（可选 SH / SZ / BJ）")
        raw = f"{suffix}{body}"
    match = _CODE_RE.match(raw)
    if match is None:
        raise ASharePublicError(
            f"无法识别的 A 股代码：{code!r}（期望 600519 / 600519.SH / sh600519）"
        )
    market = match.group("market")
    digits = match.group("digits")
    if market is None:
        # 顺序敏感：`92`（北交所新代码段）必须在 `9`（沪 B）之前判断，否则
        # 920025 会被推成 sh920025 —— `a_share_local._to_wind_code` 上真实存在过
        # 这个先后顺序 bug，R17 一并修掉；两处现在都有回归用例
        # （tests/test_a_share_local.py 的 `_to_wind_code` 参数化用例、
        # tests/test_factor_backfill_script.py 的 920201/830799 用例）。
        if digits.startswith(("92", "43", "83", "87", "88")):
            market = "bj"
        elif digits[0] in {"6", "9", "5"}:
            market = "sh"
        elif digits[0] in {"0", "3", "2", "1"}:
            market = "sz"
        else:
            raise ASharePublicError(f"无法从代码推断交易所：{code!r}")
    return f"{market}{digits}"


def _urlopen_bytes(url: str, timeout: float, headers: Mapping[str, str] | None = None) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (固定 https 域名)
        return bytes(response.read())


def _as_float(value: object, *, field: str, row: Sequence[Any]) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise ASharePublicError(f"字段 {field} 不是数字：{value!r}（行 {list(row)!r}）") from exc


def _to_open_ms(day: str) -> int:
    try:
        date = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ASharePublicError(f"日期格式不符：{day!r}（期望 YYYY-MM-DD）") from exc
    return int(datetime(date.year, date.month, date.day, tzinfo=UTC).timestamp() * 1000)


class TencentKlineClient:
    """腾讯 ``fqkline`` 日线客户端（后复权，免鉴权）。

    Args:
        timeout: 单次 HTTP 超时（秒）。
        adjust: ``"hfq"``（默认，与本地库口径一致）/ ``"qfq"`` / ``"bfq"``。
        opener: 注入式取值器 ``(url, timeout, headers) -> bytes``，测试用。
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        adjust: str = "hfq",
        opener: Opener | None = None,
    ) -> None:
        if adjust not in _ADJUST_KEYS:
            raise ASharePublicError(f"不支持的复权口径：{adjust!r}（可选 {sorted(_ADJUST_KEYS)}）")
        self._timeout = timeout
        self._adjust = adjust
        self._opener: Opener = opener if opener is not None else _urlopen_bytes

    @property
    def adjust(self) -> str:
        """当前复权口径。"""
        return self._adjust

    def fetch_daily_bars(self, code: str, *, limit: int = 180) -> tuple[CanonicalBar, ...]:
        """拉取最近 ``limit`` 根**后复权日线**。

        Args:
            code: ``600519`` / ``600519.SH`` / ``sh600519``。
            limit: 1..:data:`MAX_DAILY_BARS`。

        Returns:
            按日期升序的 ``CanonicalBar`` 元组（``is_closed=True``）。

        Raises:
            ValueError: ``limit`` 越界。
            ASharePublicError: 代码非法 / HTTP 失败 / JSON 结构不符 / 无数据。
        """
        if not 1 <= limit <= MAX_DAILY_BARS:
            raise ValueError(f"limit 必须在 1..{MAX_DAILY_BARS}，实际 {limit}")
        symbol = normalize_code(code)
        param = quote(f"{symbol},day,,,{limit},{self._adjust}", safe=",")
        url = f"{TENCENT_KLINE_URL}?param={param}"
        try:
            raw = self._opener(url, self._timeout, None)
        except urllib.error.HTTPError as exc:
            raise ASharePublicError(f"腾讯日线 HTTP {exc.code}：{symbol}") from exc
        except urllib.error.URLError as exc:
            raise ASharePublicError(f"腾讯日线不可达：{symbol}（{exc.reason}）") from exc
        return parse_tencent_kline(raw, symbol=symbol, adjust=self._adjust)


def parse_tencent_kline(
    raw: bytes | str, *, symbol: str, adjust: str = "hfq"
) -> tuple[CanonicalBar, ...]:
    """解析腾讯 ``fqkline`` 响应（**字段顺序见模块 docstring 坑 1**）。"""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ASharePublicError(f"腾讯日线返回非 JSON：{text[:120]!r}") from exc
    if not isinstance(payload, Mapping):
        raise ASharePublicError("腾讯日线返回结构不是对象")
    if payload.get("code") not in (0, "0", None):
        raise ASharePublicError(f"腾讯日线返回错误码 {payload.get('code')}：{payload.get('msg')!r}")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ASharePublicError("腾讯日线返回缺少 data")
    node = data.get(symbol)
    if not isinstance(node, Mapping):
        raise ASharePublicError(f"腾讯日线返回缺少 {symbol} 节点（可能是代码不存在或已退市）")
    key = _ADJUST_KEYS[adjust]
    rows = node.get(key)
    if rows is None and key != "day":
        # 部分标的没有复权数据，腾讯只给 day —— 明确报错而不是拿不复权价冒充后复权。
        # 用专用异常类型：调用方要据此区分"该标的没有"与"网络/解析失败"。
        raise AShareAdjustUnsupportedError(f"{symbol} 无 {key} 数据（腾讯只返回了 {sorted(node)}）")
    if not isinstance(rows, Sequence) or not rows:
        raise ASharePublicError(f"{symbol} 的 {key} 为空")

    bars: list[CanonicalBar] = []
    for row in rows:
        if not isinstance(row, Sequence) or len(row) < 6:
            raise ASharePublicError(f"{symbol} 的 {key} 行字段不足：{row!r}")
        day = str(row[0])
        open_price = _as_float(row[1], field="open", row=row)
        close_price = _as_float(row[2], field="close", row=row)
        high_price = _as_float(row[3], field="high", row=row)
        low_price = _as_float(row[4], field="low", row=row)
        volume = _as_float(row[5], field="volume", row=row)
        if high_price < max(open_price, close_price) or low_price > min(open_price, close_price):
            raise ASharePublicError(
                f"{symbol} {day} OHLC 不自洽"
                f"（开{open_price} 收{close_price} 高{high_price} 低{low_price}）"
                "—— 腾讯 fqkline 的字段顺序是 [日期,开,收,高,低,量]，不是 OHLC"
            )
        open_ms = _to_open_ms(day)
        bars.append(
            CanonicalBar(
                open_time=open_ms,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                close_time=open_ms + DAILY_INTERVAL_MS - 1,
                quote_volume=0.0,
                trade_count=0,
                taker_buy_base_volume=0.0,
                taker_buy_quote_volume=0.0,
                is_closed=True,
            )
        )
    bars.sort(key=lambda bar: bar.open_time)
    return tuple(bars)


class SinaQuoteClient:
    """新浪 ``hq.sinajs.cn`` 快照客户端 —— 只用于探活与"最新报价"级别的兜底。

    **不提供 K 线**：新浪的 K 线接口是另一个域名，本轮实测未纳入兜底范围；
    拿快照伪造日线会污染结构计算，所以本类刻意只暴露快照接口。

    ## R82：为什么加 ``fetch_quotes``（批量 + 五档）

    ADR 0003（emotion-core 盘中触发）最贵的一块是「盘中封板检测要五档」，
    而它原先准备自采。实测发现**同一个响应里五档一直都在**（34 字段），
    只是 ``fetch_quote`` 只解析前 9 个。所以这里补齐解析，让「谁采」这件事
    变成一道口径题而不是一道新建题。

    两条硬约束：

    1. **``fetch_quote`` 的返回字典一个键都不许变** —— 生产路径
       ``a_share_snapshot._attach_dual_compare`` 在读它，改名/加键都会被
       ``check_value_plumbing`` 与快照对账用例抓到。批量版走**另一个方法**。
    2. **缺字段给 ``None``，不给 ``0``** —— 停牌时五档全 0、集合竞价时
       卖一量可能为 0。把「没有这一档」记成 0 手，下游会读成
       「买一挂着 0 手」＝「买盘消失」，方向完全相反。价格 0 同理不认。
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        opener: Opener | None = None,
    ) -> None:
        self._timeout = timeout
        self._opener: Opener = opener if opener is not None else _urlopen_bytes

    def fetch_quote(self, code: str) -> dict[str, Any]:
        """返回 ``{"code","name","open","prev_close","last","high","low","volume","date","time"}``。

        Raises:
            ASharePublicError: HTTP 失败 / 响应体为空 / 字段不足。
        """
        symbol = normalize_code(code)
        # 新浪要求带 Referer，否则返回 403（实测）。
        headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
        try:
            raw = self._opener(f"{SINA_QUOTE_URL}{symbol}", self._timeout, headers)
        except urllib.error.HTTPError as exc:
            raise ASharePublicError(f"新浪快照 HTTP {exc.code}：{symbol}") from exc
        except urllib.error.URLError as exc:
            raise ASharePublicError(f"新浪快照不可达：{symbol}（{exc.reason}）") from exc
        # 新浪返回 GBK 编码的 JS 赋值语句。
        text = raw.decode("gbk", "replace") if isinstance(raw, bytes) else raw
        match = re.search(r'="(?P<body>[^"]*)"', text)
        if match is None or not match.group("body"):
            raise ASharePublicError(f"新浪快照响应为空或格式不符：{text[:120]!r}")
        fields = match.group("body").split(",")
        if len(fields) < 32:
            raise ASharePublicError(f"新浪快照字段不足（{len(fields)}）：{symbol}")
        return {
            "code": symbol,
            "name": fields[0],
            "open": _as_float(fields[1], field="open", row=fields[:6]),
            "prev_close": _as_float(fields[2], field="prev_close", row=fields[:6]),
            "last": _as_float(fields[3], field="last", row=fields[:6]),
            "high": _as_float(fields[4], field="high", row=fields[:6]),
            "low": _as_float(fields[5], field="low", row=fields[:6]),
            "volume": _as_float(fields[8], field="volume", row=fields[:9]),
            "date": fields[30],
            "time": fields[31],
        }

    def fetch_quotes(self, codes: Sequence[str]) -> dict[str, dict[str, Any]]:
        """批量快照，**含五档**。返回 ``{code: quote}``（**键是 6 位纯代码**）。

        键用 ``000002`` 而不是 ``sz000002``：调用方手上是 ``daily_bar.code`` /
        关注列表里的 6 位代码，让它每次自己 ``normalize_code`` 一遍等于把
        一处可错的转换复制到每个调用点（R82 实跑第一版就返回了 ``sh600519``
        这类前缀键，按 6 位 code 取全是 KeyError）。quote 里仍保留
        ``"code": "sz000002"`` 全限定符号，与 ``fetch_quote`` 一致。

        每只的 ``quote`` = ``fetch_quote`` 的全部键 + 下列键（见 ``parse_sina_quotes``）::

            amount        成交额（元，字段 [9]）
            bid1_vol      买一量（股，字段 [10]）  bid1_price  买一价（元，字段 [11]）
            ...           bid5_vol / bid5_price
            ask1_vol      卖一量（股，字段 [20]）  ask1_price  卖一价（元，字段 [21]）
            ...           ask5_vol / ask5_price
            bids / asks   上面十档的成对视图：[{"price": x, "volume": y}, ...]

        **缺的档位是 ``None``，不是 0**（见类 docstring 第 2 条）。价格 <= 0
        的档位整对置 ``None``：新浪在停牌/未开盘时把价格和量一起填 0。

        Args:
            codes: 任意 ``normalize_code`` 能接受的形式；超过 ``SINA_BATCH_LIMIT``
                自动切块，块间不共享请求。

        Raises:
            ASharePublicError: 任一请求 HTTP 失败 / 响应体为空 / 字段不足。
                这里**不吞**：批量采样本就要么全拿到要么报警，静默返回部分结果
                会让「这只票今天没触发」与「这只票没采到」变成同一件事。
        """
        wanted: list[str] = []
        seen: set[str] = set()
        for code in codes:
            symbol = normalize_code(code)
            if symbol not in seen:
                seen.add(symbol)
                wanted.append(symbol)
        if not wanted:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(wanted), SINA_BATCH_LIMIT):
            chunk = wanted[start : start + SINA_BATCH_LIMIT]
            out.update(self._fetch_chunk(chunk))
        return {symbol[2:]: quote for symbol, quote in out.items()}

    def _fetch_chunk(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        """取一块（<= ``SINA_BATCH_LIMIT`` 只），返回本块解析结果。"""
        headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
        url = f"{SINA_QUOTE_URL}{','.join(symbols)}"
        try:
            raw = self._opener(url, self._timeout, headers)
        except urllib.error.HTTPError as exc:
            raise ASharePublicError(f"新浪批量快照 HTTP {exc.code}：{len(symbols)} 只") from exc
        except urllib.error.URLError as exc:
            raise ASharePublicError(
                f"新浪批量快照不可达：{len(symbols)} 只（{exc.reason}）"
            ) from exc
        text = raw.decode("gbk", "replace") if isinstance(raw, bytes) else raw
        return parse_sina_quotes(text, expected=symbols)


def parse_sina_quotes(text: str, *, expected: Sequence[str] = ()) -> dict[str, dict[str, Any]]:
    """把新浪批量快照响应解析成 ``{symbol: quote}``（R82）。

    从 ``fetch_quote`` 里拆出来是为了**能单测每一种脏数据形态**，不必为每种
    情况去构造一个 opener。

    ## 三条真实形态（2026-10-21 实测）

    1. **停牌 / 未上市**：整行是 ``var hq_str_sz000000="";`` —— 空 body。
       **跳过并留在缺失集合里**，不返回 ``{"last": 0}``。
    2. **未知代码**：新浪对不认识的 symbol 直接**不回这一行**（响应里没有它）。
       所以「请求 3 只、返回 2 行」是正常情况，不是解析失败。
    3. **五档全 0**：停牌股的价格与量一起填 0。价格 <= 0 的档位置 ``None``。

    Args:
        text: 已解码的响应正文（GBK 由调用方负责解码）。
        expected: 本次请求的 symbol 列表。给了就在结尾校验「有没有少」——
            少一只意味着上游没认这个代码，属于要报的异常；不给则只解析出现的行。

    Raises:
        ASharePublicError: ``expected`` 非空但响应里一只都没匹配上；或某行
            字段数 < 32（说明响应格式变了，不该静默降级）。
    """
    out: dict[str, dict[str, Any]] = {}
    matched: set[str] = set()
    for match in _SINA_LINE_RE.finditer(text):
        symbol = match.group("symbol").lower()
        body = match.group("body")
        if not body:
            # 空 body = 停牌/未知；记进 matched 但不出现在结果里，见 docstring 第 1 条。
            matched.add(symbol)
            continue
        fields = body.split(",")
        if len(fields) < 32:
            raise ASharePublicError(f"新浪快照字段不足（{len(fields)}）：{symbol}")
        out[symbol] = _parse_sina_row(symbol, fields)
        matched.add(symbol)
    if expected and not (set(expected) & matched):
        raise ASharePublicError(
            f"新浪批量快照一只都没匹配上（请求 {len(expected)} 只，响应 {len(text)} 字符）"
        )
    return out


def _parse_sina_row(symbol: str, fields: Sequence[str]) -> dict[str, Any]:
    """把一行 34 字段解析成 quote 字典（含五档）。"""
    quote: dict[str, Any] = {
        "code": symbol,
        "name": fields[0],
        "open": _as_float(fields[1], field="open", row=fields[:6]),
        "prev_close": _as_float(fields[2], field="prev_close", row=fields[:6]),
        "last": _as_float(fields[3], field="last", row=fields[:6]),
        "high": _as_float(fields[4], field="high", row=fields[:6]),
        "low": _as_float(fields[5], field="low", row=fields[:6]),
        "volume": _as_float(fields[8], field="volume", row=fields[:9]),
        "amount": _as_float(fields[9], field="amount", row=fields[:10]),
        "date": fields[30],
        "time": fields[31],
    }
    bids: list[dict[str, float] | None] = []
    asks: list[dict[str, float] | None] = []
    for level in range(_SINA_BID_LEVELS):
        # 买档：先量后价，[10]=买一量 [11]=买一价
        vol_raw, price_raw = fields[10 + level * 2], fields[11 + level * 2]
        quote[f"bid{level + 1}_vol"] = _sina_vol(vol_raw)
        quote[f"bid{level + 1}_price"] = _sina_price(price_raw)
        bids.append(_sina_level(price_raw, vol_raw))
    for level in range(_SINA_ASK_LEVELS):
        # 卖档：同样先量后价，[20]=卖一量 [21]=卖一价
        vol_raw, price_raw = fields[20 + level * 2], fields[21 + level * 2]
        quote[f"ask{level + 1}_vol"] = _sina_vol(vol_raw)
        quote[f"ask{level + 1}_price"] = _sina_price(price_raw)
        asks.append(_sina_level(price_raw, vol_raw))
    quote["bids"] = bids
    quote["asks"] = asks
    return quote


def _sina_price(raw: object) -> float | None:
    """价格字段：<= 0 或解析不了 → ``None``（不认 0 元这个价）。"""
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _sina_vol(raw: object) -> float | None:
    """量字段：负数或解析不了 → ``None``；**0 保留为 0.0**。

    0 和 None 的区别在这里是关键：集合竞价时买一量可以是货真价实的 0
    （挂着但没量），这与「这一档不存在」是两件事，下游的封单量判据
    要按 ``> 阈值`` 比大小，两者都会落榜，但**记录**必须诚实。
    """
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _sina_level(price_raw: object, vol_raw: object) -> dict[str, float] | None:
    """一档 = 价 + 量；价格缺失则整档为 ``None``。"""
    price = _sina_price(price_raw)
    if price is None:
        return None
    volume = _sina_vol(vol_raw)
    return {"price": price, "volume": volume if volume is not None else 0.0}
