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
import pathlib
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_CONFIG_FILE = pathlib.Path.home() / ".dbconfig"
SOURCE_TX = "tx:fqkline"
TX_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
UA = "Mozilla/5.0"
DEFAULT_KLINE_DAYS = 800  # 腾讯单次上限 801 根（实测 count=800 → 801 根，覆盖 2023-06 至今）

logger = logging.getLogger("factor_backfill")


# --------------------------------------------------------------------------- #
# DB 工具（与 asel.storage.dbconfig.connection_kwargs 同步自实现）
# --------------------------------------------------------------------------- #


def _read_dbconfig() -> dict[str, str]:
    if not DB_CONFIG_FILE.exists():
        return {}
    out: dict[str, str] = {}
    for line in DB_CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("$") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def connection_kwargs() -> dict[str, object]:
    cfg = _read_dbconfig()
    if not cfg.get("$RDSHOST") or not cfg.get("$DB_PW"):
        raise SystemExit(f"~/.dbconfig 缺失 $RDSHOST 或 $DB_PW。位置: {DB_CONFIG_FILE}")
    return {
        "host": cfg["$RDSHOST"],
        "port": int(cfg.get("$DBPORT", "5432")),
        "dbname": cfg.get("$DBNAME", "longkonglong"),
        "user": cfg.get("$USER", "postgres"),
        "password": cfg["$DB_PW"],
        "connect_timeout": 15,
    }


# --------------------------------------------------------------------------- #
# 腾讯 K 线 raw + hfq 同对 → 自洽因子
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FactorRow:
    code: str
    trade_date: str  # ISO date 'YYYY-MM-DD'
    hfq_factor: float
    source: str = SOURCE_TX
    source_ref: str | None = None


def _code_to_tx(code_wind: str) -> tuple[str, str]:
    """``600519.SH`` → ``('sh600519', '600519')``；纯裸码则按首位推断市场。

    失败抛 ``ValueError``（永久错误，不重试）。
    """
    if "." in code_wind:
        base, ex = code_wind.split(".", 1)
        return f"{ex.lower()}{base}", base
    if code_wind.startswith(("6", "9", "5")):
        return f"sh{code_wind}", code_wind
    if code_wind.startswith(("0", "2", "3")):
        return f"sz{code_wind}", code_wind
    if code_wind.startswith(("4", "92")):
        return f"bj{code_wind}", code_wind
    raise ValueError(f"无法推断市场前缀: {code_wind}")


def _fetch_tx_pair(tx_sym: str, days: int) -> tuple[list[list[str]], list[list[str]]]:
    """返回 (raw, hfq)，二者同源同对。"""

    def one(adj: str) -> list[list[str]]:
        url = f"{TX_ENDPOINT}?param={tx_sym},day,,,{days},{adj}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("gbk", errors="replace")
        if text.startswith("<") or "http-equiv" in text.lower():
            raise RuntimeError(f"腾讯反爬（HTML 头）：{text[:80]!r}")
        d = json.loads(text)
        block = d["data"][tx_sym]
        key = f"{adj}day" if adj else "day"
        return block[key]

    return one(""), one("hfq")


def fetch_tx_factor_rows(code_wind: str, *, days: int = DEFAULT_KLINE_DAYS) -> list[FactorRow]:
    """拉一只票最近 N 天 raw+hfq，算每日 ``hfq_factor = hfq/raw``。"""

    tx_sym, _ = _code_to_tx(code_wind)
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
                source_ref=f"web.ifzq.gtimg.cn fqkline day/hfq {date_str}",
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
            hot_date = cur.fetchone()[0]
            cur.execute("SELECT code FROM public.hot_rank WHERE date = %s", (hot_date,))
            hot = {r[0] for r in cur.fetchall()}

            cur.execute("SELECT max(date) FROM public.ladder_day")
            lad_date = cur.fetchone()[0]
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


def latest_factor_date(conn, code: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(trade_date) FROM asel.ref_adjust_factor WHERE code=%s",
            (code,),
        )
        d = cur.fetchone()[0]
    return d.isoformat() if d else None


def upsert_factor_rows(conn, rows: Iterable[FactorRow], dry_run: bool) -> int:
    """幂等 upsert 整批因子行（主键 ``(code, trade_date)``）。

    用 ``executemany`` 批量写入——**不要**逐行 ``execute``：每只票 800 行、
    94 只就是 7.5 万条语句，逐行会慢到不可用（实测）。
    """
    now = datetime.now(UTC)
    payload = [
        (
            row.code,
            row.trade_date,
            row.hfq_factor,
            row.source,
            TX_ENDPOINT,
            row.source_ref,
            now,  # as_of
            now,  # available_at
            now,  # fetched_at
        )
        for row in rows
    ]
    if dry_run:
        return len(payload)
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO asel.ref_adjust_factor
                 (code, trade_date, hfq_factor, source, source_url, source_ref,
                  as_of, available_at, fetched_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (code, trade_date) DO UPDATE SET
                 hfq_factor=EXCLUDED.hfq_factor,
                 source=EXCLUDED.source,
                 source_url=EXCLUDED.source_url,
                 source_ref=EXCLUDED.source_ref,
                 fetched_at=EXCLUDED.fetched_at""",
            payload,
        )
    conn.commit()
    return len(payload)


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
            except ValueError as e:
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
            n = upsert_factor_rows(conn, rows, args.dry_run)
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
