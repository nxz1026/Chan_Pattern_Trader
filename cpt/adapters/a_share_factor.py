"""A 股后复权因子**重算**与暂存表管理（R37/R64/R96）。

## 为什么需要这个

``asel.ref_adjust_factor`` 是生产表，覆盖面要全市场每天都有因子。
现在有两条路在往里写：

1. **公司行动重算**（``scripts/factor_recompute.py``）：按 Wind / 东财公司行动
   算出**真·分段常数**因子，先写暂存表 ``asel.ref_adjust_factor_v2``，
   由 :func:`promote_recompute_factors` 搬进生产表（``--promote``）。这是**唯一正确**的来路。
2. **因子沿用**（``scripts/factor_carry_forward.py``）：把暂存表最新一档沿用到当天，
   只是过渡手段，不应覆盖已正确的值。

## 关键设计：不预测，只按事件

后复权因子按定义是**公司行动的台阶函数**：两次除权之间必须逐位相同。
所以这里**不取供应商的后复权序列相除**（R36/R95/R96 实测：腾讯 hfq 逐日漂移，
用它当因子等于把漂移固化成台阶）。唯一真值是公司行动本身。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final

__all__ = [
    "FACTOR_RECOMPUTE_TABLE",
    "PROMOTE_SOURCE_URL",
    "codes_with_factors",
    "ensure_recompute_stage",
    "factor_from_actions",
    "hot_pool_codes",
    "load_recent_closes",
    "promote_recompute_factors",
    "save_recompute_factors",
]

_LOG = logging.getLogger("cpt.adapters.a_share_factor")

#: 暂存表名。**重算是只写暂存表的**：``factor_recompute.py`` 默认一个字节都不碰生产表
#: （切换是人工决定：看报告 → 决定），只有显式传 ``--promote`` 才由
#: :func:`promote_recompute_factors` 搬进 ``asel.ref_adjust_factor``（R64，用户拍板 a）。
FACTOR_RECOMPUTE_TABLE: Final[str] = "asel.ref_adjust_factor_v2"

#: promote 写进生产表的 ``source_url``。重算的值来自**东财公司行动**，出处必须能对账；
#: 写成 null 会让「这一行哪来的」永远查不清。
PROMOTE_SOURCE_URL: Final[str] = "eastmoney:corporate-actions (recompute)"
# R37：按 Wind 公司行动重算后复权因子（**只写暂存表**，见 factor_recompute.py）
# --------------------------------------------------------------------------- #

_RECOMPUTE_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {FACTOR_RECOMPUTE_TABLE} (
    code        text        NOT NULL,
    trade_date  date        NOT NULL,
    hfq_factor  numeric     NOT NULL,
    basis       text        NOT NULL,
    source      text        NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (code, trade_date)
)
"""


def ensure_recompute_stage(conn: Any) -> None:
    """建暂存表（幂等）。"""
    with conn.cursor() as cur:
        cur.execute(_RECOMPUTE_DDL)
    conn.commit()


def load_recent_closes(conn: Any, code: str, *, limit: int = 800) -> tuple[tuple[int, float], ...]:
    """``(open_time_ms, close)`` **升序**，取最近 ``limit`` 根不复权 K 线。

    ⚠️ ``public.daily_bar`` **没有** ``open_time`` 列 —— 交易日的 ``open_time``
    是由 ``date`` 推出来的（UTC 当日零点毫秒），口径与
    :func:`cpt.adapters.a_share_local.fetch_validated_klines` 装配 ``CanonicalBar``
    时那一行 ``open_ms`` 完全一致。第一版这里直接 ``SELECT open_time``，
    在真机上当场炸 ``column "open_time" does not exist``。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT date, close FROM public.daily_bar WHERE code=%s ORDER BY date DESC LIMIT %s",
            (code, limit),
        )
        rows = cur.fetchall()
    out: list[tuple[int, float]] = []
    for day, close in rows:
        open_ms = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
        out.append((open_ms, float(close)))
    return tuple(sorted(out))


def codes_with_factors(conn: Any) -> tuple[str, ...]:
    """已有因子行的裸码（升序）—— 重算的候选全集。"""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT code FROM asel.ref_adjust_factor WHERE source IS NOT NULL")
        return tuple(sorted({str(r[0]).split(".")[0] for r in cur.fetchall()}))


def hot_pool_codes(conn: Any, *, limit: int = 60) -> tuple[str, ...]:
    """热门池（``public.hot_rank`` 最新一期）与策略源最新一期的裸码。

    重算要按额度分几天，所以**顺序有意义**：先跑正在被看的票。
    """
    out: list[str] = []
    with conn.cursor() as cur:
        cur.execute("SELECT max(date) FROM public.hot_rank")
        row = cur.fetchone()
        if row and row[0]:
            cur.execute("SELECT code FROM public.hot_rank WHERE date=%s", (row[0],))
            out.extend(str(r[0]).split(".")[0] for r in cur.fetchall())
        cur.execute("SELECT max(trade_date) FROM public.strategy_signal")
        row = cur.fetchone()
        if row and row[0]:
            cur.execute("SELECT code FROM public.strategy_signal WHERE trade_date=%s", (row[0],))
            out.extend(str(r[0]).split(".")[0] for r in cur.fetchall())
    seen: list[str] = []
    for c in out:
        if c and c not in seen:
            seen.append(c)
    return tuple(seen[:limit])


def save_recompute_factors(conn: Any, rows: Sequence[tuple[str, str, float, str, str]]) -> int:
    """写暂存表并 commit。``rows`` = ``(code, trade_date, factor, basis, source)``。"""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO {FACTOR_RECOMPUTE_TABLE}
                 (code, trade_date, hfq_factor, basis, source)
                 VALUES (%s,%s,%s,%s,%s)
                 ON CONFLICT (code, trade_date) DO UPDATE
                 SET hfq_factor=EXCLUDED.hfq_factor,
                     basis=EXCLUDED.basis,
                     -- ⚠️ 2026-10-23：必须一起更新 source。
                     -- 「因子沿用」写进去的行 source=carry_forward:xxx；
                     -- 若重算纠正了值却不改 source，这一格会一直挂着
                     -- 「沿用来的」标记，**事后查不出它到底准不准**。
                     source=EXCLUDED.source,
                     computed_at=now()""",
            rows,
        )
    conn.commit()
    return len(rows)


