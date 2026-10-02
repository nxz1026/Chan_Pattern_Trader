#!/usr/bin/env python3
"""A股复权因子增量 backfill（腾讯财经 K 线同源 raw+hfq 同对 → 自洽因子）。

## 为什么走腾讯而不走 Wind / sina / 东财
- **Wind**: 用户每日 1000 积分，全市场 5850 只 × 2 = 11700 次不够（用户原话）。
- **akshare → sina**: ``stock_zh_a_daily(adjust="hfq-factor")`` 本机境外 IP 频控
  ~1 次/分钟，5850 只 = 100 小时（不可行），且回吐粒度只到"除权日"。
- **akshare → 东财 (stock_zh_a_hist)**: ``push2his.eastmoney.com`` 节流本机
  ``RemoteDisconnected``（与 plan §5 已记的 ``push2.eastmoney.com 502`` 同源）。
- **腾讯 web.ifzq.gtimg.cn**: 实测稳定，**单次请求同时回吐 raw + hfq**
  （同源同对 → ``hfq_factor = hfq/raw`` 绝对自洽，与 Wind/sina 绝对值差
  仅因"起算点"不同，**不影响"避免假跳空"的目标**）。

## 数据模型
表 ``asel.ref_adjust_factor`` 由 ``migrations/0002_p0_reference.sql:96`` 定义：
- ``code`` / ``trade_date`` / ``hfq_factor`` / ``source`` / ``source_url`` /
  ``source_ref`` / ``as_of`` / ``available_at`` / ``fetched_at``
- 起点：全 0 行；本脚本负责填齐

## 增量策略
- ``incremental``：跑"热门池（hot_rank 最新日）∪ 连板梯队（ladder_day
  cont_days≥2 最新日）∪ 近 7 天上市新股 ∪ 已有覆盖的近 30 天票" —
  约 100~300 只/日（仅每日有除权事件的几只真正写入新行）。
- ``full``: 全市场 ``asel.daily_bar_raw`` 去重代码（~5850 只）。首次跑。
- 幂等：``(code, trade_date, source)`` 主键 upsert。多次跑结果一致。

## 单只工作量
- 1 次 HTTP（腾讯日 K 线 30 天窗口 + hfq 各一）
- 取最近 30 个交易日的 daily factor
- 耗时 < 1s；incremental ≈ 数分钟

## 用法
首次全市场::

    python scripts/factor_backfill.py --mode full

每日增量（cron）::

    python scripts/factor_backfill.py --mode incremental

调参:
- ``--code-prefix``: 只扫某前缀（裸码），调试/分批用
- ``--batch-size``: 批大小（默认 500）
- ``--sleep-ms``: 请求间隔（默认 100ms，腾讯节流友好）
- ``--dry-run``: 不写 DB，只打印统计
- ``--wind-fallback``: 本地（腾讯）取不到时改用 **Wind 补取**（台账决策 C1），
  **默认关闭**——Wind 每次调用消耗真实额度，不适合当默认路径。走这条路时：
  裸码经 ``a_share_local.AShareLocalClient._to_wind_code`` 转 Wind 代码，
  因子由 ``wind_source.WindSourceClient.fetch_adjust_factors``（同区间
  不复权/后复权两次 K 线相除）算出，落库 ``source`` 标成
  ``wind:get_stock_kline`` 以便与腾讯行区分。**Wind 不可用或取数失败只记
  原因并继续**，绝不中断整轮回填。
  **R31 追加：还有一道基准闸**（``check_wind_basis``）—— Wind 的后复权基准与
  腾讯不是同一个（600519 实测因子差 22.47%），而落库是覆盖式 upsert，所以
  重叠不足或中位比超 ±2% 时**带着数字拒绝写入**，绝不静默改写主源历史。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from cpt.adapters._dbconfig import connection_kwargs as _shared_connection_kwargs
from cpt.adapters.a_share_factor import (
    FactorRow,
    build_factor_rows,
    upsert_factor_rows,
)
from cpt.adapters.a_share_local import AShareLocalClient
from cpt.adapters.a_share_public import (
    TENCENT_KLINE_URL,
    ASharePublicError,
    normalize_code,
)
from cpt.adapters.wind_source import (
    WindQuotaError,
    WindSourceClient,
    WindSourceError,
    WindUnavailableError,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 端点与 ``source`` 取值都取自 cpt 的**唯一**定义，脚本不再各留一份。
#: 2026-09-25 之前脚本自带 ``TX_ENDPOINT``（与 ``TENCENT_KLINE_URL`` 逐字相同）与
#: ``SOURCE_TX = "tx:fqkline"``（适配器写 ``tencent_fqkline``）—— 同一个腾讯接口
#: 在库里分裂成两个 ``source``，实测 49,730 行 vs 7,200 行。存量行已于 2026-09-25
#: 迁移完毕（库里现只有 ``tx:fqkline``）。
UA = "Mozilla/5.0"
DEFAULT_KLINE_DAYS = 800  # 腾讯单次上限 801 根（实测 count=800 → 801 根，覆盖 2023-06 至今）

#: Wind 兜底行写库时用的 ``source`` / ``source_url``。
#:
#: **必须与腾讯行区分**（``a_share_factor.SOURCE_TX`` / ``TENCENT_KLINE_URL``）：
#: 台账决策 C1 的验收就是"能分辨这一行是本地源还是 Wind 源补的"，同一个
#: ``source`` 会让 ``WHERE source=...`` 再也分不清，正是 2026-09-25 那个
#: ``tencent_fqkline`` / ``tx:fqkline`` 分裂坑的翻版。
SOURCE_WIND: Final[str] = "wind:get_stock_kline"
WIND_SOURCE_URL: Final[str] = "wind://stock_data.get_stock_kline"

#: Wind 因子与本地因子的**基准**允许的相对偏差（R31 真机实测，见
#: :func:`check_wind_basis`）。2% 足够容纳同一家的取整误差，又远小于跨家基准差。
WIND_BASIS_TOLERANCE: Final[float] = 0.02
#: 建立/核对基准至少需要几天重叠。低于这个数就只能说"不知道"，不能"大概一致"。
WIND_BASIS_MIN_OVERLAP: Final[int] = 3

logger = logging.getLogger("factor_backfill")


# --------------------------------------------------------------------------- #
# DB 工具（解析与校验走 cpt.adapters._dbconfig 的唯一权威实现）
# --------------------------------------------------------------------------- #


def connection_kwargs() -> dict[str, Any]:
    """构造 psycopg3 连接参数（缺失 ``~/.dbconfig`` 时 ``SystemExit``）。

    解析与校验在 :mod:`cpt.adapters._dbconfig`（2026-09-25 审核 §5.1 收口，合并前
    本脚本自带一份逐行重复实现）。本脚本是运维入口，缺配置时**直接退出并打印一行
    原因**比抛栈友好，故把异常类型注入为 ``SystemExit``——与合并前行为一致。

    返回 ``dict[str, Any]``（不是 ``dict[str, object]``）以便直接 ``**`` 展开给
    ``psycopg.connect``：后者有重载签名，``object`` 会让 mypy strict 报一堆 arg-type。
    """
    return _shared_connection_kwargs(exc_type=SystemExit)


# --------------------------------------------------------------------------- #
# 腾讯 K 线 raw + hfq 同对 → 自洽因子
# --------------------------------------------------------------------------- #


def _fetch_tx_pair(tx_sym: str, days: int) -> tuple[list[list[str]], list[list[str]]]:
    """返回 (raw, hfq)，二者同源同对。"""

    def one(adj: str) -> list[list[str]]:
        url = f"{TENCENT_KLINE_URL}?param={tx_sym},day,,,{days},{adj}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("gbk", errors="replace")
        if text.startswith("<") or "http-equiv" in text.lower():
            raise RuntimeError(f"腾讯反爬（HTML 头）：{text[:80]!r}")
        d = json.loads(text)
        block = d["data"][tx_sym]
        key = f"{adj}day" if adj else "day"
        # 显式标注：``json.loads`` 回 Any，直接 return 会被 mypy 判 no-any-return。
        rows: list[list[str]] = block[key]
        return rows

    return one(""), one("hfq")


def fetch_tx_factor_rows(code_wind: str, *, days: int = DEFAULT_KLINE_DAYS) -> list[FactorRow]:
    """拉一只票最近 N 天 raw+hfq，算每日 ``hfq_factor = hfq/raw``。

    市场前缀交给 :func:`cpt.adapters.a_share_public.normalize_code`（唯一权威实现）。
    本脚本原先自带一份 ``_code_to_tx``，**有先后顺序 bug**：``9``（沪 B）写在
    ``92``（北交所新代码段）之前，于是 ``920201`` 被推成 ``sh920201``；而且它不认
    ``43/83/87/88``，``830799`` 直接抛错。``normalize_code`` 在 R17 就修掉了这个顺序
    问题（注释里点名过 ``a_share_local._to_wind_code`` 同样的 bug），本脚本这第三份
    一直没跟上。删掉本地实现后，这类 bug 由构造消除。

    **因子计算也一并收口**到 :func:`cpt.adapters.a_share_factor.build_factor_rows`：
    本函数原先自带一份 ``h/r`` 循环，只有 ``r == 0`` 一个保护，**腾讯 hfq 序列退化
    完全挡不住**（2026-09-30 实测腾讯 40 只票里 15 只的末根退化成不复权价，300750
    的因子会从 1.93 跳到 1.00、000002 跳到 115.08）。两份实现算同一个东西、保护强度
    还不一样，是 R20 修掉的第三个 drift。
    """
    tx_sym = normalize_code(code_wind)
    raw, hfq = _fetch_tx_pair(tx_sym, days)
    rows = build_factor_rows(normalize_code(code_wind)[2:], _to_bars(raw), _to_bars(hfq))
    return [replace(row, code=code_wind) for row in rows]


# --------------------------------------------------------------------------- #
# Wind 兜底（台账决策 C1）
# --------------------------------------------------------------------------- #


def wind_client() -> WindSourceClient:
    """Wind 客户端工厂。

    **构造不发请求、不花额度**（见 ``wind_source`` 模块 docstring），真正花钱的是
    :meth:`WindSourceClient.probe`。抽成函数是为了让测试注入假 runner —— 否则
    "接线是否真的通了"只能靠真花额度去试。
    """
    return WindSourceClient()


def fetch_wind_factor_rows(
    code: str,
    *,
    days: int = DEFAULT_KLINE_DAYS,
    client: WindSourceClient | None = None,
) -> list[FactorRow]:
    """Wind 兜底：6 位裸码 → Wind 代码 → 同区间两次 K 线相除 → ``FactorRow``。

    本函数是这两个"待接线"函数的**生产调用点**（接线前全仓 0 代码引用）：

    - ``a_share_local.AShareLocalClient._to_wind_code``（裸码 → ``600519.SH``）
    - ``wind_source.WindSourceClient.fetch_adjust_factors``（``{日期: 后复权因子}``）

    Raises:
        ValueError: ``code`` 非法 —— ``_to_wind_code`` 不再静默兜底成 ``.SZ``。
        WindUnavailableError / WindQuotaError / WindSourceError: Wind 侧分级失败，
            调用方据此决定记哪种原因（这三类必须分开，否则运维分不清"没接上"
            和"额度没了"）。
    """
    windcode = AShareLocalClient._to_wind_code(code)
    end = datetime.now(tz=UTC).date()
    begin = end - timedelta(days=days)
    factors = (client or wind_client()).fetch_adjust_factors(
        windcode, begin_date=begin, end_date=end
    )
    return [
        FactorRow(
            code=code,
            trade_date=day,
            hfq_factor=value,
            source=SOURCE_WIND,
            source_ref=f"wind get_stock_kline {day}",
        )
        for day, value in sorted(factors.items())
    ]


@dataclass(frozen=True, slots=True)
class _SlimBar:
    """``build_factor_rows`` 唯一读到的两个字段（其余一律不构造）。"""

    open_time: int
    close: float


def _to_bars(rows: Sequence[Sequence[str]]) -> list[Any]:
    """腾讯原始行 → 只带 ``open_time`` / ``close`` 的轻量对象。

    :func:`build_factor_rows` 只读这两个字段（``_close_map`` 取
    ``bar.open_time`` 与 ``bar.close``），而脚本拿到的是腾讯的**列表行**（字段顺序
    依赖上游约定，见 ``a_share_public`` 模块 docstring 坑 1）。这里显式按位置取，
    不构造 ``CanonicalBar``：本脚本不校验 K 线契约，多余字段只会让 12 个必填参数
    的构造变成第二个失败点。
    """
    out: list[Any] = []
    for row in rows:
        if len(row) < 3:
            continue
        out.append(_SlimBar(open_time=_day_to_open_ms(row[0]), close=float(row[2])))
    return out


def _day_to_open_ms(day: str) -> int:
    """腾讯的 ``YYYY-MM-DD`` → UTC 零点毫秒（与 :func:`_date_of` 严格互逆）。"""
    parsed = datetime.strptime(day, "%Y-%m-%d")
    return int(parsed.replace(tzinfo=UTC).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# 代码候选
# --------------------------------------------------------------------------- #


def list_all_a_codes() -> list[str]:
    """全市场代码（从 ``public.daily_bar`` 去重）。

    **表名是实测出来的**：脚本原先查 ``asel.daily_bar_raw``，而库里**没有这张表**
    （业务表只有 ``asel.ref_adjust_factor`` / ``asel.security_master`` 与
    ``public.*``）—— ``--mode full`` 一直在抛
    ``psycopg.errors.UndefinedTable: relation "asel.daily_bar_raw" does not exist``，
    即全市场 backfill 从没成功跑过。实跑日期 2026-09-30。

    ``code`` 是**裸 6 位**（实测 5,222 只无一带后缀），交给
    :func:`cpt.adapters.a_share_public.normalize_code` 推市场前缀。
    """
    import psycopg  # noqa: PLC0415

    with psycopg.connect(**connection_kwargs()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT code FROM public.daily_bar")
            codes = [r[0] for r in cur.fetchall() if r[0]]
    return sorted(codes)


def list_incremental_codes() -> list[str]:
    """增量 = 热门池 + 连板梯队 + 近 7 天新股 + 已有覆盖（防止有除权事件漏刷）。"""

    import psycopg  # noqa: PLC0415

    with psycopg.connect(**connection_kwargs()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT max(date) FROM public.hot_rank")
            row = cur.fetchone()
            hot_date = row[0] if row else None
            cur.execute("SELECT code FROM public.hot_rank WHERE date = %s", (hot_date,))
            hot = {r[0] for r in cur.fetchall()}

            cur.execute("SELECT max(date) FROM public.ladder_day")
            row = cur.fetchone()
            lad_date = row[0] if row else None
            cur.execute(
                "SELECT code FROM public.ladder_day WHERE date=%s AND cont_days>=2",
                (lad_date,),
            )
            lad = {r[0] for r in cur.fetchall()}

            cur.execute(
                """SELECT code FROM asel.daily_bar_raw
                   GROUP BY code
                   HAVING min(date) >= (SELECT max(date) FROM asel.daily_bar_raw) - 7"""
            )
            fresh = {r[0] for r in cur.fetchall()}

            cur.execute("SELECT DISTINCT code FROM asel.ref_adjust_factor")
            covered = {r[0] for r in cur.fetchall() if r[0]}

    return sorted(hot | lad | fresh | covered)


def latest_factor_date(conn: Any, code: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(trade_date) FROM asel.ref_adjust_factor WHERE code=%s",
            (code,),
        )
        row = cur.fetchone()
        d = row[0] if row else None
    return d.isoformat() if d else None


def unverifiable_dates(conn: Any, code: str) -> set[str]:
    """该票**溯源不可考**的日期集合（``source IS NULL``）。

    ## 为什么按 ``source IS NULL`` 判，而不是按因子值

    2026-09-30 补列之前写入的 3,388,417 行 ``hfq_factor`` 全是列默认值 ``1.0``
    —— 那是**占位**，不是"该票未除权"这个结论。它们没有任何一列能说明来源，
    所以判据就是"没有 source"。

    两个不能用的替代判据：

    - **``hfq_factor = 1.0``**：真除权因子恰好等于 1.0 的日子是合法数据（当日未除权），
      按值判会反复回填；更糟的是从没除过权的票（因子恒为 1.0）会被误判成"全是占位"，
      每轮都白拉一次腾讯。
    - **只补最近 N 天**：因子是**乘在 OHLC 上**的（``a_share_local._py:318-321``），
      只补最近 30 天会在窗口边界造出一条 1.0 → 6.39 的假跳空 —— 茅台那种量级下，
      比全表 1.0 危害更大。窗口边界必须在**因子序列内部**，所以宁可全量覆盖。

    补过的行 ``source = 'tx:fqkline'``，下一轮 ``--mode incremental`` 就不会再取，
    幂等性由主键 ``(code, trade_date)`` 的 upsert 保证。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date FROM asel.ref_adjust_factor WHERE code=%s AND source IS NULL",
            (code,),
        )
        return {r[0].isoformat() for r in cur.fetchall()}


