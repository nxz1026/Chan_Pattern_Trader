"""回测执行器：把「信号跃迁」变成「可比较的收益样本」并给出结论。

## 这个模块只做两件事

1. **集合统计**：每个状态单独当买点时的收益（不与别的状态配对）。
2. **给结论**：直接调用 :func:`cpt.backtest.stats.compare`，把「是不是噪声」
   判在代码里，不留给调用方手写。

## R88：配对口径已删除（不是补，是废）

R70/R87 两轮都试过「同一结构，从 ``structure_ready`` 那天买（A）vs 从
``confirmed`` 那天买（B）」。两轮都得到过一个看起来很强的差（R70 +13.37%，
R87 h20 +2.524% / t=+5.69），两轮查下来都是同一件事：

**样本被未来信息筛过。** 要让一个结构走到 ``confirmed``，它后面必须出现反向笔；
在下跌段里能等来反向笔的，本身就已经是反转那一批。所以「能配对上的结构」
不是随机样本，它的就绪日事后看全落在上涨途中 —— R87 复核里 A 组 h20
中位数 **−3.41%**、均值却 +0.88%，`pre20`（就绪前 20 日涨幅）均值 +4.71%，
按 `pre20` 分桶后收益单调递增。这是动量，不是结构信号。

想修「不依赖未来」必须换样本来源：等 5 天后回头看那一刻，再决定能不能算
（参考日 = 信号日 + 5，只要求参考日之后有 confirmed）。那是**另一个研究**，
不是把 ``ab_pairs`` 的过滤条件松一松。在那之前，配对数字一律不许进文案。

⇒ 只保留不依赖未来的 :func:`collect_status_samples`。
   配对想复活，见 ``docs/rebuild-plan-review.md`` §8.17 / §8.20。

## 一条纪律（R70 用真金白银换的）

**判别量必须两端都能成交**：``structure_ready`` 那天涨停买不进，这一天就不能进
样本。**不是**「涨停的那只票剔掉、其余留着」—— 那样样本不再是无条件抽样的。
本模块统一用 ``BarPanel.is_tradable``，标签来自 ``public.derived_bar``，
**不自己算 ±10%**（ST/科创/创业/北交规则不同，见 ``cpt/domain/a_share_rules.py``）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from cpt.backtest.panel import HORIZONS, BarPanel
from cpt.backtest.signals import SignalTrace
from cpt.backtest.stats import Sample, Summary, summarise

__all__ = [
    "A_STATUS",
    "B_STATUS",
    "StatusSamples",
    "collect_status_samples",
    "run_backtest",
    "status_samples",
]

#: 「位置已到」—— 结构成立但还没等到反向笔。
A_STATUS: Final[str] = "structure_ready"
#: 「反抽确认」—— 反向笔已经出现。**不是买点**（R70/R87 都测过没有增益）。
B_STATUS: Final[str] = "confirmed"


@dataclass(frozen=True)
class StatusSamples:
    """某个状态当买点时的收益集合（不与别的状态配对）。"""

    status: str
    by_horizon: dict[int, tuple[Sample, ...]]
    #: 该状态出现但**不能成交**（涨停）的次数。
    not_tradable: int
    #: 该状态出现但窗口不足的次数。
    no_window: int

    def summary(self, h: int) -> Summary | None:
        """该状态在 ``h`` 上的汇总。**空样本返 ``None``**，不返一个 n=0 的假对象。"""
        samples = self.by_horizon.get(h, ())
        return summarise(list(samples)) if samples else None


@dataclass
class BacktestResult:
    """一次完整回测的产物。只有一种样本单元：状态事件。"""

    codes: int
    status: dict[str, StatusSamples] = field(default_factory=dict)
    #: 每只票算不出结构的 bar 数合计。
    skipped_bars: int = 0


def collect_status_samples(
    trace: SignalTrace,
    panel: BarPanel,
    *,
    statuses: Iterable[str],
    horizons: Sequence[int] = HORIZONS,
) -> dict[str, StatusSamples]:
    """每个状态单独当买点时的收益。

    **不依赖任何未来信息**：这里每个 ``structure_ready`` 事件都收，不管它后来
    有没有变 confirmed、有没有失效。
    """
    wanted = set(statuses)
    acc: dict[str, dict[int, list[Sample]]] = {s: {h: [] for h in horizons} for s in wanted}
    not_tradable: dict[str, int] = {s: 0 for s in wanted}
    no_window: dict[str, int] = {s: 0 for s in wanted}

    for sig in trace.signals:
        if sig.status not in wanted:
            continue
        for h in horizons:
            if not _tradable(panel, sig.index):
                not_tradable[sig.status] += 1
                continue
            ret = panel.return_at(h, sig.index)
            if ret is None:
                no_window[sig.status] += 1
                continue
            acc[sig.status][h].append(Sample(code=trace.code, date_ms=sig.date_ms, ret=ret))

    return {
        s: StatusSamples(
            status=s,
            by_horizon={h: tuple(v) for h, v in acc[s].items()},
            not_tradable=not_tradable[s],
            no_window=no_window[s],
        )
        for s in wanted
    }


def run_backtest(
    *,
    panels: dict[str, BarPanel],
    traces: dict[str, SignalTrace],
    horizons: Sequence[int] = HORIZONS,
    statuses: Sequence[str] = (A_STATUS, B_STATUS, "invalidated"),
) -> BacktestResult:
    """跑一批票。``panels`` 与 ``traces`` 的 key 必须一致（都只有能算的票）。"""
    result = BacktestResult(codes=len(traces))
    merged_status: dict[str, dict[int, list[Sample]]] = {
        s: {h: [] for h in horizons} for s in statuses
    }
    status_nt = {s: 0 for s in statuses}
    status_win = {s: 0 for s in statuses}

    for code, trace in traces.items():
        panel = panels.get(code)
        if panel is None:
            continue
        result.skipped_bars += trace.skipped
        per_status = collect_status_samples(trace, panel, statuses=statuses, horizons=horizons)
        for s in statuses:
            got = per_status[s]
            for h in horizons:
                merged_status[s][h].extend(got.by_horizon.get(h, ()))
            status_nt[s] += got.not_tradable
            status_win[s] += got.no_window

    for s in statuses:
        result.status[s] = StatusSamples(
            status=s,
            by_horizon={h: tuple(v) for h, v in merged_status[s].items()},
            not_tradable=status_nt[s],
            no_window=status_win[s],
        )
    return result


def status_samples(result: BacktestResult, status: str, h: int) -> list[Sample]:
    """便捷取样本（报告层用）。"""
    got = result.status.get(status)
    if got is None:
        return []
    return list(got.by_horizon.get(h, ()))


def _tradable(panel: BarPanel, i: int) -> bool:
    """第 ``i`` 根能不能买入。**只用面板上的标签**，不自己算阈值。"""
    if i < 0 or i >= len(panel):
        return False
    return panel.is_tradable(i)
