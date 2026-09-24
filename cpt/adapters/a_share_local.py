"""A 股本地适配器：从 ``public.daily_bar`` × ``asel.ref_adjust_factor`` 读后复权
OHLC，转为 :class:`~cpt.domain.models.CanonicalBar`。

**只读**，绝不写 DB。因子由 ``scripts/factor_backfill.py`` 单独维护（不在 cpt
核心依赖里，避免把 akshare/psycopg 等拖进 ``dependencies = []``）。

## 数据流
- ``public.daily_bar``: ``code`` / ``date`` / OHLC / volume / amount（**不复权**，5,225 只）
- ``asel.ref_adjust_factor``: ``(code, trade_date) → hfq_factor``
- 输出：``CanonicalBar.open = raw_open × factor``，high/low/close 同理；
  ``volume / amount / trade_count / quote_volume`` 保留**不复权**数值（成交量复权
  在缠论里无意义——除权日成交股数会被反复"补偿"，画出来噪声极大；这是行业惯例）。

## 设计取舍
- **DB 连接是可选项**（``AShareLocalClient(conn=None)``）— 让测试与 replay 可注入
  mock，避免拖入 psycopg 到 cpt 核心依赖。
- **缺口处理**：因子缺失的日期**拒绝该日**（不静默用 1.0 填，否则产生假跳空）。
- **停牌**：``public.daily_bar`` 不含停牌日（库是交易日表），所以停牌不需要额外
  处理——只在缺因子时拒绝。
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from cpt.domain.models import CanonicalBar
from cpt.domain.types import BarLike

__all__ = [
    "AShareFetchResult",
    "AShareLocalClient",
    "AShareLocalError",
    "AShareNoDataError",
    "AShareNoFactorError",
    "SecurityName",
    "fetch_security_names",
]

#: ``public.daily_bar`` 实际列名（与 DB schema 对齐）
_DATE_COL: Final[str] = "date"
_CODE_COL: Final[str] = "code"
_OHLC_COLS: Final[tuple[str, ...]] = ("open", "high", "low", "close")
_VOL_COL: Final[str] = "volume"
_AMT_COL: Final[str] = "amount"

#: ``~/.dbconfig`` 位置（与 ``scripts/factor_backfill.py`` / ``a_share_pool.py`` 同源）
_DB_CONFIG_FILE: Final[pathlib.Path] = pathlib.Path.home() / ".dbconfig"

#: ``asel.security_master`` 里的证券名称表（5,930 行，覆盖全部 5,225 个有日线的代码）
_SECURITY_MASTER: Final[str] = "asel.security_master"

#: 名称里的**填充空白**：老行情源把 3 字名按 4 字宽补齐，于是 ``深 赛 格``、
#: ``ST 中 侨``、``万  科Ａ`` 这样存进来，直接显示很难看。
#:
#: 归一化策略是**删掉全部空白**（不是折叠成单个空格——那样 ``深 赛 格`` 原样不变）。
#: 已对全部 80 条含空白的名称核对过：没有任何一条是"ASCII 单词之间的有意义空格"
#: （``[A-Za-z] +[A-Za-z]`` 匹配数为 0），所以删除是安全的。删除后
#: ``ST 中 侨`` → ``ST中侨``、``TCL 通讯`` → ``TCL通讯``，都是正确写法。
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


@dataclass(frozen=True)
class SecurityName:
    """证券名称（+ 板块，用于在 UI 上区分主板/创业板/科创板/北交所）。"""

    code: str
    name: str
    board: str | None = None


def _clean_security_name(raw: Any) -> str:
    """去掉填充空白；非字符串/空 → 空串（调用方据此判定"没名字"）。"""
    if not isinstance(raw, str):
        return ""
    return _WHITESPACE_RE.sub("", raw).strip()


def fetch_security_names(
    codes: Sequence[str],
    *,
    conn: Any | None = None,
    conn_factory: Callable[[], Any] | None = None,
) -> dict[str, SecurityName]:
    """批量查证券名称：``{6位裸码: SecurityName}``。

    查不到的代码**不出现在返回字典里**（不是返回空名），调用方自行决定兜底文案。
    传入空序列直接返回 ``{}``，**不连 DB**。

    :param conn: 复用已有连接（调用方负责生命周期）。
    :param conn_factory: 没有 ``conn`` 时用它建一个，用完即关。
    """
    bare = [str(code).split(".", 1)[0].strip() for code in codes]
    wanted = sorted({code for code in bare if code})
    if not wanted:
        return {}

    owned = False
    if conn is None:
        if conn_factory is None:
            import psycopg  # noqa: PLC0415

            conn = psycopg.connect(**connection_kwargs())
        else:
            conn = conn_factory()
        owned = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT code, name, board FROM {_SECURITY_MASTER} WHERE code = ANY(%s)",
                (wanted,),
            )
            rows = cur.fetchall()
    finally:
        if owned:
            conn.close()

    found: dict[str, SecurityName] = {}
    for row in rows:
        code = str(row[0]).split(".", 1)[0].strip()
        name = _clean_security_name(row[1])
        if not code or not name:
            continue
        board = row[2] if len(row) > 2 and isinstance(row[2], str) else None
        found[code] = SecurityName(code=code, name=name, board=board)
    return found


def _read_dbconfig() -> dict[str, str]:
    """解析 ``~/.dbconfig`` 的 ``$KEY=value`` 行。

    与 ``asel.storage.dbconfig.read_dbconfig`` 同口径，但**本模块自带实现**——
    不 import ``asel``（该包只存在于 ``a_share_emotion_leader/`` 项目里，CPT 与
    长龙 venv 都没有）。这是刻意的：``cpt/`` 只依赖 ``psycopg``（可选）+
    ``~/.dbconfig`` 这一个文件约定，跨项目边界最小。
    """
    if not _DB_CONFIG_FILE.exists():
        return {}
    out: dict[str, str] = {}
    for line in _DB_CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("$") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def connection_kwargs() -> dict[str, Any]:
    """构造 psycopg3 连接参数（与 ``asel.storage.dbconfig.connection_kwargs`` 同口径）。"""
    cfg = _read_dbconfig()
    if not cfg.get("$RDSHOST") or not cfg.get("$DB_PW"):
        raise AShareLocalError(f"~/.dbconfig 缺失 $RDSHOST 或 $DB_PW。位置: {_DB_CONFIG_FILE}")
    ssl_cert = pathlib.Path.home() / "global-bundle.pem"
    return {
        "host": cfg["$RDSHOST"],
        "port": int(cfg.get("$DBPORT", "5432")),
        "dbname": cfg.get("$DBNAME", "longkonglong"),
        "user": cfg.get("$USER", "postgres"),
        "password": cfg["$DB_PW"],
        "connect_timeout": 15,
        **({"sslrootcert": str(ssl_cert)} if ssl_cert.exists() else {}),
    }


class AShareLocalError(RuntimeError):
    """A股本地适配器错误（DB 不可达、因子缺失、代码无数据等）。"""


class AShareNoDataError(AShareLocalError):
    """区间内在 ``public.daily_bar`` 完全没有该代码的行情。"""


class AShareNoFactorError(AShareLocalError):
    """有行情但区间内**每一天都缺复权因子** —— 画不出后复权序列。

    这是最常见的一种失败：全库 5225 只里只有 94 只有因子。单独成类是为了让上层
    能把"这只票没数据"和"这只票缺因子"分开告诉用户（实测前者会误导排查方向）。
    """


@dataclass(frozen=True)
class AShareFetchResult:
    """拉取结果（含被跳过的日期缺口，便于上层做可观测性）。"""

    bars: tuple[CanonicalBar, ...]
    skipped_no_factor: tuple[str, ...]  # ISO 日期元组


class AShareLocalClient:
    """A 股日线本地适配器（DB → CanonicalBar）。

    构造时**不连 DB**；首次调用 fetch 时按需 lazy 连接。生产可注入
    已有的 psycopg 连接（测试里注入 mock）。
    """

    def __init__(
        self,
        *,
        conn_factory: Callable[[], Any] | None = None,
    ) -> None:
        """``conn_factory`` 返回一个支持 ``cursor()`` 上下文管理器的 DB 连接。
        默认从 ``asel.storage.dbconfig.connection_kwargs`` 即时构造。"""
        self._conn_factory = conn_factory
        self._conn: Any | None = None

    def _get_conn(self) -> Any:
        if self._conn is None:
            if self._conn_factory is None:
                import psycopg  # noqa: PLC0415

                self._conn = psycopg.connect(**connection_kwargs())
            else:
                self._conn = self._conn_factory()
        return self._conn

    def fetch_security_name(self, code: str) -> SecurityName | None:
        """查单只证券名称（查不到返回 ``None``）。

        放在客户端里而不是让上层自己连库：**名字和 K 线必须来自同一条链路**，
        这样测试注入假客户端时名字自然缺席（不会偷偷连真库），生产用真客户端时
        名字自动就有。
        """
        bare = str(code).split(".", 1)[0].strip()
        if not bare:
            return None
        return fetch_security_names([bare], conn=self._get_conn()).get(bare)

    @staticmethod
    def _to_wind_code(code: str) -> str:
        """``000002`` → ``000002.SZ`` / ``600519`` → ``600519.SH`` /
        ``920025`` → ``920025.BJ``。"""
        if "." in code:
            return code
        # 顺序敏感：`92`（北交所 920xxx）必须早于 `9`（沪 B），否则 920025 会被
        # 推成 920025.SH，Wind 那边直接查无此码（R17 修）。
        if code.startswith(("92", "43", "83", "87", "88")):
            return f"{code}.BJ"
        if code.startswith(("6", "9", "5")):
            return f"{code}.SH"
        if code.startswith("4"):
            return f"{code}.BJ"
        return f"{code}.SZ"

    def fetch_validated_klines(
        self,
        code: str,
        start_ms: int,
        end_ms: int,
    ) -> AShareFetchResult:
        """拉取区间 [start_ms, end_ms] 的日线，返回后复权 CanonicalBar 序列。

        :param code: 6 位裸码（如 ``"600519"``）或带后缀（``"600519.SH"``）。
        :param start_ms / end_ms: Unix 毫秒（与 Binance 协议对齐）。
        :raises AShareLocalError: 区间内无任何数据，或 DB 异常。
        """
        start_date = datetime.fromtimestamp(start_ms / 1000, tz=UTC).date()
        end_date = datetime.fromtimestamp(end_ms / 1000, tz=UTC).date()
        bare_code = code.split(".", 1)[0]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT {_DATE_COL}, {_OHLC_COLS[0]}, {_OHLC_COLS[1]},
                               {_OHLC_COLS[2]}, {_OHLC_COLS[3]}, {_VOL_COL}, {_AMT_COL}
                        FROM public.daily_bar
                        WHERE {_CODE_COL} = %s AND {_DATE_COL} BETWEEN %s AND %s
                        ORDER BY {_DATE_COL}""",
                    (bare_code, start_date, end_date),
                )
                rows = cur.fetchall()

                cur.execute(
                    """SELECT trade_date, hfq_factor
                       FROM asel.ref_adjust_factor
                       WHERE code = %s AND trade_date BETWEEN %s AND %s""",
                    (bare_code, start_date, end_date),
                )
                factors = {row[0]: float(row[1]) for row in cur.fetchall()}
        except Exception as e:
            raise AShareLocalError(
                f"DB 读取失败 {bare_code} {start_date}~{end_date}: {type(e).__name__}: {e}"
            ) from e

        if not rows:
            raise AShareNoDataError(
                f"{bare_code} {start_date}~{end_date} 在 public.daily_bar 无数据"
            )

        bars: list[CanonicalBar] = []
        skipped: list[str] = []
        for row in rows:
            d, op, hi, lo, cl, vol, amt = row
            factor = factors.get(d)
            if factor is None:
                skipped.append(d.isoformat())
                continue
            # 时间字段：CanonicalBar 用 open_time=date 00:00:00 UTC 毫秒
            open_ms = int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)
            close_ms = open_ms + 24 * 3600 * 1000 - 1
            bars.append(
                CanonicalBar(
                    open_time=open_ms,
                    open=float(op) * factor,
                    high=float(hi) * factor,
                    low=float(lo) * factor,
                    close=float(cl) * factor,
                    volume=float(vol) if vol is not None else 0.0,
                    close_time=close_ms,
                    quote_volume=float(amt) if amt is not None else 0.0,
                    trade_count=0,  # public.daily_bar 无此列
                    taker_buy_base_volume=0.0,
                    taker_buy_quote_volume=0.0,
                    is_closed=True,
                )
            )

        if not bars:
            raise AShareNoFactorError(
                f"{bare_code} {start_date}~{end_date} 区间内所有日期都缺因子（{len(skipped)} 日）"
            )

        return AShareFetchResult(
            bars=tuple(bars),
            skipped_no_factor=tuple(skipped),
        )

    def fetch_validated_bars(self, code: str, start_ms: int, end_ms: int) -> list[BarLike]:
        """与 ``BinanceFuturesClient.fetch_validated_klines`` 同协议的薄封装。"""
        return list(self.fetch_validated_klines(code, start_ms, end_ms).bars)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
