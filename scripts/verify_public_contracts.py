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
    """交叉校验（附加）：腾讯 hfq 价与**本地因子表**锚定值是否自洽。

    两条独立数据路径（腾讯 hfq  vs  东财重算的因子表）算出的**当日后复权价**
    应该一致。不一致 ⇒ 至少有一边的复权口径漂了。
    """
    bars = TencentKlineClient().fetch_daily_bars("600519", limit=3)
    tencent_hfq_close = bars[-1].close
    from cpt.adapters.a_share_local import AShareLocalClient

    conn = AShareLocalClient()._get_conn()  # noqa: SLF001
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""SELECT hfq_factor FROM asel.ref_adjust_factor
                   WHERE code='600519' ORDER BY trade_date DESC LIMIT 1""")
    row = cur.fetchone()
    cur.execute("""SELECT close FROM public.daily_bar
                   WHERE code='600519' ORDER BY date DESC LIMIT 1""")
    raw = cur.fetchone()[0]
    local_hfq_close = float(raw) * float(row[0])
    dev = abs(local_hfq_close / tencent_hfq_close - 1)
    if dev > 0.01:
        return f"⚠️ 两侧后复权价差 {dev*100:.2f}%：腾讯 {tencent_hfq_close:.2f} vs 本地 {local_hfq_close:.2f}"
    return f"✅ 两侧一致（差 {dev*100:.3f}%）：腾讯 {tencent_hfq_close:.2f} / 本地 {local_hfq_close:.2f}"


CHECKS = (
    ("契约1 fqkline 字段顺序 = [日期,开,收,高,低,量]", c1_field_order),
    ("契约2 复权键名 hfqday/qfqday/day", c2_adjust_keys),
    ("契约3 逐标的「无 hfq」抛专用异常", c3_per_stock_unsupported),
    ("契约4 新浪快照带 Referer", c4_sina_quote),
    ("交叉 腾讯 hfq vs 本地因子表", c5_hfq_matches_local_factor),
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="只打印结论")
    args = ap.parse_args(argv)

    for name, fn in CHECKS:
        check(name, fn)

    print("=" * 88)
    bad = 0
    for name, ok, detail in results:
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
