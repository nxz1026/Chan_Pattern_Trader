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

import logging
import pathlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

from cpt.adapters._dbconfig import connection_kwargs as _shared_connection_kwargs
from cpt.domain.a_share_rules import AShareDailyTag
from cpt.domain.models import CanonicalBar
from cpt.domain.types import BarLike

_LOG = logging.getLogger(__name__)

__all__ = [
    "AShareFetchResult",
    "AShareLocalClient",
    "AShareLocalError",
    "AShareNoDataError",
    "AShareNoFactorError",
    "SecurityName",
    "check_t_plus_one_calendar",
    "fetch_daily_tags",
    "fetch_factor_codes",
    "fetch_security_names",
    "hfq_factor_on",
    "is_trade_day",
    "open_days_between",
]

#: ``public.daily_bar`` 实际列名（与 DB schema 对齐）
_DATE_COL: Final[str] = "date"

# 涨跌停标签——一旦某日触发，整根 bar 都受影响（开盘涨停 / 收盘涨停 / 一字板）。
# R24：从 domain.a_share_rules 搬来（那是 SQL，不该待在纯领域层）。
_DERIVED_FIELDS = (
    "is_limit_up",  # 收盘涨停
    "is_limit_down",  # 收盘跌停（用于对称展示）
    "is_bomb",  # 炸板（封板后开板）
    "is_one_word",  # 一字板（开/收/高/低全相等）
)


