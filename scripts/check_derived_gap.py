#!/usr/bin/env python3
"""门禁⑱：**``derived_bar`` 的缺口只许是上市首日**（R90 新增）。

## 为什么要这道门

``public.derived_bar`` 由 **emotion-core 的 ``derive_service`` 写入**，CPT 只读。
2026-10-22 审计发现它比 ``daily_bar`` 少 221 行，长期被当成「未知缺口」挂着。

查清了：**221 条全是每只票的上市首日**（``date = min(date)`` per code，221/221，
非首日 0 条）。原因是 ``derive_service._load_bars`` 用 45 天窗口重放
``compute_derived`` 算 ``cont_days``（连板数）与 ``amplitude``（振幅）——
新股首日没有前一根，算不出来，跳过是对的。

但「差 221」这个数字本身不带信息：**同样是 221，可能是首日，也可能是上游
昨天整批断更**。人眼分不出，面板上也看不出来 —— 直到某次回测被涨停标记
缺失带偏。

⇒ 这道检查把「差多少」换成「差的都是什么」：

- 缺口 ∈ {上市首日} ⇒ 绿，且把条数报出来（221 是稳定值，变多才可疑）；
- 出现**任何**非首日缺口 ⇒ 红，并列出前几条 ``code / date``。

## 为什么要连库，而其它门禁不连

与 ``check_shared_tables.py`` 同类：判据是**数据事实**，不是源码事实，CI 的
干净库答不了。所以它按「运维门禁」定位 —— 由 ``cron_daily_report.py`` 每天
跑一次，红了发飞书；CI 里**不跑**（``check_gate_coverage.py`` 的白名单里
已登记理由）。

## 退出码

- ``0`` 缺口全是上市首日（或没有缺口）
- ``1`` 有非上市首日缺口（**报警**）
- ``2`` 连不上库 / 查询出错（**报警** —— 「查不到」与「查了是好的」不能同码，
  与 ``check_shared_tables.py`` 同源）

## 探针

    python scripts/check_derived_gap.py
    python scripts/check_derived_gap.py --quiet
    python scripts/check_derived_gap.py --max-nonfirst 0   # 测红用
"""

from __future__ import annotations

import argparse
import sys

#: 非首日缺口超过这个条数就报红。默认 0 —— 首日缺口是已知稳定形态，其它一条都不许有。
_DEFAULT_MAX_NONFIRST = 0

#: 报红时最多列几条样例，够定位就行。
_SAMPLE_LIMIT = 10

_GAP_SQL = """
WITH firsts AS (
    SELECT code, min(date) AS first_date FROM public.daily_bar GROUP BY code
),
gap AS (
    SELECT b.code, b.date, firsts.first_date
      FROM public.daily_bar b
      LEFT JOIN public.derived_bar d ON d.code = b.code AND d.date = b.date
      JOIN firsts ON firsts.code = b.code
     WHERE d.code IS NULL
)
SELECT
    count(*) FILTER (WHERE date = first_date) AS first_day,
    count(*) FILTER (WHERE date <> first_date) AS other_day
  FROM gap
"""

_GAP_SAMPLES_SQL = """
WITH firsts AS (
    SELECT code, min(date) AS first_date FROM public.daily_bar GROUP BY code
)
SELECT b.code, b.date
  FROM public.daily_bar b
  LEFT JOIN public.derived_bar d ON d.code = b.code AND d.date = b.date
  JOIN firsts ON firsts.code = b.code
 WHERE d.code IS NULL AND b.date <> firsts.first_date
 ORDER BY b.date DESC, b.code
 LIMIT %s
"""


def check(conn: object) -> tuple[list[str], list[str], dict[str, int]]:
    """跑判据。

    Args:
        conn: ``psycopg`` 连接。

    Returns:
        ``(problems, lines, counts)``，``counts`` 有 ``first_day`` / ``other_day``。
    """
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(_GAP_SQL)
        row = cur.fetchone()
    first_day = int(row[0] or 0) if row else 0
    other_day = int(row[1] or 0) if row else 0
    counts = {"first_day": first_day, "other_day": other_day}

    lines = [
        f"  上市首日缺口 {first_day} 条（已知形态，不算问题）",
        f"  非上市首日缺口 {other_day} 条",
    ]
    problems: list[str] = []
    if other_day > _DEFAULT_MAX_NONFIRST:
        samples: list[str] = []
        try:
            with conn.cursor() as cur:  # type: ignore[attr-defined]
                cur.execute(_GAP_SAMPLES_SQL, (_SAMPLE_LIMIT,))
                samples = [f"{code} {date}" for code, date in cur.fetchall()]
        except Exception:  # noqa: BLE001 - 样例拿不到不影响判红
            samples = []
        if samples:
            lines.append("  样例：")
            lines.extend(f"    · {s}" for s in samples)
        problems.append(
            f"derived_bar 有 {other_day} 条非上市首日缺口 —— 上游 derive 断了，"
            "涨停标记会缺，回测会被带偏"
        )
    return problems, lines, counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="derived_bar 缺口只许是上市首日")
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
        problems, lines, _counts = check(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 查询失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if problems or not a.quiet:
        print("derived_bar 缺口：")
        for line in lines:
            print(line)
    if problems:
        print(f"\n发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    if not a.quiet:
        print("  ✅ 缺口全是上市首日。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
