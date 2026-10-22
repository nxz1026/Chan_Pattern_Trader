"""价格形状扫描：把「某种 K 线形态之后 N 日的收益」做成一把可复用的尺子。

## 为什么需要这一层（R88 的教训）

R88 给产品文案换数时发现，面板上那句「20 日新低后收阳 +0.81%、胜率 48%」
**复现不出来**：同批票、同窗口、同预热约定重跑，它是 **-0.248%**，比
「随便挑一天」的基线（+0.224%）还差。原因不是数据变了，是当初的量法没有
固化成代码 —— 探针写在 ``.tmp_audit/`` 里、口径写在脑子里，改一句文案就跑一遍，
每次的「新低」定义都可能不一样（实测 ``cl[i] <= min(cl[i-20:i])`` 与
``cl[i] <= min(cl[i-19:i])`` 差 5 倍样本、0.74 个百分点）。

**这一层的存在意义**：任何「某个形状之后会涨」的说法，都必须能用一个具名的
:class:`Shape` 复跑出来；复跑不出来就不许写进文案。

## 三条硬规矩

1. **形状必须有界窗口**。每根 bar 只看它**之前** ``lookback`` 根，不看之后 ——
   否则又是 R70/R87 那种「用未来信息筛样本」。
2. **基线是同一把尺子量出来的**。:func:`baseline` 与 :func:`shape_samples`
   都从同一份面板取收益，样本单元一致（R70 的 4 千事件 vs 130 万 K 线
   那种错配不许再出现）。
3. **预热期必须显式**。不卡预热期，同一批票的 h20 是 +2.52%，卡 250 根之后
   只剩 +0.25% —— 差的 2.27 个百分点全是窗口前 250 根的反弹段。默认
   ``warmup=250``，且报告里一定打出来。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from cpt.backtest.panel import BarPanel
from cpt.backtest.stats import Sample, summarise

__all__ = [
    "DEFAULT_WARMUP",
    "SHAPES",
    "Shape",
    "baseline",
    "describe",
    "dip_below_ma",
    "drop_in_window",
    "low_after_n_day_low",
    "scan",
    "shape_samples",
    "today_ms",
    "up_after_down_streak",
]

#: 预热根数。与 R70/R87 一致 —— 结构层冷启动也要这么多。
DEFAULT_WARMUP: Final[int] = 250


@dataclass(frozen=True)
class Shape:
    """一个具名的价格形状。

    Attributes:
        name: 机器名（报告与 JSON 的键）。
        title: 一句话人话标题，直接给非专业读者看。
        lookback: 判定只许看前这么多根。
        match: ``(panel, i) -> bool``，判定第 ``i`` 根当天是否构成这个形状。
        note: 量法说明（写清「新低」这类含糊词的确切定义）。
    """

    name: str
    title: str
    lookback: int
    match: Callable[[BarPanel, int], bool]
    note: str


def _closes_ok(panel: BarPanel, i: int, need: int) -> bool:
    """第 ``i`` 根之前够 ``need`` 根、且每根都已复权（未复权的价格会造假跌）。"""
    if i < need:
        return False
    if len(panel.adjusted) < i + 1:
        return False
    return all(panel.adjusted[i - need : i + 1])


def low_after_n_day_low(*, days: int, tol: float = 1.0, name: str | None = None) -> Shape:
    """收盘跌破（或贴着）前 ``days`` 根最低价，且当天收阳。

    「新低」的确切定义：``cl[i] <= min(cl[i-days:i]) * tol``。
    **注意**：``i-days`` 到 ``i-1`` 是 ``days`` 根，不含当天 —— 与 R88 的
    ``cl[i] <= min(cl[i-20:i])`` 完全一致。换成 ``[i-19:i]`` 就是另一个形状。
    """

    def _match(panel: BarPanel, i: int) -> bool:
        if not _closes_ok(panel, i, days):
            return False
        window = panel.close[i - days : i]
        if not window:
            return False
        if panel.open[i] <= 0 or panel.close[i] <= 0:
            return False
        if panel.close[i] <= panel.open[i]:
            return False
        return panel.close[i] <= min(window) * tol

    # 名字必须唯一 —— 两个形状撞名会让 JSON 键互相覆盖（实跑时 ``tol=1.0``
    # 与 ``tol=1.02`` 都叫 ``low_20_up``，结果只剩一个）。
    label = name or (f"low_{days}_up" if tol <= 1.0 else f"near_low_{days}_up")
    return Shape(
        name=label,
        title=(
            f"{days} 日新低后收阳"
            if tol <= 1.0
            else f"{days} 日新低（往上 {(tol - 1) * 100:.0f}% 以内）后收阳"
        ),
        lookback=days,
        match=_match,
        note=f"cl[i] <= min(cl[i-{days}:i]) * {tol}，且 cl[i] > op[i]",
    )


def up_after_down_streak(*, days: int, pct: float) -> Shape:
    """连续 ``days`` 根阴线（或累计跌超 ``pct``）之后的第一个收阳日。

    「跌超 pct」的确切定义：``cl[i-1] / cl[i-days] - 1 <= -pct``，
    窗口是 ``i-days`` 到 ``i-1``，同样**不含当天**。
    """

    def _match(panel: BarPanel, i: int) -> bool:
        if not _closes_ok(panel, i, days):
            return False
        start = panel.close[i - days]
        if start <= 0 or panel.close[i] <= 0 or panel.open[i] <= 0:
            return False
        if panel.close[i] <= panel.open[i]:
            return False
        return panel.close[i - 1] / start - 1.0 <= -pct

    return Shape(
        name=f"drop_{int(pct * 100)}pct_up",
        title=f"前 {days} 日跌超 {int(pct * 100)}% 后收阳",
        lookback=days,
        match=_match,
        note=f"cl[i-1] / cl[i-{days}] - 1 <= -{pct}，且 cl[i] > op[i]",
    )


def drop_in_window(*, days: int, pct: float) -> Shape:
    """收盘价跌到**前 ``days`` 日最低收盘的 ``1-pct`` 倍以下**，**不要求当天收阳**。

    与 R88 探针 ``.tmp_audit/shape_probe.py`` 第 33 行的
    ``if cl[i] <= min(w) * 0.96`` 逐字对齐 —— 那一行**在 ``if up:`` 块外面**，
    所以不看当天阴阳。形状名里的 ``anyclose`` 说的就是这个。

    **三个近义定义不许混用**（都在 5,131 只票上量过，h20）：

    =======================================  =========  =========  ======
    定义                                       n         均值       胜率
    =======================================  =========  =========  ======
    ``cl[i] <= min(cl[i-20:i]) * 0.96``          20,189  +4.05%     58.4%
    ``cl[i-1]/cl[i-20] - 1 <= -0.04``           476,252  +1.07%     49.4%
    同上且``cl[i] > op[i]``（收阳）               228,660  +1.04%     49.5%
    =======================================  =========  =========  ======

    第一行和第二行只差一个「比较基准」，样本差 23 倍、收益差 3 个百分点。
    文案里那句「跌超 4% 之后 +4.05%、胜率 58.4%」量的是**第一行**。

    这是 R88 探针 ``n4pct_anyclose`` 的正式定义 —— 文案里那句
    「跌超 4% 之后 +4.05%、胜率 58.4%」量的就是它。

    **必须与 :func:`up_after_down_streak` 分开命名**：两者只差一个
    「当天收阳」的附加条件，实测样本量差 11 倍（20,189 vs 231,437）、
    收益差 3.2 个百分点。同一个名字套两个定义，就是 R88 那句文案复现不出来的
    直接原因。
    """

    def _match(panel: BarPanel, i: int) -> bool:
        if not _closes_ok(panel, i, days):
            return False
        window = panel.close[i - days : i]
        if not window or panel.close[i] <= 0:
            return False
        return panel.close[i] <= min(window) * (1.0 - pct)

    return Shape(
        name=f"drop_{int(pct * 100)}pct",
        title=f"前 {days} 日跌超 {int(pct * 100)}%（不看当天阴阳）",
        lookback=days,
        match=_match,
        note=f"cl[i] <= min(cl[i-{days}:i]) * {1.0 - pct:.2f}",
    )


def dip_below_ma(*, span: int) -> Shape:
    """收盘价跌破 ``span`` 日均线，且当天收阳。"""
    if span < 1:
        raise ValueError("span 必须 >= 1")

    def _match(panel: BarPanel, i: int) -> bool:
        if not _closes_ok(panel, i, span - 1):
            return False
        window = panel.close[i - span + 1 : i + 1]
        if len(window) != span:
            return False
        if panel.close[i] <= panel.open[i]:
            return False
        return panel.close[i] < sum(window) / span

    return Shape(
        name=f"below_ma{span}_up",
        title=f"跌破 {span} 日均线后收阳",
        lookback=span,
        match=_match,
        note=f"cl[i] < mean(cl[i-{span - 1}:i+1])，且 cl[i] > op[i]",
    )


#: 入仓的形状。**改这里就等于改产品文案里的数** —— 新增形状必须连测试一起加。
#:
#: R88 实测（5,080 只 / 1,308,385 样本 / 预热 250 / h20 / 未复权口径下的原始价
#: 与复权口径同向）：
#:
#: - 文案里那句「跌超 4% 之后 +4.05%、胜率 58.4%」对应的是 **``drop_4pct``**
#:   （前 20 日跌超 4%，**不看当天阴阳**），全市场复跑 +4.05% / 58.4% 完全吻合。
#:   加一个「且收阳」会把它变成 ``drop_4pct_up``：样本从 20,189 涨到 231,437，
#:   收益跌到 +0.83% / 49.0%。**这两个形状不许混用同一句话。**
SHAPES: Final[tuple[Shape, ...]] = (
    low_after_n_day_low(days=20),
    low_after_n_day_low(days=20, tol=1.02, name="near_low_20_up"),
    drop_in_window(days=20, pct=0.04),
    up_after_down_streak(days=20, pct=0.04),
    dip_below_ma(span=20),
)


def shape_samples(
    panels: Mapping[str, BarPanel],
    shape: Shape,
    *,
    horizon: int,
    warmup: int = DEFAULT_WARMUP,
) -> list[Sample]:
    """扫出所有构成 ``shape`` 的位置，取它们的 ``h`` 日收益。

    ``i`` 从 ``warmup`` 开始 —— 预热期内的位置**不算**（R88 已知不加预热
    会让同一批票的 h20 从 +0.25% 变成 +2.52%）。
    """
    out: list[Sample] = []
    for code, panel in panels.items():
        n = len(panel)
        for i in range(warmup, n):
            ret = panel.return_at(horizon, i)
            if ret is None:
                continue
            if not shape.match(panel, i):
                continue
            out.append(Sample(code=code, date_ms=panel.dates_ms[i], ret=ret))
    return out


def baseline(
    panels: Mapping[str, BarPanel],
    *,
    horizon: int,
    warmup: int = DEFAULT_WARMUP,
) -> list[Sample]:
    """**同一把尺子**量出来的基线：每根（可成交且窗口完整的）bar 都算一个样本。

    与 :func:`shape_samples` 用同一个 ``warmup``、同一个 ``horizon``、
    同一批面板 —— 所以两者的 n 可以直接比（这是 R70 教训的直接产物）。
    """
    out: list[Sample] = []
    for code, panel in panels.items():
        n = len(panel)
        for i in range(warmup, n):
            ret = panel.return_at(horizon, i)
            if ret is None:
                continue
            out.append(Sample(code=code, date_ms=panel.dates_ms[i], ret=ret))
    return out


def scan(
    panels: Mapping[str, BarPanel],
    *,
    shapes: Sequence[Shape] = SHAPES,
    horizons: Sequence[int] = (1, 5, 10, 20),
    warmup: int = DEFAULT_WARMUP,
) -> list[dict[str, object]]:
    """把每个形状在多个窗口下跑一遍，输出可直接写 JSON 的行。

    每行一个「形状 × 窗口」，字段：
    ``shape / title / note / horizon / warmup / n / mean / median / win_rate /
    base_n / base_mean / base_win_rate``。
    """
    rows: list[dict[str, object]] = []
    for h in horizons:
        base = summarise(baseline(panels, horizon=h, warmup=warmup))
        for shape in shapes:
            got = summarise(shape_samples(panels, shape, horizon=h, warmup=warmup))
            rows.append(
                {
                    "shape": shape.name,
                    "title": shape.title,
                    "note": shape.note,
                    "horizon": h,
                    "warmup": warmup,
                    "n": got.n,
                    "mean": got.mean,
                    "median": got.median,
                    "win_rate": got.win_rate,
                    "base_n": base.n,
                    "base_mean": base.mean,
                    "base_win_rate": base.win_rate,
                }
            )
    return rows


def describe(row: Mapping[str, object]) -> str:
    """``row`` 的一行人话。给 CLI 用，也给文档抄数字用。"""
    mean = row.get("mean")
    win = row.get("win_rate")
    base_mean = row.get("base_mean")
    if not isinstance(mean, (int, float)) or not isinstance(win, (int, float)):
        return f"{row.get('title')} · h{row.get('horizon')} · 样本不足"
    delta = ""
    if isinstance(base_mean, (int, float)):
        delta = f"（基线 {base_mean * 100:+.2f}%，差 {(mean - base_mean) * 100:+.2f}pp）"
    return (
        f"{row.get('title')} · h{row.get('horizon')} · "
        f"n={row.get('n')} 均值 {mean * 100:+.2f}% 胜率 {win * 100:.1f}%{delta}"
    )


def today_ms(date: dt.date) -> int:
    """把日期换成面板用的 Unix 毫秒（UTC 零点）。"""
    return int(dt.datetime(date.year, date.month, date.day, tzinfo=dt.UTC).timestamp() * 1000)
