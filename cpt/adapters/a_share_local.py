"""A 股本地适配器：从 ``public.daily_bar`` × ``asel.ref_adjust_factor`` 读后复权
OHLC，转为 :class:`~cpt.domain.models.CanonicalBar`。

**只读**，绝不写 DB。因子由 ``scripts/factor_recompute.py``（R39 起默认东财源，
写暂存表）或 ``scripts/factor_backfill.py``（按需从腾讯补单只）维护 ——
两者都不在 cpt 核心依赖里，避免把 akshare/psycopg 等拖进 ``dependencies = []``。

## ⚠️ 因子表有「口径纪元」，读之前必须知道

``asel.ref_adjust_factor`` 里的值**不是一个恒定的真值** —— 它取决于「谁写的、
什么时候写的」。跨口径直接比较（尤其是跟历史信号/快照比）会得出错误结论。

### 口径 A（2026-09 之前，tx:fqkline）

- 因子 = 腾讯「后复权收盘 ÷ 不复权收盘」的**逐日比值**，本身带漂移；
- 当时实测 5222 只里 **3036 只是占位**（``source IS NULL``，恒为 1.0），
  另有 **2125 只非单调** —— 纯后复权因子必须单调不降，所以那一列对它们
  **不是后复权因子**；
- 后果：占位票 ``hfq_factor ≡ 1.0`` ⇒ ``CanonicalBar.open = raw_open``，
  除权日的跳空被当成**真实下跌**喂给缠论 ⇒ 分型/笔端点位置偏。

### 口径 B（R44 起，eastmoney:events）

- 由 `scripts/factor_recompute.py` 按**公司行动**重算，台阶匹配率实测 100%、
  非单调 0 只、孤儿行 0；
- 保留 205 只未覆盖的票（98 只东财确认从未分红 ⇒ 1.0 本来就对；
  其余是除权日早于 bar 起点而**故意拒写**的，见 R44 交接文档）。

### 跨口径比较时怎么办

``public.cpt_signal_event`` 与 ``public.cpt_dashboard_run`` 里有**用旧口径算出来的
price / snapshot**。切到口径 B 后它们**不会自动复现**（结构判定已变）——
比较时必须按时间点区分，别把「信号消失」当成 bug 排查一轮。
详见 ``docs/archive/plans-and-acceptance.md``。

重算只写暂存表 ``asel.ref_adjust_factor_v2``，**不碰生产表**（R37 起的纪律）；
切换与否见 ``scripts/factor_report.py`` 的逐票结论。
详见 ``docs/known-traps.md`` 与 ``docs/progress-log.md`` R39~R44。

## 数据流
- ``public.daily_bar``: ``code`` / ``date`` / OHLC / volume / amount（**不复权**，5,223 只）
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
    "ASharePlaceholderRowsError",
    "SecurityName",
    "check_t_plus_one_calendar",
    "fetch_daily_tags",
    "fetch_factor_codes",
    "fetch_latest_raw_bar",
    "fetch_latest_raw_close",
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


def fetch_latest_raw_bar(code: str) -> tuple[date, float] | None:
    """不复权收盘价的**交易日 + 价格**（``public.daily_bar`` 最新一行）。

    为什么在 :func:`fetch_latest_raw_close` 之外多这一个：算「后复权倍率」必须
    **分子分母同一天**。快照的 K 线会**拒绝缺因子的那一天**（见本模块「缺口处理」，
    拒绝而非填 1.0），所以 ``candles[-1]`` 未必是 ``daily_bar`` 的最新一天 ——
    最新那天缺因子时，快照会退到前一天，而 :func:`fetch_latest_raw_close` 仍返回
    最新那天。两天的后复权因子在除权日之间会变，相除得到的倍率是错的。

    把日期一起带出来，调用方才能判定「确实是同一天」再相除；判不了就不给倍率。

    拿不到（无 psycopg / DB 不可达 / 该代码无数据 / close 为 NULL）返回 ``None``，
    **不抛** —— 理由同 :func:`fetch_latest_raw_close`。

    # gate: allow-silent: 拿不到就**不给倍率**，不崩 —— 消费方
    # ``cpt/web/a_share_routes.py`` 的 price_ratio 分支把 None 当**正常分支**
    # （就是「今天没有倍率」），不会拿它冒充「有值但算错」。且本函数自己
    # ``with psycopg.connect(...)`` 建连、用完即弃，**不复用调用方的连接**，
    # 所以吞掉不会把同连接后续语句拖进 aborted 事务（那才是门禁担心的放大路径）。
    """
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(**connection_kwargs()) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT date, close FROM public.daily_bar WHERE code=%s ORDER BY date DESC LIMIT 1",
                (code,),
            )
            row = cur.fetchone()
        if not row or row[1] is None:
            return None
        trade_date = row[0]
        # 列是 date 但驱动可能回 datetime / str，统统归一成 date。
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()
        elif isinstance(trade_date, str):
            trade_date = date.fromisoformat(trade_date)
        return trade_date, float(row[1])
    except Exception:  # noqa: BLE001 — 拿不到就不给倍率，不崩
        return None


def fetch_latest_raw_close(code: str) -> float | None:
    """不复权收盘价（``public.daily_bar.close``，未复权）。

    与快照里的 ``candles[-1].close`` **口径不同**：快照那份是后复权价（画图用，
    复权后价格连续、结构才连得上）；这里给「推荐留痕」记真实成交价。

    R46：从 ``cpt.web.a_share_routes._raw_close`` 下沉而来 —— web 层不再直接写
    SQL（过 ``scripts/check_sql_layering.py`` 分层门禁）。

    拿不到（无 psycopg / DB 不可达 / 该代码无数据）返回 ``None``，**不抛**：
    调用方会退回后复权价。

    实现在 :func:`fetch_latest_raw_bar`（那把交易日一起带回来）—— 本函数只要价格。
    """
    bar = fetch_latest_raw_bar(code)
    return bar[1] if bar is not None else None


class AShareLocalError(RuntimeError):
    """A股本地适配器错误（DB 不可达、因子缺失、代码无数据等）。"""


class AShareNoDataError(AShareLocalError):
    """区间内在 ``public.daily_bar`` 完全没有该代码的行情。"""


class AShareNoFactorError(AShareLocalError):
    """有行情但区间内**每一天都缺复权因子** —— 画不出后复权序列。

    这是最常见的一种失败：因子表只覆盖热门池并集，全库大部分标的没有。单独成类是为了让上层
    能把"这只票没数据"和"这只票缺因子"分开告诉用户（实测前者会误导排查方向）。
    """


class ASharePlaceholderRowsError(AShareLocalError):
    """有行情行，但**每一行都是占位行**（OHL 全 0、无成交）—— 画不出任何序列。

    单独成类的原因：它与 :class:`AShareNoDataError`、:class:`AShareNoFactorError`
    是三种不同的上游故障，排查方向完全不同 ——
    「没数据」查采集是否在跑，「缺因子」查因子表覆盖，
    而「全是占位行」说明采集**跑了但写出了废行**（实测 2026-09-28~09-30
    上游一次性写出 35 行 O/H/L=0、vol=0、amt=0，其中 18 行连 close 也是 0，
    涉及 18 只票）。合并成同一类会把排查指到错误的方向。
    """


@dataclass(frozen=True)
class AShareFetchResult:
    """拉取结果（含被跳过的日期缺口，便于上层做可观测性）。"""

    bars: tuple[CanonicalBar, ...]
    skipped_no_factor: tuple[str, ...]  # ISO 日期元组
    # R52 新增：被丢弃的**占位行**日期（原始 O/H/L 全 0，无论 close 是否为 0）。
    # 与 skipped_no_factor 分开记，因为两者对上游的指控完全不同：
    # 前者是「因子表没覆盖这只票」，后者是「采集写出了废行」。默认值 `()`
    # 是为了不破坏测试里那些只给两个字段的 duck-type 假结果对象。
    skipped_placeholder: tuple[str, ...] = ()


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
            # ⚠️ R52：必须 rollback。``_get_conn()`` **复用**同一条连接
            # （lazy 连接只在第一次建），所以语句失败后连接停在 aborted 态，
            # 这个客户端后续每一次 SQL 都报 ``current transaction is aborted`` ——
            # 一次局部失败被放大成整条 A 股链路全废。与同文件
            # ``check_t_plus_one_calendar`` 的回滚纪律一致。
            try:
                conn.rollback()
            except Exception as rb_exc:  # noqa: BLE001 — 回滚失败也不能因此抛出
                _LOG.debug("日线读取失败后回滚失败 %s: %s", bare_code, rb_exc)
            raise AShareLocalError(
                f"DB 读取失败 {bare_code} {start_date}~{end_date}: {type(e).__name__}: {e}"
            ) from e

        if not rows:
            raise AShareNoDataError(
                f"{bare_code} {start_date}~{end_date} 在 public.daily_bar 无数据"
            )

        bars: list[CanonicalBar] = []
        skipped: list[str] = []
        placeholders: list[str] = []
        for row in rows:
            d, op, hi, lo, cl, vol, amt = row
            factor = factors.get(d)
            if factor is None:
                skipped.append(d.isoformat())
                continue
            # R52：**占位行**守卫。上游会在某些交易日写出 O/H/L 全 0、
            # vol=amt=0 的行（实测 2026-09-28~09-30 一次批量 35 行 / 18 只票）。
            # 这类行**构造不出合法 K 线**，但危害有两档，且都很糟：
            #
            # ① close≠0（上游填了前收盘价）：``validate_ashare_bars`` 抛
            #    DataValidationError ⇒ **整只票降级**，657 根里 1 根坏就全废。
            # ② close=0（完全空行）：校验器 ``low<=close<=high`` 判 0<=0<=0 **成立**
            #    ⇒ 放行 ⇒ 零价 K 线进结构计算 ⇒ 造出假分型/假笔/假中枢，
            #    **全程零报错**。这档更毒，因为没有任何信号。
            #
            # 判据只看 O/H/L 是否**同时**为 0，**不看 close** —— 正是为了让②也被拦下。
            # 用原始值（未复权）判定：复权因子再正常，0 * factor 仍是 0。
            #
            # 为什么不修校验器去「容忍」：0 价 K 线本身就不合法，放它进去等于
            # 违反 ``docs/rules.md`` §5.3「非交易日不出图、不用 0 填充」。
            # 丢弃才是对的 —— A 股本来就有合法的日历/停牌缺口，缺一天不影响结构。
            if float(op) == 0.0 and float(hi) == 0.0 and float(lo) == 0.0:
                placeholders.append(d.isoformat())
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
            # 三种「一条都画不出来」的原因，指控对象各不相同，必须分开报，
            # 否则排查会被指到错误的方向（见三个错误类的 docstring）。
            if placeholders and len(placeholders) == len(rows) - len(skipped):
                # 有因子、也查到了行情行，但**每一行都是占位行**。
                raise ASharePlaceholderRowsError(
                    f"{bare_code} {start_date}~{end_date} 区间内 "
                    f"{len(placeholders)} 行全是占位行（O/H/L 全 0、无成交）—— "
                    f"上游采集写出了废行，不是「无行情」也不是「缺因子」。"
                    f"样例日期：{', '.join(placeholders[:3])}"
                )
            raise AShareNoFactorError(
                f"{bare_code} {start_date}~{end_date} 区间内所有日期都缺因子（{len(skipped)} 日）"
            )

        return AShareFetchResult(
            bars=tuple(bars),
            skipped_no_factor=tuple(skipped),
            skipped_placeholder=tuple(placeholders),
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