def fetch_daily_tags(conn: Any, code: str, start_ms: int, end_ms: int) -> dict[str, AShareDailyTag]:
    """从 ``public.derived_bar`` 拉取区间内的衍生标签。

    :param conn: psycopg 连接（测试中可注入 mock）
    :returns: ``{iso_date: AShareDailyTag}``；缺失日期不出现在 dict 中
    """
    start_d = datetime.fromtimestamp(start_ms / 1000, tz=UTC).date()
    end_d = datetime.fromtimestamp(end_ms / 1000, tz=UTC).date()
    bare_code = code.split(".", 1)[0]

    fields_sql = ", ".join(_DERIVED_FIELDS)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT date, {fields_sql}
                FROM public.derived_bar
                WHERE code = %s AND date BETWEEN %s AND %s""",
            (bare_code, start_d, end_d),
        )
        out: dict[str, AShareDailyTag] = {}
        for row in cur.fetchall():
            d = row[0]
            d_iso = d.isoformat() if hasattr(d, "isoformat") else str(d)
            tag = AShareDailyTag(
                code=bare_code,
                trade_date=d_iso,
                is_limit_up=bool(row[1]),
                is_limit_down=bool(row[2]),
                is_bomb=bool(row[3]),
                is_one_word=bool(row[4]),
            )
            out[tag.trade_date] = tag
    return out


def fetch_factor_codes() -> set[str]:
    """查 ``asel.ref_adjust_factor`` 里所有已有复权因子的代码。

    R24 新增：原先这段 SQL 直接写在 ``cpt/web/a_share_routes.py::_factor_codes``
    里（注释还写着「就是要碰真连接」）。web 层不碰 IO 是分层底线 ——
    SQL 归 adapters。

    这条查的是**共享数据枢纽**的表，不是 CPT 自有表，所以属于 adapters 而非 storage。
    """
    client = AShareLocalClient()
    try:
        with client._get_conn().cursor() as cur:  # noqa: SLF001
            cur.execute("SELECT DISTINCT code FROM asel.ref_adjust_factor")
            return {str(row[0]) for row in cur.fetchall()}
    finally:
        client.close()


def is_trade_day(conn: Any, date_iso: str) -> bool | None:
    """查 ``public.trade_calendar``：该日是否开市。

    :returns: ``True``/``False``；**表里没这一天返回 ``None``**（区别于「当天休市」，
        两者在 UI 上要显示不同的 reason：``not_a_trade_day`` vs ``calendar_unknown``）。

    R24 新增：原先这段 SQL 内联在 ``cpt/application/a_share_snapshot.py``
    的 ``_attach_close_countdown`` 里。application 不该出现 SQL。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_open FROM public.trade_calendar WHERE date = %s",
            (date_iso,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return bool(row[0])


def open_days_between(conn: Any, start: date, end: date) -> set[date]:
    """``[start, end]`` 区间内**所有开市日**（一次查询，不逐日问）。

    R31 新增：给「缺整天」的探测用。

    **为什么要批量**：``is_trade_day`` 是单日查询，而探测要扫 45 天 —— 逐日调就是
    45 次往返。日历表 13k 行、一次拉区间更划算。

    :returns: 开市日集合。**表里整个区间都没数据时返回空集**（调用方需自己区分
        「没有缺口」与「日历不可用」—— 本函数不替调用方猜）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT date FROM public.trade_calendar WHERE is_open AND date >= %s AND date <= %s",
            (start, end),
        )
        rows = cur.fetchall()
    return {row[0] for row in rows}


def hfq_factor_on(conn: Any, code: str, trade_date: Any) -> float | None:
    """``asel.ref_adjust_factor`` 里某个交易日的后复权因子；没这一行返回 ``None``。

    R35 新增：给「把**不复权**的实时报价换算到后复权口径」用。

    为什么要它：``fetch_validated_klines`` 出的 K 线是**后复权**（raw × 因子），
    而所有实时行情快照（新浪 / 腾讯 / 东财）给的**现价都是不复权**的。两者直接
    相减会得到荒谬的结论 —— 600519 / 2026-09-30 实测：库里不复权 1258.62、
    因子 7.06053932，快照收盘 8886.536，拿 1258.62 去比就是 **−85.84%**。

    :param trade_date: ``datetime.date`` 或 ISO 字符串。
    """
    bare = code.split(".", 1)[0]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT hfq_factor FROM asel.ref_adjust_factor WHERE code = %s AND trade_date = %s",
            (bare, trade_date),
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


# --------------------------------------------------------------------------- #
# T+1 日历查询（R21 接线；R24 从 domain 搬来）
# --------------------------------------------------------------------------- #


def check_t_plus_one_calendar(client: Any) -> dict[str, Any]:
    """查 ``public.trade_calendar`` 判断今日是否可买（T+1 日历约束）。

    只读 ``public.trade_calendar``，不涉及持仓/账户（roadmap「明确不做持仓」）。

    :returns: 字典 ``{"available": bool, "reason": str, "today": str | None,
                        "next_trade_date": str | None}``。
    """
    import datetime as _dt

    today = _dt.date.today().isoformat()
    conn: Any = None
    try:
        conn = client._get_conn()
        with conn.cursor() as cur:
            # 查今日是否开市
            cur.execute(
                "SELECT is_open FROM public.trade_calendar WHERE date = %s",
                (today,),
            )
            row = cur.fetchone()
            if row is None:
                return {
                    "available": False,
                    "reason": "calendar_unknown",
                    "today": today,
                    "next_trade_date": _next_trade_date(cur, today),
                }
            if not row[0]:
                return {
                    "available": False,
                    "reason": "not_a_trade_day",
                    "today": today,
                    "next_trade_date": _next_trade_date(cur, today),
                }
            return {
                "available": True,
                "reason": "trade_day",
                "today": today,
                "next_trade_date": None,
            }
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("T+1 日历查询失败: %s", exc)
        # 本函数**自己吞掉**异常并返回降级字典，所以调用方
        # （a_share_snapshot._attach_t_plus_one）的 except 永远不会触发 ——
        # 回滚义务只能落在这里。少了这一步，连接会留在 aborted 态，
        # 同一客户端后续所有 SQL 全废（与 R23 漏 commit 同族的坑）。
        try:
            if conn is not None:
                conn.rollback()
        except Exception as rb_exc:  # noqa: BLE001 — 回滚失败也不能因此抛出
            _LOG.debug("T+1 日历查询后回滚失败: %s", rb_exc)
        return {
            "available": False,
            "reason": "calendar_check_failed",
            "today": today,
            "next_trade_date": None,
        }


def _next_trade_date(cur: Any, after_date: str) -> str | None:
    """查 ``after_date`` 之后的下一个开市日。"""
    cur.execute(
        "SELECT date::text FROM public.trade_calendar "
        "WHERE is_open AND date > %s ORDER BY date LIMIT 1",
        (after_date,),
    )
    row = cur.fetchone()
    return row[0] if row else None


_CODE_COL: Final[str] = "code"
_OHLC_COLS: Final[tuple[str, ...]] = ("open", "high", "low", "close")
_VOL_COL: Final[str] = "volume"
_AMT_COL: Final[str] = "amount"

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

#: Wind 代码的**裸码部分**：只认 6 位 ASCII 数字。
#:
#: 不用 ``str.isdigit()``：它会放过全角数字（``６００５１９``）和上标（``²``），
#: 那些拼进 Wind 代码同样查不到票。
_WIND_BARE_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]{6}")


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
            try:
                import psycopg  # noqa: PLC0415
            except ModuleNotFoundError as exc:
                raise AShareLocalError(
                    "psycopg 未安装。A 股本地数据层需要 psycopg[binary]，"
                    "运行 `pip install -e '.[db]'` 后重启服务。"
                ) from exc

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


def connection_kwargs() -> dict[str, Any]:
    """构造 psycopg3 连接参数（缺失 ``~/.dbconfig`` 时抛 :class:`AShareLocalError`）。

    解析与校验走 :mod:`cpt.adapters._dbconfig` 的**唯一权威实现**（2026-09-25 审核
    §5.1 收口）；本函数只保留 A 股本地库特有的两件事：①异常类型注入为
    ``AShareLocalError``；②在返回值上追加 RDS CA 证书 ``~/global-bundle.pem``
    （文件存在才带——``a_share_pool`` / ``factor_backfill`` 不需要，所以不放进共享层）。
    """
    kwargs = _shared_connection_kwargs(exc_type=AShareLocalError)
    ssl_cert = pathlib.Path.home() / "global-bundle.pem"
    if ssl_cert.exists():
        kwargs["sslrootcert"] = str(ssl_cert)
    return kwargs


class AShareLocalError(RuntimeError):
    """A股本地适配器错误（DB 不可达、因子缺失、代码无数据等）。"""


class AShareNoDataError(AShareLocalError):
    """区间内在 ``public.daily_bar`` 完全没有该代码的行情。"""


class AShareNoFactorError(AShareLocalError):
    """有行情但区间内**每一天都缺复权因子** —— 画不出后复权序列。

    这是最常见的一种失败：因子表只覆盖热门池并集，全库大部分标的没有。单独成类是为了让上层
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
                try:
                    import psycopg  # noqa: PLC0415
                except ModuleNotFoundError as exc:
                    raise AShareLocalError(
                        "psycopg 未安装。A 股本地数据层需要 psycopg[binary]，"
                        "运行 `pip install -e '.[db]'` 后重启服务。"
                    ) from exc

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

    def fetch_daily_tags(
        self,
        code: str,
        start_ms: int,
        end_ms: int,
    ) -> dict[str, AShareDailyTag]:
        """拉区间内的 A 股衍生标签（``{ISO 日期: AShareDailyTag}``）。

        同样放在客户端里而不是让 application 自己连库，理由与
        :meth:`fetch_security_name` 相同：**标签必须和 K 线来自同一条链路**，
        测试注入假客户端时它自然缺席（不会偷偷连真库），生产用真客户端时
        标签自动就有（application 侧用 ``getattr`` 鸭子探针，不强制实现）。

        SQL 与标签语义在 :mod:`cpt.domain.a_share_rules`；adapters → domain
        是允许的方向（``Layered architecture`` 契约只禁止反向）。
        """
        bare = str(code).split(".", 1)[0].strip()
        if not bare:
            return {}
        return fetch_daily_tags(self._get_conn(), bare, start_ms, end_ms)

    @staticmethod
    def _to_wind_code(code: str) -> str:
        """``000002`` → ``000002.SZ`` / ``600519`` → ``600519.SH`` /
        ``920025`` → ``920025.BJ``。

        **非法输入抛 ``ValueError``，不再静默兜底**：接线前本函数对既非 6 位数字、
        又无已知后缀的输入（``""`` / ``"abc"`` / ``"600519.XX"`` / ``"700000"``）
        一律返回 ``"<原样>.SZ"``，等于拿一个不存在的 Wind 代码去查库，报回来的是
        "查无此码"—— 把"输入不合法"伪装成"Wind 没有这只票"。调用方
        （``scripts/factor_backfill.py`` 的 Wind 兜底）按 ``ValueError`` 降级。
        """
        raw = str(code).strip().upper()
        digits, _, suffix = raw.partition(".")
        if _WIND_BARE_RE.fullmatch(digits) is None:
            raise ValueError(f"不是 6 位 A 股裸码：{code!r}")
        if suffix:
            if suffix not in {"SH", "SZ", "BJ"}:
                raise ValueError(f"无法识别的交易所后缀：{code!r}")
            return f"{digits}.{suffix}"
        # 顺序敏感：`92`（北交所 920xxx）必须早于 `9`（沪 B），否则 920025 会被
        # 推成 920025.SH，Wind 那边直接查无此码（R17 修）。
        if digits.startswith(("92", "43", "83", "87", "88")):
            return f"{digits}.BJ"
        if digits.startswith(("6", "9", "5")):
            return f"{digits}.SH"
        if digits.startswith("4"):
            return f"{digits}.BJ"
        if digits.startswith(("0", "3", "2", "1")):
            return f"{digits}.SZ"
        # 剩下的首位（7 等）在 A 股不存在：宁可抛，也不推一个错的交易所出去。
        raise ValueError(f"无法从代码推断交易所：{code!r}")

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
