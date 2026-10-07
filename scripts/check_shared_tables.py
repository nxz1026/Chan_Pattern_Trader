#!/usr/bin/env python3
"""共享 A 股表的新鲜度检查（CPT 是消费方，报警也该由消费方负责）。

## 为什么需要它

``emotion_core`` 库里 7 张 A 股表由 **emotion-core 拥有并写入**，CPT **只读**。
零代码依赖、单向数据流是当前架构的正确形态 —— 但它有个致命前提：
**没人保证上游真的在写**。

2026-10-07 上线审计实测：Oracle 上 ``emotion-core-daily`` / ``-strategy``
两个 timer 都是 inactive/disabled，全机 timer + active service + crontab
枚举下来**没有任何东西会写** ``daily_bar``。当时看不出来，是因为国庆休市
（交易日历 10-01~10-07 全 ``is_open=False``），所以「数据停在 09-30」是
正确的；**等 10-08 开盘就会静默断更**。

CPT 自己的 A 股快照里**已经有** ``data_quality.severity``（stale/gap/ok），
但那是给看板上的人看的 —— 没人盯着面板，就等于没有。

⇒ 这道检查把「上游断了」变成一条**会被日报发到飞书的失败**。

## 判定规则（休市必须算对，否则假期天天误报）

「期望的最新交易日」= **收盘时刻已过的最近一个交易日**：

- 今天不是交易日（周末/节假日）⇒ 期望 = 上一个交易日（休市期间数据本来就该停）
- 今天是交易日但**还没收盘**（北京时间 < 15:00）⇒ 期望 = 上一个交易日
- 今天是交易日且**已收盘**（北京时间 >= 15:00）⇒ 期望 = 今天

``trade_calendar`` 本身单独判：只要覆盖到今天即算新鲜（它是预生成的，
实测已铺到年底）。

## 退出码

- ``0`` 全部新鲜
- ``1`` 有表缺失或过期（**报警**）
- ``2`` 连不上库 / 查询出错（**报警**——「查不到」和「查到是新的」不能同码，
  这与 ``run_metric_store.prune`` 的失败语义同源）
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from typing import Final
from zoneinfo import ZoneInfo

#: A 股收盘时刻（北京时间）。收盘**前**不算「今天应该有数据」。
MARKET_TZ: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE: Final[dt.time] = dt.time(15, 0)

#: CPT 读取、由 emotion-core 写入的共享表 → 该表「最新数据日期」列名。
#: 与 docs/shared-tables-contract.md 一一对应；改这里必须同步改那份契约
#: （有测试双向钉住，见 tests/test_shared_tables_contract.py）。
SHARED_TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("daily_bar", "date"),
    ("derived_bar", "date"),
    ("hot_rank", "date"),
    ("ladder_day", "date"),
    ("limit_pool_em", "date"),
    ("strategy_signal", "trade_date"),
)

#: 交易日历表：只要覆盖到今天就算新鲜（预生成，不随行情更新）。
CALENDAR_TABLE: Final[str] = "trade_calendar"


def expected_trade_day(now: dt.datetime, calendar: list[tuple[dt.date, bool]]) -> dt.date | None:
    """收盘时刻已过的最近一个交易日；日历为空时返回 ``None``。"""
    today_local = now.astimezone(MARKET_TZ).date()
    open_days = sorted(d for d, is_open in calendar if is_open and d <= today_local)
    if not open_days:
        return None
    if today_local not in {d for d, _ in calendar if _} and not any(
        d == today_local and is_open for d, is_open in calendar
    ):
        # 今天不是交易日 ⇒ 期望上一个交易日
        return open_days[-1]
    passed_close = now.astimezone(MARKET_TZ).time() >= MARKET_CLOSE
    if passed_close:
        return today_local
    return open_days[-2] if len(open_days) >= 2 else open_days[-1]


def check(conn) -> tuple[list[str], list[str], dt.date | None]:
    """返回 (问题列表, 状态行列表, 期望交易日)。"""
    problems: list[str] = []
    lines: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT date, is_open FROM public.{CALENDAR_TABLE} "
            f"WHERE date >= CURRENT_DATE - INTERVAL '10 days' "
            f"ORDER BY date"
        )
        calendar = [
            (r[0].date() if hasattr(r[0], "date") else r[0], bool(r[1])) for r in cur.fetchall()
        ]
        expected = expected_trade_day(dt.datetime.now(dt.UTC), calendar)

        cur.execute(f"SELECT max(date) FROM public.{CALENDAR_TABLE}")
        cal_max = cur.fetchone()[0]
        today_local = dt.datetime.now(dt.UTC).astimezone(MARKET_TZ).date()
        if cal_max is None or cal_max < today_local:
            problems.append(f"{CALENDAR_TABLE} 覆盖到 {cal_max}，未覆盖今天 {today_local}")
            lines.append(f"  ✗ {CALENDAR_TABLE}: max={cal_max}")
        else:
            lines.append(f"  ✓ {CALENDAR_TABLE}: 覆盖到 {cal_max}")

        for table, col in SHARED_TABLES:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            if cur.fetchone()[0] is None:
                problems.append(f"{table} 表不存在")
                lines.append(f"  ✗ {table}: 表不存在")
                continue
            cur.execute(f"SELECT max({col}) FROM public.{table}")
            raw = cur.fetchone()[0]
            latest = raw.date() if hasattr(raw, "date") else raw
            if latest is None:
                problems.append(f"{table} 一行数据都没有")
                lines.append(f"  ✗ {table}: 空表")
            elif expected is not None and latest < expected:
                problems.append(
                    f"{table} 最新 {latest}，落后于期望交易日 {expected}"
                    f"（缺 {(expected - latest).days} 天）"
                )
                lines.append(f"  ✗ {table}: 最新 {latest}（期望 ≥ {expected}）")
            else:
                lines.append(f"  ✓ {table}: 最新 {latest}")
    return problems, lines, expected


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="只在有问题时输出")
    a = ap.parse_args(argv)

    try:
        import psycopg
        from cpt.adapters._dbconfig import connection_kwargs
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 依赖导入失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        conn = psycopg.connect(**connection_kwargs())
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 连不上库: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        problems, lines, expected = check(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 查询失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if problems or not a.quiet:
        print(f"共享表新鲜度（期望交易日 {expected}）：")
        for line in lines:
            print(line)
    if problems:
        print(f"\n发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("  全部新鲜。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
