#!/usr/bin/env python3
"""因子**沿用**：把暂存表里每票最新一档的因子，沿用到 ``daily_bar`` 的最新交易日。

## 为什么需要它（2026-10-23）

全市场重算（``factor_recompute.py``）在 **02:20 UTC（北京 10:20）** 跑，
而当天 K 线要等 ``emotion-core-daily``（北京 17:20 = 09:20 UTC，跑约 11.5 分钟）
才入库 ⇒ **重算的 target 永远停在前一天**。

结果：当天因子全市场缺失 ⇒ CPT 侧 ``price_ratio`` 取不出 ⇒
**A 股结构位整段退化为「不复权」**（实测 10-23 只有 1/5211 只由按需拉取补上）。

## 为什么「沿用」是精确的，不是近似

因子是**公司行动的台阶函数**：无除权事件的日期，因子值与前一日**逐位相同**
（实测 600519 的 6.39414671 连续多日不变，``source=eastmoney:events steps=13``）。
⇒ 把前一日的值沿用到当日，**与重算的结果逐位相同**（同一套事件源、同一套算法）。

⚠️ **但有除权事件的当天，因子会变**（实测 000002 在 2026-10-23：363.06 ≠ 10-22 的 367.84）。
这类票由**当天的全市场重算**纠正；沿用行会把 ``source`` 标成 ``carry_forward``，
事后可查"这一格当时用的是沿用值"。

## 两条安全阀

1. **只补「生产表当天没有」的票** —— 生产表当天已有的行来自按需拉取（腾讯比值），
   那是**实测更准**的值；沿用绝不能覆盖它（否则 promote 会把对的换成错的）。
2. **写暂存表，不直接写生产表** —— 搬运一律走既有的
   ``promote_recompute_factors()``（只写与生产表相差 > 1e-8 的行）。

用法::

    .venv/bin/python scripts/factor_carry_forward.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from cpt.adapters.a_share_factor import promote_recompute_factors

_LOG = logging.getLogger("factor_carry_forward")

#: 沿用行的来源标记（事后可查"这格是沿用来的"）
CARRY_SOURCE = "carry_forward"


def _target_date(conn: Any) -> str | None:
    """``daily_bar`` 的最新交易日 —— 沿用的目标日。

    由**数据**决定而不是日历：没入库的交易日算不出来。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT max(date) FROM public.daily_bar")
        row = cur.fetchone()
    return row[0].isoformat() if row and row[0] else None


def _latest_before(conn: Any, target: str) -> list[tuple[str, Any, str, str]]:
    """每票在 ``target`` 之前最新的一档因子：``(code, hfq_factor, basis, source)``。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (code) code, hfq_factor, basis, source
            FROM asel.ref_adjust_factor_v2
            WHERE trade_date < %s
            ORDER BY code, trade_date DESC
            """,
            (target,),
        )
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]


def _codes_present_on(conn: Any, table: str, target: str) -> set[str]:
    """某表在 ``target`` 当天已有的票（这些不参与沿用）。"""
    with conn.cursor() as cur:
        cur.execute(f"SELECT code FROM {table} WHERE trade_date = %s", (target,))  # noqa: S608
        return {r[0] for r in cur.fetchall()}


