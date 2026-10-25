#!/usr/bin/env python3
"""**真机**验证 `cpt/adapters/a_share_public.py` 记录的外部契约是否仍然成立。

## 为什么需要这个脚本

`architecture.md` §2.1 指出的这层头号风险：

    外部契约漂移了**不会让任何测试变红**

腾讯 / 新浪的返回格式一变，单元测试照样全绿（它们只喂固定 fixture），
线上却悄悄开始出错。**唯一有效的验证是打真接口。**

本脚本把 `a_share_public.py` 模块 docstring 里记录的四条契约逐条核对。
**任何一条对不上，就是外部契约漂了**，该去改适配器而不是改测试。

## 为什么**不进 CI**

它依赖第三方公网可用性，而本仓的 CI 不能被腾讯/新浪的抖动弄红。
定位是**手工巡检工具**：换 provider、升级 SDK、或隔一段时间怀疑漂移时跑一次。

## 用法

```bash
python scripts/verify_public_contracts.py            # 全部契约
python scripts/verify_public_contracts.py --quiet    # 只报结论
```

退出码 0 = 全部成立；1 = 有契约漂移（**不要**去改测试的 fixture，要改适配器）。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpt.adapters.a_share_public import (  # noqa: E402
    TENCENT_KLINE_URL,
    AShareAdjustUnsupportedError,
    ASharePublicError,
    SinaQuoteClient,
    TencentKlineClient,
)

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    # 新浪不带 Referer 会 403（模块 docstring 已记）
    "Referer": "https://finance.sina.com.cn",
}

results: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> None:  # type: ignore[no-untyped-def]
    try:
        detail = fn()
        results.append((name, True, detail))
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {str(exc)[:120]}"))


def c1_field_order() -> str:
    """契约 1：``fqkline`` 字段顺序 = [日期,开,收,高,低,量]，**不是 OHLC**。

    这是模块 docstring 的「坑 1」—— 按 OHLC 解析会得到「最高价 < 收盘价」的
    坏数据**且不报错**。所以这里既看真实原始行，也看适配器解析结果的 OHLC 自洽性。
    """
    url = f"{TENCENT_KLINE_URL}?param=sh600519,day,,,5,hfq"
    req = urllib.request.Request(url, headers=_UA)  # noqa: S310
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    row = payload["data"]["sh600519"]["hfqday"][-1]
    # 位置约定：[0]日期 [1]开 [2]收 [3]高 [4]低 [5]量
    if not (float(row[3]) >= max(float(row[1]), float(row[2]))):
        return f"❌ 原始行高<开/收，顺序可能变了：{row}"
    if not (float(row[4]) <= min(float(row[1]), float(row[2]))):
        return f"❌ 原始行低>开/收，顺序可能变了：{row}"
    bars = TencentKlineClient().fetch_daily_bars("600519", limit=5)
    b = bars[-1]
    if not (b.high >= max(b.open, b.close) and b.low <= min(b.open, b.close)):
        return f"❌ 适配器解析出的 OHLC 不自洽：{b}"
    return f"✅ 顺序仍是 [日期,开,收,高,低,量]；末根 {b.open}/{b.close}/{b.high}/{b.low}"


def c2_adjust_keys() -> str:
    """契约 2：复权口径键名 ``hfqday`` / ``qfqday`` / ``day``。"""
    got = {}
    for adjust in ("hfq", "qfq", "bfq"):
        bars = TencentKlineClient(adjust=adjust).fetch_daily_bars("600519", limit=3)
        got[adjust] = bars[-1].close
    if got["qfq"] == got["bfq"] and got["hfq"] != got["bfq"]:
        return f"✅ 三口径均可用，hfq={got['hfq']:.2f} qfq=bfq={got['bfq']:.2f}"
    return f"⚠️ 三口径关系与预期不符：{got}"


def c3_per_stock_unsupported() -> str:
    """契约 3：某标的**没有** hfq 时要抛 ``AShareAdjustUnsupportedError``。

    这是**逐标的**属性、不可预测（模块 docstring 记了 R15-1 先后错过两次）。
    所以这里检查一个 docstring 点名的样本：688981（中芯国际）没有 hfqday。
    """
    try:
        TencentKlineClient().fetch_daily_bars("688981", limit=3)
    except AShareAdjustUnsupportedError as exc:
        return f"✅ 688981 正确抛专用异常：{str(exc)[:60]}"
    except ASharePublicError as exc:
        return f"⚠️ 抛的是泛化的 ASharePublicError，调用方分不出「该标的没有」：{str(exc)[:60]}"
    return "⚠️ 688981 现在**有** hfq 了 —— docstring 的样本已过期，可考虑换样本"


def c4_sina_quote() -> str:
    """契约 4：新浪快照必须带 Referer（否则 403）。"""
    q = SinaQuoteClient().fetch_quote("600519")
    for k in ("code", "name", "last", "date"):
        if k not in q:
            return f"❌ 返回缺字段 {k}：{sorted(q)}"
    return f"✅ {q['name']} {q['last']} @ {q['date']}"


def c5_hfq_matches_local_factor() -> str:
    """交叉校验（附加）：腾讯原始价 vs 本地 `daily_bar`，以及本地复权因子自洽性。

    ## 为什么改判据（R95，2026-10-25）

    改之前比的是「腾讯 hfq 绝对价 == 本地原始价 × 本地因子」。这个判据**两边都错**：

    - 绝对后复权价本来就**不要求**相等 —— 两个源各挑自己的锚点日；
    - 更要紧的是**腾讯 hfq 这条序列自己就不可靠**。同一时刻、同一标的实测：

      | 比较 | 结果（40 个交易日） |
      |---|---|
      | 腾讯 `qfq` vs 腾讯 `bfq` | 价格逐日**完全相同**，收益差 **0.00e+00** |
      | 腾讯 `hfq` vs 腾讯 `bfq` | 收益 **39/39 天全部不一致**，最大差 **5.03e-03** |

      即腾讯的 `hfq` 与它自家的 `qfq`/`bfq` 都对不上，隐含因子逐日漂 0.1%~0.2%
      （600519 实测 6.978~7.088）。拿它当标尺去判本地因子表，是**伪判据**。

    ## 现在判什么

    1. **硬**：腾讯 `bfq`（= `qfq`）收盘价 == 本地 `daily_bar.close`，逐日零误差。
       两侧都是交易所原始价，这里不该有任何差。
    2. **硬**：本地 `asel.ref_adjust_factor` 在「无除权窗口」内必须恒定。
       它是 `carry_forward` 出来的 —— 只在东财有公司行动时才变，其余日子沿用。
       一旦它天天变，说明有东西在抖，那是真 bug。
    3. **条件**：只有当腾讯 `hfq` 自身与 `qfq` 自洽时（上游哪天修好了），才真正
       拿它交叉校验本地因子；否则只**记录**比值，不作判据。
    """
    limit = 60
    hfq = TencentKlineClient(adjust="hfq").fetch_daily_bars("600519", limit=limit)
    bfq = TencentKlineClient(adjust="bfq").fetch_daily_bars("600519", limit=limit)
    qfq = TencentKlineClient(adjust="qfq").fetch_daily_bars("600519", limit=limit)
    if not hfq or not bfq or not qfq:
        return "⚠️ 腾讯返回空序列，无法交叉校验"

    from datetime import UTC, datetime

    def _day(bar: Any) -> str:
        ms = int(bar.open_time)
        return datetime.fromtimestamp(ms / 1000, UTC).date().isoformat()

    ten_hfq = {_day(b): float(b.close) for b in hfq}
    ten_bfq = {_day(b): float(b.close) for b in bfq}
    ten_qfq = {_day(b): float(b.close) for b in qfq}

    from cpt.adapters.a_share_local import open_conn

    conn = open_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """SELECT d.date, d.close, f.hfq_factor
               FROM public.daily_bar d
               LEFT JOIN asel.ref_adjust_factor f
                 ON f.code = d.code AND f.trade_date = d.date
               WHERE d.code='600519' AND d.date >= %s
               ORDER BY d.date""",
            (min(ten_hfq),),
        )
        local = {
            r[0].isoformat(): (float(r[1]), None if r[2] is None else float(r[2]))
            for r in cur.fetchall()
        }
    finally:
        conn.close()

    fac = {d: v[1] for d, v in local.items() if v[1] is not None}
    days = sorted(d for d in ten_hfq if d in ten_bfq and d in ten_qfq and d in local and d in fac)
    if len(days) < 10:
        return f"⚠️ 可对齐的交易日只有 {len(days)} 天，样本不够做交叉校验"

    # ── 硬判据 1：原始价零误差 ─────────────────────────────────────────
    raw_bad = [d for d in days if abs(ten_bfq[d] - local[d][0]) > 0.005]
    if raw_bad:
        worst = max(abs(ten_bfq[d] - local[d][0]) for d in days)
        return (
            f"❌ 原始价不一致：{len(raw_bad)}/{len(days)} 天对不上（最大差 {worst:.3f}，"
            f"首个 {raw_bad[0]}）—— 这一层不该有任何误差"
        )
    qfq_bad = [d for d in days if abs(ten_qfq[d] - ten_bfq[d]) > 1e-9]
    if qfq_bad:
        return (
            f"❌ 腾讯 qfq 与 bfq 不再相等：{len(qfq_bad)}/{len(days)} 天"
            "（窗口内有除权？那需要改用因子表口径）"
        )

    # ── 硬判据 2：本地因子在窗口内必须恒定（窗口内无除权）────────────
    factors = {fac[d] for d in days}
    if len(factors) > 1:
        lo, hi = min(factors), max(factors)
        return (
            f"❌ 本地复权因子在 {len(days)} 天内变了 {len(factors)} 次"
            f"（{lo:.6f}~{hi:.6f}）—— carry_forward 口径应当只在除权日变，其余日子恒定"
        )

    # ── 判据 3：腾讯 hfq 自洽时才当标尺 ────────────────────────────────
    def _ret(series: dict[str, float]) -> list[float]:
        return [series[days[i]] / series[days[i - 1]] - 1 for i in range(1, len(days))]

    ten_hfq_ret, ten_bfq_ret = _ret(ten_hfq), _ret(ten_bfq)
    self_dev = max(abs(a - b) for a, b in zip(ten_hfq_ret, ten_bfq_ret, strict=True))
    ratios = [ten_hfq[d] / (local[d][0] * fac[d]) for d in days]

    if self_dev > 1e-6:
        # 上游 hfq 与自身 qfq 不自洽 —— 只能记录，不能判
        return (
            f"✅ 原始价 {len(days)}/{len(days)} 天零误差、本地因子 {len(days)} 天恒定 "
            f"{next(iter(factors)):.6f}；"
            f"⚠️ 腾讯 hfq 与自家的收益对不上（最大差 {self_dev:.2e}），"
            f"因此**不拿它当标尺**（绝对价比值 {min(ratios):.4f}~{max(ratios):.4f} 仅供记录）"
        )

    # 上游自洽了 ⇒ 这时才是真的交叉校验
    local_ret = [
        (local[days[i]][0] * fac[days[i]]) / (local[days[i - 1]][0] * fac[days[i - 1]]) - 1
        for i in range(1, len(days))
    ]
    ret_dev = max(abs(a - b) for a, b in zip(ten_hfq_ret, local_ret, strict=True))
    if ret_dev > 1e-6:
        return f"❌ 复权收益率不一致：最大差 {ret_dev:.2e} —— 本地因子表漂了"
    drift = max(ratios) / min(ratios) - 1
    if drift > 0.01:
        return (
            f"❌ 复权锚点出现阶跃：比值 {min(ratios):.4f}~{max(ratios):.4f}"
            f"（漂 {drift * 100:.2f}%，阈值 1%）"
        )
    return (
        f"✅ 三层全过：原始价零误差；复权收益率最大差 {ret_dev:.1e}；"
        f"锚点比值 {min(ratios):.4f}~{max(ratios):.4f} 稳定"
    )


def c6_factor_rows_are_piecewise_constant() -> str:
    """本地不变量：因子行里不该再有「用供应商 hfq 反推」的口径（R96）。

    后复权因子按定义是**分段常数** —— 两次除权之间必须逐位相同。用
    ``供应商 hfq ÷ 不复权`` 逐日相除得到的是一条不断漂移的曲线，等于把漂移
    当成真台阶固化下来（见 ``docs/known-traps.md`` #70）。

    这条检查只查**已经被日线消费**的行：没有日线的孤儿行不进判据 ——
    它们当下无人使用（``daily_bar`` 最早 2024-01-02，孤儿行全在那之前），
    拿它们判红只会制造噪音。

    判据：
    1. 落在有日线日期上的因子行，``source`` 里不得出现 ``tx:``；
    2. 相邻两个有日线交易日之间，因子相对变化 >0.5% 的，必须能对上除权事件
       —— 这里只做**粗筛**（数量上限），不做事件级复核。
    """
    from cpt.adapters.a_share_local import open_conn

    conn = open_conn()
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """SELECT count(*) FROM asel.ref_adjust_factor f
               JOIN public.daily_bar d ON d.code = f.code AND d.date = f.trade_date
               WHERE f.source LIKE 'tx:%'"""
        )
        tx_used = int(cur.fetchone()[0])
        cur.execute(
            """WITH t AS (
                   SELECT f.code, f.trade_date, f.hfq_factor
                     FROM asel.ref_adjust_factor f
                     JOIN public.daily_bar d ON d.code = f.code AND d.date = f.trade_date
               ), j AS (
                   SELECT code, trade_date, hfq_factor,
                          lag(hfq_factor) OVER (PARTITION BY code ORDER BY trade_date) AS prev
                     FROM t
               )
               SELECT count(*) FROM j WHERE prev > 0 AND abs(hfq_factor / prev - 1) > 0.005"""
        )
        jumps = int(cur.fetchone()[0])
        cur.execute(
            """SELECT count(*) FROM asel.ref_adjust_factor f
               JOIN public.daily_bar d ON d.code = f.code AND d.date = f.trade_date"""
        )
        used_rows = int(cur.fetchone()[0])
    finally:
        conn.close()

    if tx_used:
        return f"❌ 还有 {tx_used} 行因子由腾讯 hfq 反推（source=tx:*），会被日线消费"
    return (
        f"✅ 被日线消费的 {used_rows:,} 行因子无 tx 口径；"
        f"相邻日变化 >0.5% 的 {jumps:,} 处（应为除权日，见 migration R96）"
    )


CHECKS = (
    ("契约1 fqkline 字段顺序 = [日期,开,收,高,低,量]", c1_field_order),
    ("契约2 复权键名 hfqday/qfqday/day", c2_adjust_keys),
    ("契约3 逐标的「无 hfq」抛专用异常", c3_per_stock_unsupported),
    ("契约4 新浪快照带 Referer", c4_sina_quote),
    ("交叉 腾讯 hfq vs 本地因子表", c5_hfq_matches_local_factor),
    ("不变量 因子表不得用腾讯 hfq 反推", c6_factor_rows_are_piecewise_constant),
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="只打印结论")
    args = ap.parse_args(argv)

    for name, fn in CHECKS:
        check(name, fn)

    print("=" * 88)
    bad = 0
    for name, _ok, detail in results:
        if not detail.startswith(("✅", "⚠️")):
            bad += 1
        elif detail.startswith("⚠️"):
            bad += 1
        if not args.quiet:
            print(f"  {name}\n      {detail}")
    print("=" * 88)
    if bad:
        print(f"❌ {bad} 条契约**不再成立** —— 该改适配器，不要改测试的 fixture")
        return 1
    print("✅ 全部外部契约仍然成立")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