#: promote 判「这一行要不要改写」的容差。**实测的差值分布是两簇**（2026-10-15，
#: 暂存表 ⋈ 生产表 3.36M 行）::
#:
#:     >1e-9  1,806,097   浮点表示噪声（numeric↔double 的舍入路径不同）
#:     >1e-8     92,924   真实修正
#:     >1e-3     91,579   其中幅度 ≥1e-3 的（漂移/台阶方向那类结构性错）
#:
#: 取 ``1e-8``：比噪声底高一个数量级，又远低于最小真实修正。写 1e-9 每晚白写
#: 180 万行；写 1e-6 口径上没差别，但 1e-8 与 1e-3 之间没有第三簇，取哪儿都行。
_PROMOTE_EPS: Final[float] = 1e-8


def promote_recompute_factors(
    conn: Any, *, dry_run: bool = False, batch: int = 200_000
) -> tuple[int, int]:
    """把暂存表的重算因子搬进生产表 → ``(scanned, changed)``。

    ## 为什么需要它（R64，2026-10-15）

    ``asel.ref_adjust_factor_v2`` 自 R37 起只写不读（只有 ``factor_report.py`` 拿它
    对账），于是生产表最新那一档一直停在腾讯的逐日比值上：600519 的 2026-10-13 是
    ``6.3942``，而按公司行动重算的同一天属于 ``7.0605`` 那一档 —— 差 9.4%。快照读的
    是生产表，所以画出来的后复权序列在接缝处会跳。

    ## 安全性论证（实测，不是推断）

    5,206 只里 **5,021 只（97.6%）**在暂存表最新日上与生产表**逐位相等**
    （逐只算 ``v2/prod = 1.000000``）；123 只相差 < 1%（那些日期上生产表存的是腾讯
    比值）。两者同源同尺度 ⇒ 覆盖不会造成价格量级跳变。日线窗口只有 2024-01-02
    起，2023 年那些行不在暂存表里，原样保留。

    ## 只写有差异的行

    ⚠️ 判据必须是**带容差的数值比较**，不能写 ``IS DISTINCT FROM``：暂存表的
    ``hfq_factor`` 是 ``numeric``、生产表是 ``double precision``，PostgreSQL 会把
    double 提升成 numeric 再比 —— 同一个 Python float 存成 numeric 是
    ``12.4293652541882``、由 double 转回来是 ``12.4293652541882001…``。实测三种判据
    各自会改多少行（2026-10-15）::

        IS DISTINCT FROM（还比 source 字符串里的 steps=N）  2,248,057
        纯数值、容差 1e-9                                    1,806,097
        纯数值、容差 1e-8（= _PROMOTE_EPS）                     92,924

    真实修正只有最后那一簇（其中 91,579 行幅度 ≥1e-3）。3.4M 行里绝大多数算出来
    就是同一个值，跳过它们才不用每晚白写一百八十万行（WAL、索引膨胀都是代价）。
    用 ``(code, trade_date)`` 键集分批，避免一个事务里锁住全表。

    ``dry_run=True`` 只数不写 —— ``factor_recompute.py --promote-only --dry-run`` 依赖它。
    """
    diff = f"abs(v.hfq_factor::double precision - p.hfq_factor) > {_PROMOTE_EPS}"
    if dry_run:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT count(*),
                           count(*) FILTER (WHERE p.code IS NULL OR {diff})
                    FROM {FACTOR_RECOMPUTE_TABLE} v
                    LEFT JOIN asel.ref_adjust_factor p
                           ON p.code = v.code AND p.trade_date = v.trade_date"""
            )
            scanned, changed = cur.fetchone()
        return int(scanned), int(changed)

    scanned = changed = 0
    last_code, last_day = "", datetime(1900, 1, 1, tzinfo=UTC).date()
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"""WITH src AS (
                        SELECT code, trade_date, hfq_factor, source, basis, computed_at
                        FROM {FACTOR_RECOMPUTE_TABLE}
                        WHERE (code, trade_date) > (%s, %s)
                        ORDER BY code, trade_date
                        LIMIT %s
                    ), upd AS (
                        INSERT INTO asel.ref_adjust_factor
                            (code, trade_date, hfq_factor, source, source_url, source_ref,
                             as_of, available_at, fetched_at)
                        SELECT code, trade_date, hfq_factor, source, %s, basis,
                               computed_at, computed_at, now()
                        FROM src
                        ON CONFLICT (code, trade_date) DO UPDATE SET
                            hfq_factor=EXCLUDED.hfq_factor,
                            source=EXCLUDED.source,
                            source_url=EXCLUDED.source_url,
                            source_ref=EXCLUDED.source_ref,
                            as_of=EXCLUDED.as_of,
                            available_at=EXCLUDED.available_at,
                            fetched_at=EXCLUDED.fetched_at
                        WHERE abs(asel.ref_adjust_factor.hfq_factor - EXCLUDED.hfq_factor)
                                  > {_PROMOTE_EPS}
                        RETURNING 1
                    ), tail AS (
                        SELECT code, trade_date FROM src
                        ORDER BY code DESC, trade_date DESC LIMIT 1
                    )
                    SELECT (SELECT count(*) FROM src), (SELECT count(*) FROM upd),
                           (SELECT code FROM tail), (SELECT trade_date FROM tail)""",
                (last_code, last_day, batch, PROMOTE_SOURCE_URL),
            )
            row = cur.fetchone()
        conn.commit()
        if not row or row[0] is None:
            break
        scanned += int(row[0])
        changed += int(row[1] or 0)
        if row[2] is None:
            break
        last_code, last_day = str(row[2]), row[3]
    return scanned, changed


def factor_from_actions(
    actions: Sequence[Any],
    *,
    prev_closes: dict[str, float],
) -> dict[str, float]:
    """由公司行动按**定义**算后复权因子 → ``{除权日: 该日起生效的累计乘子}``。

    公式（从最新一次除权往前乘）::

        f = Π  (1 + 送转比例) / (1 − 每股派息 / 除权前收盘)

    ## 为什么不能继续用供应商的后复权序列相除

    现在的因子就是 ``腾讯后复权收盘 ÷ 库里不复权收盘`` 的**逐日比值**，而那个比值
    本身不是常数（R36 实测：600519 在 125 个交易日里相对极差 6.25%，000001 在
    20 天里 2.57%）。**拿一条带漂移的曲线当因子，等于把漂移固化下来。**
    公司行动是离散事件，事件才是真值。

    :param actions: :class:`~cpt.adapters.wind_source.CorporateAction` 序列。
    :param prev_closes: ``{除权日: 除权前收盘}``；取不到的那次会被跳过
        （**宁可少一个台阶，也不用猜的收盘价**）。
    :returns: 只含**除权日**上的因子（分段常数的分段点）。
    """
    out: dict[str, float] = {}
    acc = 1.0
    for act in sorted(actions, key=lambda a: a.ex_date, reverse=True):
        # 税前优先、税后回落：Wind 的列集合**按标的而变**（实测 600036 只给"税后"），
        # 少了任一种都不能让那一次除权凭空消失。
        cash = getattr(act, "cash_pre_tax", None)
        if cash is None:
            cash = getattr(act, "cash_after_tax", None)
        prev = prev_closes.get(act.ex_date)
        ratio = float(getattr(act, "share_ratio", 0.0) or 0.0)
        if cash is None or prev is None or prev <= 0:
            if ratio:
                acc *= 1.0 + ratio
                out[act.ex_date] = acc
            continue
        drop = float(cash) / prev
        if not 0 <= drop < 1:  # 派息不可能超过除权前收盘，否则是数据错
            _LOG.warning("跳过异常除权 %s：派息 %s / 前收 %s", act.ex_date, cash, prev)
            # R67（审计 P2-6）：**只丢这次现金，不能连送转一起丢**。
            # 一条 CorporateAction 可以同时带现金与 `share_ratio`（送转 + 派息同日）、
            # 而 Wind 的列集合按标的而变（`cash_pre_tax` 有时是 `cash_after_tax`
            # 的近似，见上面的 docstring）。坏现金只否定"派息"那一半 ——
            # 原来的 `continue` 会把**与该现金无关的送转台阶**一起丢掉，
            # 于是该除权日的因子偏小，并且从那天起**向后全错**（分段常数一旦错
            # 就不是局部误差）。这与上面 `cash is None` 分支的处置保持一致。
            if ratio:
                acc *= 1.0 + ratio
                out[act.ex_date] = acc
            continue
        acc *= (1.0 + ratio) / (1.0 - drop)
        out[act.ex_date] = acc
    return out