def carry_forward(conn: Any, *, dry_run: bool = False) -> tuple[int, int, int]:
    """执行沿用。返回 ``(目标候选数, 实际写入数, 跳过数)``。"""
    target = _target_date(conn)
    if target is None:
        _LOG.warning("public.daily_bar 里没有 K 线 ⇒ 本轮无事可做")
        return 0, 0, 0

    candidates = _latest_before(conn, target)
    if not candidates:
        _LOG.warning("暂存表里没有 %s 之前的因子 ⇒ 无可沿用（等全市场重算先跑一轮）", target)
        return 0, 0, 0

    # 安全阀①：生产表当天已有的行是按需拉的实测值，不许被沿用覆盖
    prod_has = _codes_present_on(conn, "asel.ref_adjust_factor", target)
    # 安全阀②：暂存表当天已有的行（若有）也不动
    stage_has = _codes_present_on(conn, "asel.ref_adjust_factor_v2", target)

    todo = [(c, f, b, s) for (c, f, b, s) in candidates if c not in prod_has and c not in stage_has]
    skipped = len(candidates) - len(todo)
    if prod_has:
        _LOG.info("生产表 %s 当天已有 %d 只（按需拉取，更准）⇒ 跳过", target, len(prod_has))
    if stage_has:
        _LOG.info("暂存表 %s 当天已有 %d 只 ⇒ 跳过", target, len(stage_has))

    if not todo:
        _LOG.info("没有可沿用的票（候选 %d 只全部已存在）", len(candidates))
        return len(candidates), 0, skipped

    if dry_run:
        _LOG.info("（dry-run）将沿用 %d 只到 %s", len(todo), target)
        return len(candidates), 0, skipped

    with conn.cursor() as cur:
        for code, factor, basis, source in todo:
            cur.execute(
                """
                INSERT INTO asel.ref_adjust_factor_v2
                    (code, trade_date, hfq_factor, basis, source, computed_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (code, trade_date) DO NOTHING
                """,
                (
                    code,
                    target,
                    factor,
                    basis,
                    # ⚠️ 先剥掉已有的 carry_forward: 前缀再写 ——
                    # 否则「重算失败的那天」会让前缀逐日叠加
                    # （carry_forward:carry_forward:...），标签会烂掉。
                    # 正常情况重算每天都会把暂存表的 source 改回 eastmoney:，
                    # 所以最多只叠一层；但那恰恰是重算失败的时候，不能指望它。
                    f"{CARRY_SOURCE}:{_strip_carry(source)}",
                ),
            )
        conn.commit()
    _LOG.info("已沿用 %d 只到 %s（跳过 %d 只）", len(todo), target, skipped)
    return len(candidates), len(todo), skipped


def _strip_carry(source: str) -> str:
    """剥掉已有的 ``carry_forward:`` 前缀（最多剥到只剩原来源）。"""
    prefix = CARRY_SOURCE + ":"
    while source.startswith(prefix):
        source = source[len(prefix) :]
    return source


def _fresh_conn(client: Any) -> Any:
    """关掉缓存连接并要一条**新的**。

    ``AShareLocalClient._conn`` 是缓存的 ⇒ 直接 ``_get_conn()`` 拿到的还是同一条，
    而它可能已经被服务端掐断（``the connection is closed``）。
    """
    old = getattr(client, "_conn", None)
    try:
        if old is not None:
            old.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        client._conn = None
    except Exception:  # noqa: BLE001
        pass
    return client._get_conn()  # noqa: SLF001


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把暂存表的因子沿用到 daily_bar 最新日（并 promote）")
    parser.add_argument("--dry-run", action="store_true", help="只数不写")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from cpt.adapters.a_share_local import AShareLocalClient

    client = AShareLocalClient()
    conn = client._get_conn()  # noqa: SLF001
    try:
        candidates, written, skipped = carry_forward(conn, dry_run=args.dry_run)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    if args.dry_run:
        print(f"\n  （dry-run）候选 {candidates} 只，跳过 {skipped} 只")
        return 0

    if written == 0 and candidates > 0:
        print(f"\n  没有可沿用的行（候选 {candidates} 只，跳过 {skipped} 只）")
        return 0

    # 搬运一律走既有 promote（只写与生产表相差 > 1e-8 的行）。
    # ⚠️ 必须用**新连接**：``client._get_conn()`` 返回的是**缓存**的那条 ——
    #    沿用阶段把它用坏之后（实测：RDS 掐断后 ``the connection is closed``），
    #    promote 会拿着同一条死连接去跑 345 万行的扫描，必挂。
    scanned = changed = 0
    for attempt in (1, 2):
        conn2 = _fresh_conn(client)
        try:
            scanned, changed = promote_recompute_factors(conn2, dry_run=False)
            break
        except Exception as exc:  # noqa: BLE001
            _LOG.error(
                "promote 失败（第 %d 次）：%s: %s", attempt, type(exc).__name__, exc
            )
            if attempt == 2:
                return 3
        finally:
            try:
                conn2.close()
            except Exception:  # noqa: BLE001
                pass

    print(f"\n  沿用 {written} 只 → 目标日；promote 扫描 {scanned} 行、改写 {changed} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