#: 网络失败重试次数。腾讯按 IP 限流，返回 ``501 Not Implemented``（R20 实测：密集
#: 抓 80+ 只票后持续 501，连 ``curl`` 换 UA/Referer 都一样；同一时刻 ``qt.gtimg.cn``
#: 快照端点正常 200 → 限流是**按端点**的）。退避 5/10/20s 共 3 次。
DEFAULT_RETRIES: Final[int] = 3
RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 10.0, 20.0)


def _retry(code: str, idx: int, exc: Exception, retries: int) -> bool:
    """网络失败退避重试；返回 ``True`` 表示**已退避并跳过本轮**（交给下一轮补）。

    不在原地重试同一只票：限流是**全局**的（实测 501 期间所有代码都失败），
    原地重试只是把退避时间浪费在同一次注定失败的请求上。改成记为待补、继续下一只，
    让全市场跑完后由 ``--mode incremental`` 统一补齐。
    """
    if retries <= 0:
        return False
    backoff = RETRY_BACKOFF_SECONDS[min(idx, len(RETRY_BACKOFF_SECONDS) - 1)]
    logger.warning(
        "腾讯限流/网络失败 %s（%s），退避 %.0fs 后跳过本轮（本轮不再重试，"
        "限流是全局的，稍后用 --mode incremental 统一补）",
        code,
        str(exc)[:60],
        backoff,
    )
    time.sleep(backoff)
    return True


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def _failure_text(kind: str, etype: str, detail: str) -> str:
    """本地源失败原因的可读文案（Wind 兜底日志里"本地那一半"）。"""
    if kind == "empty":
        return "本地源无数据"
    if not detail:
        return f"本地源{kind}失败"
    return f"本地源{kind}失败 {etype}: {detail}"


