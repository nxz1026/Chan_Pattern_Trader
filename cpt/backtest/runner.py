"""回测执行器：把「信号跃迁」变成「可比较的收益样本」并给出结论。

## 这个模块只做三件事

1. **配对**：同一个结构，从 ``structure_ready`` 那天买到 ``confirmed`` 那天买，
   两者的前视收益各是多少 —— 只算 ``(结构, h)`` 两边都能成交的样本。
2. **集合统计**：每个状态单独当买点时的收益（不与别的状态配对）。
3. **给结论**：直接调用 :func:`cpt.backtest.stats.compare`，把「是不是噪声」
   判在代码里，不留给调用方手写。

## 三条纪律（都是 R70 用真金白银换的）

**纪律一：配对只认同一结构**
R70 第一版把「所有 structure_ready 的票」和「所有 confirmed 的票」各自平均，
算出 +4.15% vs +0.70%。那不是差异，是两个不同样本集合的均值差 ——
后来的 +13.37% 假结论就是这么来的。

**纪律二：配对的两端必须都能成交**
``structure_ready`` 那天涨停买不进，这一对就不能用。**不是**「A 端剔掉、
B 端留着」—— 那样两只脚站的样本不是一个世界。

**纪律三：不许用未来信息筛选样本**
R70 的 +13% 全部来自「只保留最终会 confirmed 的结构」—— 要 confirmed 就必须
在之后出现反向笔，上涨段里的反向笔就是短回调，于是这些结构的就绪日事后看
全落在上涨途中。本模块的 ``ab_pairs`` **只做配对**，要算「全体 structure_ready
当买点」必须用 ``status_samples``（不依赖未来）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from cpt.backtest.panel import HORIZONS, BarPanel
from cpt.backtest.signals import SignalTrace
from cpt.backtest.stats import Sample, Summary, compare, summarise

__all__ = [
    "PairSet",
    "StatusSamples",
    "ab_pairs",
    "collect_status_samples",
    "run_backtest",
    "status_samples",
]

#: 两个可比状态。A = 提前（结构就绪），B = 等确认。
A_STATUS: Final[str] = "structure_ready"
B_STATUS: Final[str] = "confirmed"


@dataclass(frozen=True)
class PairSet:
    """一个 horizon 上的配对样本。

    ``a`` 与 ``b`` 是**同下标对齐**的：``a[i]`` 与 ``b[i]`` 来自同一结构的
    同一次跃迁。
    """

    horizon: int
    a: tuple[float, ...]
    b: tuple[float, ...]
    #: 因为 A 端买不进（涨停）被剔掉的对数。
    dropped_not_tradable: int
    #: 因为窗口不足（末尾 h 根）被剔掉的对数。
    dropped_no_window: int
    #: ``confirmed`` 之前没配到 ``structure_ready`` 的次数（历史行不全）。
    #: 这类行在 R71 之前成批存在，沉默丢掉会让配对样本量看起来比实际大。
    unpaired: int = 0

    def __len__(self) -> int:
        return len(self.a)

    def samples_a(self, code_of: str = "") -> list[Sample]:
        return [Sample(code=code_of, date_ms=i, ret=v) for i, v in enumerate(self.a)]


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
    """一次完整回测的产物。``pairs`` 与 ``status`` 是两个**不同的样本单元**。"""

    codes: int
    pairs: dict[int, PairSet] = field(default_factory=dict)
    status: dict[str, StatusSamples] = field(default_factory=dict)
    #: 每只票算不出结构的 bar 数合计。
    skipped_bars: int = 0

    def comparison(self, h: int) -> Any:
        ps = self.pairs.get(h)
        if ps is None or not ps.a:
            return compare([], [], label_a=A_STATUS, label_b=B_STATUS, horizon=f"h{h}")
        return compare(
            [Sample(code="pair", date_ms=i, ret=v) for i, v in enumerate(ps.a)],
            [Sample(code="pair", date_ms=i, ret=v) for i, v in enumerate(ps.b)],
            label_a=A_STATUS,
            label_b=B_STATUS,
            horizon=f"h{h}",
        )


def ab_pairs(
    trace: SignalTrace,
    panel: BarPanel,
    *,
    horizons: Sequence[int] = HORIZONS,
) -> dict[int, PairSet]:
    """把一只票的跃迁序列配成 A/B 对。

    同一 ``structure_id`` 上，**第一次** ``structure_ready`` 与**其后第一次**
    ``confirmed`` 配成一对。多个结构并存时各配各的（用 dict 分开，不交叉）。
    """
    ready_at: dict[str, int] = {}
    a_by_h: dict[int, list[float]] = {h: [] for h in horizons}
    b_by_h: dict[int, list[float]] = {h: [] for h in horizons}
    drop_nt = {h: 0 for h in horizons}
    drop_win = {h: 0 for h in horizons}
    unpaired = {h: 0 for h in horizons}

    for sig in trace.signals:
        sid = sig.structure_id
        if sig.status == A_STATUS:
            ready_at.setdefault(sid, sig.index)
            continue
        if sig.status != B_STATUS:
            continue
        a_idx = ready_at.pop(sid, None)
        if a_idx is None:
            # 没经过 ready 就直接 confirmed（比如历史行不全）—— 配不上，但要计数。
            for h in horizons:
                unpaired[h] += 1
            continue
        for h in horizons:
            # 纪律二：两端都要能成交。任何一端涨停就整对丢掉。
            if not _tradable(panel, a_idx) or not _tradable(panel, sig.index):
                drop_nt[h] += 1
                continue
            a_ret = panel.return_at(h, a_idx)
            b_ret = panel.return_at(h, sig.index)
            if a_ret is None or b_ret is None:
                drop_win[h] += 1
                continue
            a_by_h[h].append(a_ret)
            b_by_h[h].append(b_ret)

    return {
        h: PairSet(
            horizon=h,
            a=tuple(a_by_h[h]),
            b=tuple(b_by_h[h]),
            dropped_not_tradable=drop_nt[h],
            dropped_no_window=drop_win[h],
            unpaired=unpaired[h],
        )
        for h in horizons
    }


def collect_status_samples(
    trace: SignalTrace,
    panel: BarPanel,
    *,
    statuses: Iterable[str],
    horizons: Sequence[int] = HORIZONS,
) -> dict[str, StatusSamples]:
    """每个状态单独当买点时的收益。

    与 :func:`ab_pairs` 的区别：**不依赖任何未来信息**。这里每个
    ``structure_ready`` 事件都收，不管它后来有没有变 confirmed。
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
    merged_pairs: dict[int, list[tuple[float, float]]] = {h: [] for h in horizons}
    drop_nt = {h: 0 for h in horizons}
    drop_win = {h: 0 for h in horizons}
    unpaired = {h: 0 for h in horizons}
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
        pairs = ab_pairs(trace, panel, horizons=horizons)
        for h in horizons:
            merged_pairs[h].extend(zip(pairs[h].a, pairs[h].b, strict=True))
            drop_nt[h] += pairs[h].dropped_not_tradable
            drop_win[h] += pairs[h].dropped_no_window
            unpaired[h] += pairs[h].unpaired
        per_status = collect_status_samples(trace, panel, statuses=statuses, horizons=horizons)
        for s in statuses:
            got = per_status[s]
            for h in horizons:
                merged_status[s][h].extend(got.by_horizon.get(h, ()))
            status_nt[s] += got.not_tradable
            status_win[s] += got.no_window

    for h in horizons:
        pairs_list = merged_pairs[h]
        result.pairs[h] = PairSet(
            horizon=h,
            a=tuple(a for a, _ in pairs_list),
            b=tuple(b for _, b in pairs_list),
            dropped_not_tradable=drop_nt[h],
            dropped_no_window=drop_win[h],
            unpaired=unpaired[h],
        )
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
