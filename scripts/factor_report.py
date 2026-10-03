"""因子对账报告：决定「暂存表能不能切生产表」的唯一依据。

## 为什么要有这个工具

R40 修完 off-by-one 之后，重算与生产的因子在除权日步长上已对齐到 ±0.3%，
但仍有两类问题必须**逐票**看清楚才能决策：

1. **生产因子会向下跳**（992 只票，实测向上 4162 次 / 向下 2852 次）——
   纯后复权因子必须单调不降。这是生产侧的硬伤，与重算准不准无关。
2. **58% 的票因子是占位 1.0**（3028/5222 从未计算过）—— 这些票重算是
   唯一有值的来源，但也要确认重算给出的确实不是 1.0。

所以报告按**票**给结论，而不是给一个总数。

## 每只票的四个判定

==========  =========================================================
``placeholder``  生产侧因子全 1.0 ⇒ 从没算过，重算是唯一有值的来源
``nonmono``      生产侧因子向下跳 ⇒ 生产这一列不是后复权因子
``step_mismatch``除权日台阶与生产差 > 2% ⇒ 两边对不齐，别急着信任何一边
``ok``           台阶对得上、单调 ⇒ 可切
==========  =========================================================

## 判据为什么是「除权日台阶」而不是「因子值」

因子**值**受重标定锚点影响（两条序列可以差一个常数倍而形状完全正确），
直接比值会得到大量假差异。除权日的**跳变倍数**与锚点无关 ——
这正是 R39 台阶对账能成立、R40 却仍漏掉 off-by-one 的原因：
比倍数看不出日期归属，所以 R40 才又换了「除权日连续性」这条独立判据。
本报告两者都用。

## 用法

    python scripts/factor_report.py                     # 全部
    python scripts/factor_report.py --limit 30          # 只看偏差最大的
    python scripts/factor_report.py --codes 600519,000002
    python scripts/factor_report.py --json out.json     # 存机读结果

退出码恒为 0 —— 这是**报告**不是**门禁**；切换与否由人决定。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cpt.adapters._dbconfig import connection_kwargs
from cpt.adapters.eastmoney_actions import (
    EastmoneyActionClient,
    EastmoneyActionUnavailable,
)

#: 台阶对不齐的阈值。R40 实测修复后正常在 ±0.3% 内，所以 2% 足够宽松。
STEP_MISMATCH_TOL = 0.02


@dataclass
class CodeReport:
    code: str
    bars_prod: int = 0
    bars_new: int = 0
    prod_down_jumps: int = 0
    new_down_jumps: int = 0
    ex_dates: int = 0
    step_matched: int = 0
    max_step_dev: float = 0.0
    prod_placeholder: bool = False
    #: 重算侧因子**恒定**（= 窗口内无任何已实施公司行动）
    new_constant: bool = False
    mean_rel_dev: float = 0.0
    flags: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        # ⚠️ 顺序有讲究：「恒定」与「台阶对不齐」**不冲突**。窗口内无公司
        # 行动的票，台阶数就是 0（无从对齐），但那**恰恰是对的结果** ——
        # R40 实测 000016/000002/000826/000615 窗口内确实一次分红都没有
        # （末次分别在 2022-06 / 2023-08 / 2019-07 / 2018-06），因子恒定
        # 才对；而生产侧同期在 152~400 之间乱跳，那边才是错的。
        # 所以「恒定」优先于「台阶对不齐」判。
        if self.new_constant:
            if self.prod_down_jumps:
                return "无公司行动/因子恒定（生产在乱跳）"
            return "无公司行动/因子恒定"
        if "step_mismatch" in self.flags:
            return "台阶对不齐"
        if "nonmono" in self.flags:
            return "生产非单调（重算可用）"
        if "placeholder" in self.flags:
            return "生产占位（重算可用）"
        return "可切"


def _series(cur: Any, table: str, code: str) -> list[tuple[str, float]]:
    cur.execute(
        f"SELECT trade_date, hfq_factor FROM {table} WHERE code=%s ORDER BY trade_date",
        (code,),
    )
    return [(str(d), float(f)) for d, f in cur.fetchall() if f]


def _down_jumps(rows: Sequence[tuple[str, float]], tol: float = 0.999) -> int:
    return sum(1 for i in range(1, len(rows)) if rows[i][1] < rows[i - 1][1] * tol)


def analyse(cur: Any, code: str, ex_dates: Sequence[str]) -> CodeReport | None:
    prod = _series(cur, "asel.ref_adjust_factor", code)
    new = _series(cur, "asel.ref_adjust_factor_v2", code)
    if not prod or not new:
        return None

    rep = CodeReport(code=code, bars_prod=len(prod), bars_new=len(new))
    rep.prod_down_jumps = _down_jumps(prod)
    rep.new_down_jumps = _down_jumps(new)
    rep.prod_placeholder = len({v for _, v in prod}) == 1
    rep.new_constant = len({v for _, v in new}) == 1

    pm = dict(prod)
    devs = [abs(pm[d] / v - 1.0) for d, v in new if d in pm and v]
    rep.mean_rel_dev = sum(devs) / len(devs) if devs else 0.0

    ex_dates = sorted(ex_dates)

    ordered = [d for d, _ in prod]
    idx = {d: i for i, d in enumerate(ordered)}
    for d in ex_dates:
        i = idx.get(d)
        if i is None or i == 0 or prod[i - 1][1] == 0:
            continue
        pj = prod[i][1] / prod[i - 1][1]
        nm = dict(new).get(d)
        pm_prev = new[i - 1][1] if i - 1 < len(new) else None
        if nm is None or not pm_prev:
            continue
        nj = nm / pm_prev
        rep.ex_dates += 1
        dev = abs(nj / pj - 1.0)
        rep.max_step_dev = max(rep.max_step_dev, dev)
        if dev <= STEP_MISMATCH_TOL:
            rep.step_matched += 1

    if rep.prod_placeholder:
        rep.flags.append("placeholder")
    if rep.prod_down_jumps:
        rep.flags.append("nonmono")
    if rep.ex_dates and rep.step_matched < rep.ex_dates:
        rep.flags.append("step_mismatch")
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="因子对账报告（暂存表 vs 生产表）")
    ap.add_argument("--codes", default="", help="只查这些代码（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="只报偏差最大的 N 只（0=全部）")
    ap.add_argument("--json", default="", help="把结果写成 JSON")
    args = ap.parse_args(argv)

    import psycopg

    conn = psycopg.connect(**connection_kwargs())
    cur = conn.cursor()
    client = EastmoneyActionClient()

    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        cur.execute("SELECT DISTINCT code FROM asel.ref_adjust_factor_v2 ORDER BY code")
        codes = [r[0] for r in cur.fetchall()]

    print(f"逐票对账 {len(codes)} 只（暂存表 asel.ref_adjust_factor_v2）\n", flush=True)
    # 公司行动**先批量取**再逐票分析：每只票一次 HTTP 在 2000+ 只上要跑很久，
    # 而这批数据在一次运行里是**不变**的，没必要反复取。
    #
    # ⚠️ R44：瞬时失败**必须重试**，且必须 `flush`。
    #
    # 1) 原来 `except` 直接把 ex_by_code[code] 置空，而那只票的
    #    ``ex_dates`` 为空 ⇒ analyse() 里 ``rep.ex_dates = 0`` ⇒ 该票既不进
    #    「台阶对不齐」也不参与匹配率统计 —— **一次网络抖动就让这只票从对账里
    #    悄悄消失**，而报告照常打印，数字看起来完全正常。
    #    实测 2026-10-03：000698 一次 read timeout 就被这样吞掉。
    # 2) ``print`` 不带 flush，重定向到文件时是**块缓冲**：5000+ 只要跑一小时，
    #    日志里却一行都看不到，无法判断是在跑还是卡死（本轮为此白等三轮）。
    ex_by_code: dict[str, list[str]] = {}
    transient = 0
    for i, code in enumerate(codes, 1):
        for attempt in range(3):
            try:
                ex_by_code[code] = sorted({a.ex_date for a in client.fetch_actions(code)})
                break
            except EastmoneyActionUnavailable as exc:
                # 网络类：瞬时的，有界重试有意义（与 factor_recompute 同口径）
                if attempt == 2:
                    transient += 1
                    print(
                        f"    [warn] {code} 公司行动取数重试 3 次仍失败（已从对账中剔除）: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    ex_by_code[code] = []
                time.sleep(1.5 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001 — 单只取不到不中断整批
                print(f"    [warn] {code} 公司行动取数失败: {exc}", file=sys.stderr, flush=True)
                ex_by_code[code] = []
                break
        if i % 100 == 0:
            print(f"  ... 公司行动 {i}/{len(codes)}", flush=True)
    if transient:
        print(
            f"\n  ⚠️ {transient} 只因网络问题被剔出对账 —— 它们**没有**计入任何结论，"
            f"重跑本报告可补上。",
            flush=True,
        )
    print(flush=True)
    reports: list[CodeReport] = []
    for i, code in enumerate(codes, 1):
        rep = analyse(cur, code, ex_by_code.get(code, ()))
        if rep is None:
            continue
        reports.append(rep)
        if i % 50 == 0:
            print(f"  ... {i}/{len(codes)}", flush=True)

    if args.limit:
        reports = sorted(reports, key=lambda r: -r.mean_rel_dev)[: args.limit]

    by_verdict: dict[str, int] = {}
    for r in reports:
        by_verdict[r.verdict] = by_verdict.get(r.verdict, 0) + 1

    print()
    print("=" * 96)
    header = (
        f"{'代码':<9} {'行数':<6} {'除权日':<7} {'台阶对':<7} "
        f"{'最大台阶差':<11} {'平均因子差':<11} {'生产向下':<9} {'结论'}"
    )
    print(header)
    print("-" * 96)
    for r in reports:
        print(
            f"{r.code:<9} {r.bars_prod:<6} {r.ex_dates:<7} "
            f"{f'{r.step_matched}/{r.ex_dates}':<7} {r.max_step_dev:>9.2%}  "
            f"{r.mean_rel_dev:>9.2%}  {r.prod_down_jumps:<9} {r.verdict}"
        )

    print()
    print("=" * 96)
    print(f"共 {len(reports)} 只。结论分布：")
    for k, v in sorted(by_verdict.items(), key=lambda x: -x[1]):
        print(f"  {k:<24} {v} 只")
    tot_ex = sum(r.ex_dates for r in reports)
    tot_ok = sum(r.step_matched for r in reports)
    if tot_ex:
        print(f"  除权日台阶总体匹配率：{tot_ok}/{tot_ex} = {tot_ok / tot_ex:.1%}")

    if args.json:
        Path(args.json).write_text(
            json.dumps([asdict(r) for r in reports], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
