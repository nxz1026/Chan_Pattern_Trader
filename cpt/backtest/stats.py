"""回测统计：一组「事件样本」的收益，与一组「基线样本」的差值。

## 为什么基线不能用「全部 K 线」

R70 报过「structure_ready 20 日 +0.481%」，看上去是全体基线
（+0.320%）的 1.5 倍。但那是拿 **4 千个事件** 与 **130 万根 K 线** 比 ——
样本单元不同，结论作废。

本模块强制 **两组样本单位一致**：要么都是事件，要么都是时间点。
:func:`compare` 只做「同一批票、同一段时间、同一批样本位置」下的配对比较。

## 配对 vs 非配对

- :func:`summarise`：描述一组样本。
- :func:`paired`：A 与 B **在同一天**的收益差。这是唯一能回答
  「等 A 确认 vs 提前在 B 买入，差多少」的口径。
- :func:`excess_over_market`：减掉同日全市场等权收益，回答「是不是只是大盘涨」。

R70 的教训：``paired`` 的 n 会远小于两组各自的 n。**n 必须报出来**，
不然「差 +3.4%，t=7.6」会让人以为样本很多，实际配对只有 0 个（R70 实测过
A 与 B 同一天只 1 个的情况）。
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "Sample",
    "Summary",
    "compare",
    "excess_over_market",
    "paired",
    "summarise",
]

#: t 统计量低于此值视为「无法与 0 区分」。**不是**显著性判据，只是提醒。
_MIN_ABS_T: Final[float] = 2.0


@dataclass(frozen=True)
class Sample:
    """一个事件样本：某只票、某个日期、某个窗口的收益。

    ``ret`` 是**小数**（0.0123 = +1.23%）。``date_ms`` 用来做配对与去市场均值。
    """

    code: str
    date_ms: int
    ret: float


@dataclass(frozen=True)
class Summary:
    """一组样本的描述统计。``n == 0`` 时其余字段全是 ``None``。"""

    n: int
    mean: float | None = None
    median: float | None = None
    win_rate: float | None = None
    stdev: float | None = None
    t_stat: float | None = None
    #: t 的绝对值 < 2 时为 ``True``，表示「和 0 分不开」。
    indistinguishable_from_zero: bool = False

    def as_dict(self) -> dict[str, float | int | None | bool]:
        return {
            "n": self.n,
            "mean": self.mean,
            "median": self.median,
            "win_rate": self.win_rate,
            "stdev": self.stdev,
            "t_stat": self.t_stat,
            "indistinguishable_from_zero": self.indistinguishable_from_zero,
        }


def summarise(samples: Sequence[Sample]) -> Summary:
    """描述一组样本。

    ``win_rate`` 是 ``ret > 0`` 的比例 —— **不含等号**。R70 报过
    「胜率 46%」而均值 +0.11%，两者同时成立正是因为中位数远低于均值。
    """
    if not samples:
        return Summary(n=0)
    rets = [s.ret for s in samples]
    n = len(rets)
    mean = statistics.fmean(rets)
    median = statistics.median(rets)
    win = sum(1 for r in rets if r > 0) / n
    if n < 2:
        return Summary(n=n, mean=mean, median=median, win_rate=win)
    sd = statistics.stdev(rets)
    if sd == 0:
        # 方差为零 = 每个样本一模一样。这是**确定性**，不是「无信息」。
        # 第一版把 ``t is None`` 当成「与 0 分不开」，于是「50 个样本全是
        # +5%」被判成噪声 —— 逻辑反了。sd==0 且 mean!=0 时 t 无穷大。
        t = math.inf if mean != 0 else None
        return Summary(
            n=n,
            mean=mean,
            median=median,
            win_rate=win,
            stdev=sd,
            t_stat=t,
            indistinguishable_from_zero=(mean == 0),
        )
    t = mean / (sd / math.sqrt(n))
    return Summary(
        n=n,
        mean=mean,
        median=median,
        win_rate=win,
        stdev=sd,
        t_stat=t,
        indistinguishable_from_zero=abs(t) < _MIN_ABS_T,
    )


def paired(a: Sequence[Sample], b: Sequence[Sample]) -> Summary:
    """同日配对：``a`` 的收益减 ``b`` 的收益。

    只保留 **``(code, date_ms)`` 两边都有** 的样本。n 会很小于两组各自的 n，
    这是正常的 —— 报 n 就是为了让这个变小可见。

    R70 实测过：等确认（B）与提前买（A）在同一天几乎不会同时成立，
    于是「配对差值 +3.4%」其实是极少数样本算出来的。
    """
    key_b = {(s.code, s.date_ms): s.ret for s in b}
    diffs: list[Sample] = []
    for s in a:
        other = key_b.get((s.code, s.date_ms))
        if other is None:
            continue
        diffs.append(Sample(code=s.code, date_ms=s.date_ms, ret=s.ret - other))
    return summarise(diffs)


def excess_over_market(
    samples: Sequence[Sample],
    market: dict[int, float],
) -> Summary:
    """减掉同日全市场等权收益。

    ``market`` 是 ``{date_ms: 当日全市场平均收益}``。样本日期不在 ``market``
    里就**丢掉那个样本**（不补 0 —— 补 0 等于假设那天大盘没动）。

    这是 R70 最关键的一步：那个 +13% 的「超额」在扣掉同日后仍然 +12.6%，
    才排除了「只是那一波大盘涨」的解释。
    """
    adjusted: list[Sample] = []
    for s in samples:
        m = market.get(s.date_ms)
        if m is None:
            continue
        adjusted.append(Sample(code=s.code, date_ms=s.date_ms, ret=s.ret - m))
    return summarise(adjusted)


@dataclass(frozen=True)
class Comparison:
    """两组样本的对照结果。"""

    a: Summary
    b: Summary
    paired: Summary
    #: 一句话结论（人话），由 :func:`compare` 按数据自动生成。
    verdict: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "a": self.a.as_dict(),
            "b": self.b.as_dict(),
            "paired": self.paired.as_dict(),
            "verdict": self.verdict,
            "notes": list(self.notes),
        }


def compare(
    a: Sequence[Sample],
    b: Sequence[Sample],
    *,
    label_a: str = "A",
    label_b: str = "B",
    horizon: str = "h20",
) -> Comparison:
    """对照两组样本，自动生成一句人话结论。

    结论的判据（写死在代码里，免得每次换一种说法）：

    1. 任一组 ``n == 0`` → 「样本不足，无法比较」
    2. 配对 ``n == 0`` → 「两组没有同一天的样本，配对比较不成立」
       （这条是 R70 差点踩进去的坑：非配对差值看起来再大也没意义）
    3. 配对均值的 t 绝对值 < 2 → 「差异与 0 分不开」
    4. 否则按方向说「A 比 B 高/低多少」
    """
    sa, sb, sp = summarise(a), summarise(b), paired(a, b)
    notes: list[str] = []

    if sa.n == 0 or sb.n == 0:
        verdict = f"样本不足（{label_a} n={sa.n}，{label_b} n={sb.n}），无法比较 {horizon} 收益。"
        return Comparison(a=sa, b=sb, paired=sp, verdict=verdict, notes=notes)

    if sp.n == 0:
        verdict = (
            f"{label_a} 与 {label_b} 没有同一天的样本（配对 n=0），"
            f"配对比较不成立 —— 各自的均值（{sa.mean:+.3%} vs {sb.mean:+.3%}）"
            "受样本选择影响，不能当差异读。"
        )
        notes.append("非配对比较在样本单元不同的时候会得出假结论（R70 的教训）。")
        return Comparison(a=sa, b=sb, paired=sp, verdict=verdict, notes=notes)

    assert sp.mean is not None
    if sp.indistinguishable_from_zero:
        verdict = (
            f"{horizon} 上 {label_a} 与 {label_b} 差 {sp.mean:+.3%}"
            f"（配对 n={sp.n}，t={_fmt_t(sp.t_stat)}），与 0 分不开。"
        )
    else:
        direction = "高" if sp.mean > 0 else "低"
        verdict = (
            f"{horizon} 上 {label_a} 比 {label_b} {direction} {abs(sp.mean):.3%}"
            f"（配对 n={sp.n}，t={_fmt_t(sp.t_stat)}）。"
        )

    if sp.n < 30:
        notes.append(f"配对样本只有 {sp.n} 个，t 值不可靠。")
    if sa.win_rate is not None and sa.win_rate < 0.5 and sa.mean is not None and sa.mean > 0:
        notes.append(
            f"{label_a} 均值 {sa.mean:+.3%} 但胜率 {sa.win_rate:.0%} <50% —— "
            "少数大涨拉高了均值，典型的一笔交易多半是亏的。"
        )
    return Comparison(a=sa, b=sb, paired=sp, verdict=verdict, notes=notes)


def _fmt_t(t: float | None) -> str:
    if t is None:
        return "n/a"
    if math.isinf(t):
        return "+inf" if t > 0 else "-inf"
    return f"{t:+.2f}"
