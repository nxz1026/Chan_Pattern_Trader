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
from typing import Any

from cpt.adapters._dbconfig import connection_kwargs as _shared_connection_kwargs
from cpt.adapters.a_share_factor import FactorRow, factor_source_ref, upsert_factor_rows
from cpt.adapters.a_share_public import (
    TENCENT_KLINE_URL,
    ASharePublicError,
    normalize_code,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 端点与 ``source`` 取值都取自 cpt 的**唯一**定义，脚本不再各留一份。
#: 2026-09-25 之前脚本自带 ``TX_ENDPOINT``（与 ``TENCENT_KLINE_URL`` 逐字相同）与
#: ``SOURCE_TX = "tx:fqkline"``（适配器写 ``tencent_fqkline``）—— 同一个腾讯接口
#: 在库里分裂成两个 ``source``，实测 49,730 行 vs 7,200 行。存量行已于 2026-09-25
#: 迁移完毕（库里现只有 ``tx:fqkline``）。
UA = "Mozilla/5.0"
DEFAULT_KLINE_DAYS = 800  # 腾讯单次上限 801 根（实测 count=800 → 801 根，覆盖 2023-06 至今）

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
    """

    tx_sym = normalize_code(code_wind)
    raw, hfq = _fetch_tx_pair(tx_sym, days)
    raw_map = {r[0]: float(r[2]) for r in raw}  # date -> raw close
    hfq_map = {r[0]: float(r[2]) for r in hfq}
    rows: list[FactorRow] = []
    for date_str in sorted(set(raw_map) & set(hfq_map)):
        r, h = raw_map[date_str], hfq_map[date_str]
        if r == 0:
            continue
        rows.append(
            FactorRow(
                code=code_wind,
                trade_date=date_str,
                hfq_factor=h / r,
                source_ref=factor_source_ref(date_str),
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# 代码候选
# --------------------------------------------------------------------------- #


def list_all_a_codes() -> list[str]:
    """全市场代码（从 daily_bar_raw 去重）。"""
    import psycopg  # noqa: PLC0415

    with psycopg.connect(**connection_kwargs()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT code FROM asel.daily_bar_raw")
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


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A 股复权因子 backfill（腾讯财经）")
    parser.add_argument("--mode", choices=("full", "incremental"), default="incremental")
    parser.add_argument("--code-prefix", help="只处理以此开头的代码")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--sleep-ms", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
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
        "模式=%s 待处理=%d 前缀=%s dry_run=%s sleep=%dms",
        args.mode,
        len(codes),
        args.code_prefix or "(all)",
        args.dry_run,
        args.sleep_ms,
    )
    if not codes:
        logger.info("无代码可处理，退出。")
        return 0

    import psycopg  # noqa: PLC0415

    total = 0
    fail_count = 0
    with psycopg.connect(**connection_kwargs()) as conn:
        for idx, code in enumerate(codes, 1):
            try:
                rows = fetch_tx_factor_rows(code)
            except (ValueError, ASharePublicError) as e:
                # 永久错误：代码本身不认识/无法推断市场 —— 重试没有意义，直接跳过。
                # ``normalize_code`` 抛的是 ``ASharePublicError``（基类是 ``RuntimeError``
                # 而不是 ``ValueError``），必须显式列出；这条要在下面那条
                # ``RuntimeError`` 之前，否则会被归类成"可重试的网络失败"。
                logger.info("跳过 %s: %s", code, str(e)[:100])
                fail_count += 1
                time.sleep(args.sleep_ms / 1000)
                continue
            except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as e:
                logger.warning("腾讯因子失败 %s: %s: %s", code, type(e).__name__, str(e)[:100])
                fail_count += 1
                time.sleep(args.sleep_ms / 1000)
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning("未知错误 %s: %s: %s", code, type(e).__name__, str(e)[:100])
                fail_count += 1
                time.sleep(args.sleep_ms / 1000)
                continue
            if not rows:
                time.sleep(args.sleep_ms / 1000)
                continue
            latest_existing = latest_factor_date(conn, code)
            if latest_existing:
                rows = [r for r in rows if r.trade_date > latest_existing]
                if not rows:
                    time.sleep(args.sleep_ms / 1000)
                    continue
            n = upsert_factor_rows(conn, rows, source_url=TENCENT_KLINE_URL, dry_run=args.dry_run)
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