def _local_factors(conn: Any, code: str) -> dict[str, float]:
    """本地库里这只票**已落盘**的因子（``{ISO 日期: hfq_factor}``）。

    只取 ``source IS NOT NULL`` 的行 —— ``source IS NULL`` 的是回填前的占位行，
    拿它当"本地基准"等于自己跟自己比。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trade_date, hfq_factor FROM asel.ref_adjust_factor "
            "WHERE code = %s AND source IS NOT NULL",
            (code,),
        )
        return {str(d): float(v) for d, v in cur.fetchall()}


def check_wind_basis(rows: Sequence[FactorRow], local: dict[str, float]) -> str:
    """Wind 因子与本地因子的基准是否一致；返回 ``""``=一致，否则是一句拒绝理由。

    ## R31 真机实测（2026-10-02，600519，2026-09-30）

    - **不复权收盘两边完全一致**：Wind 1258.62 = 本地 ``public.daily_bar`` 1258.62；
    - **后复权因子差 22.47%**：Wind 8.6469 vs 本地 7.0605。

    也就是说**数据没错，是"后复权"的基准不是同一个**。后复权价 = 不复权价 × 因子，
    而因子落库走 ``ON CONFLICT (code, trade_date) DO UPDATE`` **直接覆盖**同键旧值。
    于是写进去的不是"精度略差的数据"，而是**整段历史被换了一套基准**，并且在与
    旧数据的交界处凭空出现一个 22% 的跳空 —— 而 ``fetch_validated_klines`` 正是
    拿这个因子去乘 OHLC 画笔的。

    宁可少取几只票，也不能悄悄改写主源数据。所以这里只做两件事：能证明一致就放行，
    不能证明（重叠不足）或证明不一致，就带着数字拒绝。

    :param rows: Wind 取回的因子行。
    :param local: 本地已落盘因子。
    :returns: ``""`` 表示放行；非空为拒绝理由（会进日志）。
    """
    shared = [r for r in rows if r.trade_date in local and local[r.trade_date] > 0]
    if len(shared) < WIND_BASIS_MIN_OVERLAP:
        return (
            f"wind_basis_unknown: 与本地因子只重叠 {len(shared)} 天"
            f"（<{WIND_BASIS_MIN_OVERLAP}），无法确认基准一致，不写入"
        )
    ratios = sorted(r.hfq_factor / local[r.trade_date] for r in shared)
    mid = ratios[len(ratios) // 2]
    if abs(mid - 1.0) > WIND_BASIS_TOLERANCE:
        return (
            f"wind_basis_mismatch: Wind/本地 因子中位比 {mid:.4f}"
            f"（偏差 {(mid - 1) * 100:+.2f}%，容差 ±{WIND_BASIS_TOLERANCE:.0%}）"
            f"，两家的后复权基准不是同一个，不写入"
        )
    return ""


def _wind_fallback(code: str, conn: Any) -> tuple[list[FactorRow], str]:
    """跑一次 Wind 兜底，返回 ``(行, 状态文案)``。

    **任何异常都只转成文案，绝不向上抛**：兜底失败不能把整轮回填带崩（任务硬约束）。
    文案前缀区分"功能没接通"（``wind_unavailable``）与"接通了但取不到"
    （``wind_error`` / ``wind_quota`` / ``wind_empty``）—— 否则运维看到一行
    "没数据"分不清是去修数据还是去装 CLI。

    R31 新增一道**基准闸**：Wind 取回来的因子未必和本地同一基准（实测差 22.47%），
    而落库是覆盖式 upsert，所以对不齐就不写（见 :func:`check_wind_basis`）。
    """
    try:
        rows = fetch_wind_factor_rows(code)
    except ValueError as e:
        return [], f"wind_code_invalid: {e}"
    except WindUnavailableError as e:
        return [], f"wind_unavailable: {e}"
    except WindQuotaError as e:
        return [], f"wind_quota: {e}"
    except WindSourceError as e:
        return [], f"wind_error: {e}"
    except Exception as e:  # noqa: BLE001
        return [], f"wind_unexpected: {type(e).__name__}: {e}"
    if not rows:
        return [], "wind_empty: Wind 未返回可用因子行"
    try:
        basis_note = check_wind_basis(rows, _local_factors(conn, code))
    except Exception as e:  # noqa: BLE001
        # 基准核不出来就不能放行 —— 与其猜，不如明说
        return [], f"wind_basis_check_failed: {type(e).__name__}: {e}"
    if basis_note:
        return [], basis_note
    return rows, "wind ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A 股复权因子 backfill（腾讯财经）")
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--code-prefix", help="只处理以此开头的代码")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--sleep-ms", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--wind-fallback",
        action="store_true",
        help=(
            "本地（腾讯）取不到时改用 Wind 源补取。默认关闭：Wind 调用消耗真实额度。"
            "另需与本地因子基准一致（±2%%）才写入，否则只记原因不写"
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="网络失败退避重试次数（0=不重试；限流是全局的，重试只是跳过本轮）",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.mode == "full":
        codes = list_all_a_codes()
    else:
        codes = list_incremental_codes()

    if args.code_prefix:
        codes = [c for c in codes if c.startswith(args.code_prefix)]

    logger.info(
        "模式=%s 待处理=%d 前缀=%s dry_run=%s sleep=%dms wind_fallback=%s",
        args.mode,
        len(codes),
        args.code_prefix or "(all)",
        args.dry_run,
        args.sleep_ms,
        args.wind_fallback,
    )
    if not codes:
        logger.info("无代码可处理，退出。")
        return 0

    import psycopg  # noqa: PLC0415

    total = 0
    fail_count = 0
    with psycopg.connect(**connection_kwargs()) as conn:
        for idx, code in enumerate(codes, 1):
            # 本地源（腾讯）取数。失败按**类别**记下来：默认路径要逐字复现接线前的
            # 日志与计数，Wind 兜底路径要能分辨"本地源没数据"和"Wind 没接通"。
            rows: list[FactorRow] = []
            source_url = TENCENT_KLINE_URL
            failure = ""  # ""=本地成功 / empty / permanent / network / unknown
            etype = ""
            detail = ""
            try:
                rows = fetch_tx_factor_rows(code)
                if not rows:
                    failure = "empty"
            except (ValueError, ASharePublicError) as e:
                # 永久错误：代码本身不认识/无法推断市场、腾讯不供该标的后复权 ——
                # 重试没有意义，直接跳过。``normalize_code`` 抛的是 ``ASharePublicError``
                # （基类是 ``RuntimeError`` 而不是 ``ValueError``），必须显式列出；
                # 这条要在下面那条 ``RuntimeError`` 之前，否则会被归类成"可重试的
                # 网络失败"。
                failure, etype, detail = "permanent", type(e).__name__, str(e)[:100]
            except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as e:
                if _retry(code, idx, e, args.retries):
                    continue
                failure, etype, detail = "network", type(e).__name__, str(e)[:100]
            except Exception as e:  # noqa: BLE001
                failure, etype, detail = "unknown", type(e).__name__, str(e)[:100]

            if failure:
                if not args.wind_fallback:
                    # ---- 默认路径：与接线前逐字一致 ----
                    if failure == "permanent":
                        logger.info("跳过 %s: %s", code, detail)
                        fail_count += 1
                    elif failure == "network":
                        logger.warning("腾讯因子失败 %s: %s: %s", code, etype, detail)
                        fail_count += 1
                    elif failure == "unknown":
                        logger.warning("未知错误 %s: %s: %s", code, etype, detail)
                        fail_count += 1
                    # failure == "empty"：本地无数据，不打日志也不计失败（旧行为）。
                    time.sleep(args.sleep_ms / 1000)
                    continue
                # ---- Wind 兜底（--wind-fallback）----
                wind_rows, wind_note = _wind_fallback(code, conn)
                if not wind_rows:
                    logger.warning(
                        "Wind 兜底未取到 %s：本地[%s]；Wind[%s]",
                        code,
                        _failure_text(failure, etype, detail),
                        wind_note,
                    )
                    fail_count += 1
                    time.sleep(args.sleep_ms / 1000)
                    continue
                rows, source_url = wind_rows, WIND_SOURCE_URL
                logger.info(
                    "Wind 兜底命中 %s：本地[%s] → Wind 取到 %d 行",
                    code,
                    _failure_text(failure, etype, detail),
                    len(wind_rows),
                )
            # 过滤：**只**跳过已写入真实数据的行（``source IS NOT NULL``）。存量占位行
            # （``source IS NULL``）必须放行，否则 ``max(trade_date)`` 已经是最新那天、
            # 全部行都被过滤掉，脚本会"成功"地写入 0 行。详见 unverifiable_dates()。
            unverifiable = unverifiable_dates(conn, code)
            if unverifiable:
                rows = [r for r in rows if r.trade_date in unverifiable]
                if not rows:
                    time.sleep(args.sleep_ms / 1000)
                    continue
            n = upsert_factor_rows(conn, rows, source_url=source_url, dry_run=args.dry_run)
            total += n
            logger.info(
                "[%d/%d] %s 新增 %d 行 (%s ~ %s)",
                idx,
                len(codes),
                code,
                n,
                rows[0].trade_date,
                rows[-1].trade_date,
            )
            time.sleep(args.sleep_ms / 1000)
            if idx % args.batch_size == 0:
                logger.info(
                    "=== 已 %d/%d 写入=%d 失败=%d ===",
                    idx,
                    len(codes),
                    total,
                    fail_count,
                )

    logger.info(
        "完成 — 模式=%s 写入=%d 失败=%d dry_run=%s",
        args.mode,
        total,
        fail_count,
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
