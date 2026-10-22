"""回测用的结构信号生成：把一只票的完整 bar 序列变成「按时间排好的信号事件」。

## 为什么这层要单独存在

R70 的探针把结构计算、信号判定、收益统计、涨跌停剔除全塞在一个 139 行的
``/tmp`` 脚本里。后果是那七轮排除性验证**没有一行进仓**：口径改没改过、
被测过没有，事后只能靠翻日志。

本模块只做一件事：**逐根往前推，记录每个时点信号状态变成了什么**。
它不碰收益、不碰数据库、不碰打印 —— 收益归 :mod:`cpt.backtest.runner`。

## 前视不变式（最重要的一条）

第 ``t`` 根下产生的信号，**只许看到 ``bars[:t+1]``**。这是回测唯一
真正容易作弊的地方：一旦某个环节偷偷用了 ``bars[t+1:]``，任何策略都会
显得神准。:func:`cpt.backtest.runner` 里有对账测试钉住这一点。

## 为什么是 O(n) 而不是 O(n²)

R70 每根都从 ``bars[:t+1]`` 重算一遍缠论结构，5,131 只票 × 527 根跑了一小时。
这里在 ``WARMUP`` 之后的每根仍要重算（缠论结构本身非增量），但把
**同一结构的状态机**放在外面：``structure_id`` 相同就沿用上一状态，
只在真正发生跃迁时产出事件。这样同一结构不会因为「每天都满足
structure_ready」而被记成 400 次。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "DEFAULT_MIN_BI_LEN",
    "DEFAULT_WARMUP",
    "SignalAt",
    "SignalTrace",
    "empty_trace",
    "structure_signals",
    "trace_summary",
]

#: 结构层冷启动需要的根数。R70 实测：少于 250 根时笔的条数不稳定，
#: 中枢数也跟着跳。与 :data:`cpt.backtest.panel.load_panel` 的 ``min_bars`` 同值。
DEFAULT_WARMUP: Final[int] = 250

#: 生产默认值（``NativeChanlunBackend(min_bi_len=6)``）。
DEFAULT_MIN_BI_LEN: Final[int] = 6


@dataclass(frozen=True)
class SignalAt:
    """某一根 bar 上产生的**一次状态跃迁**。

    Attributes:
        index: 在面板里的 bar 下标（``t``）。
        date_ms: 该 bar 的日期（Unix 毫秒）。
        status: ``structure_ready`` / ``confirmed`` / ``invalidated`` 等。
        structure_id: 哪个结构。同一结构多次跃迁共享同一个 id。
        divergence_status: ``detected`` / ``not_detected`` / ``not_checked``。
        prev_status: 跃迁前的状态（``None`` = 首次出现）。
    """

    index: int
    date_ms: int
    status: str
    structure_id: str
    divergence_status: str
    prev_status: str | None


@dataclass(frozen=True)
class SignalTrace:
    """一只票的全部跃迁。"""

    code: str
    bars_count: int
    signals: tuple[SignalAt, ...]
    #: 每个 ``status`` 出现次数（含每一天都重复出现的，不是跃迁数）。
    status_counts: dict[str, int]
    #: 算不出结构的 bar 数（笔为空 / 数据不足）。这些天不算「没有信号」，
    #: 是「**没算**」—— 两者混在一起会让样本量看起来比实际大。
    skipped: int


def structure_signals(
    bars: Sequence[Any],
    *,
    code: str,
    backend: Any,
    config: Any,
    warmup: int = DEFAULT_WARMUP,
) -> SignalTrace:
    """逐根推进，记录信号跃迁。**只读 ``bars[:t+1]``**。

    Args:
        bars: 该票的完整 bar 序列（``CanonicalBar``），按时间升序。
        code: 6 位纯代码。
        backend: 缠论后端（有 ``compute_structures(bars, config)``）。
        config: 后端配置（``ReferenceChanlunConfig``）。
        warmup: 前多少根只用于冷启动，不出信号。

    Returns:
        :class:`SignalTrace`。
    """
    # 延迟导入：本模块被 tests 直接调用时不希望拖进适配层依赖。
    from cpt.adapters.reference_chanlun import map_bi, map_zhongshu
    from cpt.application.first_buy_bridge import derive_first_buy_facts
    from cpt.domain.signal import assess_first_buy

    n = len(bars)
    signals: list[SignalAt] = []
    counts: dict[str, int] = {}
    skipped = 0
    #: ``structure_id -> (上次状态, 上次 divergence_status)``。
    #: 状态没变就不产出事件 —— 否则同一个结构会每天报一次。
    last: dict[str, tuple[str, str]] = {}

    for t in range(warmup, n):
        sub = list(bars[: t + 1])
        result = backend.compute_structures(sub, config)
        bis = [
            map_bi(b, level=0, source_ids=(f"fx:{b.start_bar}", f"fx:{b.end_bar}"), bars=sub)
            for b in result.bi_list
        ]
        if not bis:
            skipped += 1
            continue
        zs = [
            map_zhongshu(z, level=0, source_ids=tuple(f"bi:{i}" for i in z.bi_indices), bars=sub)
            for z in result.zs_list
        ]
        direction = bis[-1].direction
        facts = derive_first_buy_facts(
            level=0, trend_direction=direction, bis=bis, zhongshus=zs, code=code
        )
        if facts is None:
            skipped += 1
            continue

        structure_id = facts.structure_id or f"level0:empty:{code}"
        signal = assess_first_buy(
            level=0,
            structure_id=structure_id,
            center_ids=facts.center_ids,
            trend_direction=direction,
            has_two_centers=facts.has_two_centers,
            has_divergence_leg=facts.has_divergence_leg,
            has_reversal_bi=facts.has_reversal_bi,
            divergence_status=facts.divergence_status,
            price=float(sub[-1].close),
        )
        counts[signal.status] = counts.get(signal.status, 0) + 1

        previous = last.get(structure_id)
        prev_status = previous[0] if previous else None
        # ``invalidated`` 是终态：assess_first_buy 的 previous 参数在真实链路里
        # 由调用方从库里读，这里用内存里的上一状态代替。两者语义一致。
        if previous is not None and previous[0] == signal.status:
            # 状态没变 —— 但背驰态可能变了（not_checked -> detected），
            # 那种情况下游要用到，也记一条事件。
            if previous[1] == facts.divergence_status:
                continue
        last[structure_id] = (signal.status, facts.divergence_status)
        signals.append(
            SignalAt(
                index=t,
                date_ms=_date_ms(bars[t]),
                status=signal.status,
                structure_id=structure_id,
                divergence_status=facts.divergence_status,
                prev_status=prev_status,
            )
        )

    return SignalTrace(
        code=code,
        bars_count=n,
        signals=tuple(signals),
        status_counts=counts,
        skipped=skipped,
    )


def _date_ms(bar: Any) -> int:
    """``CanonicalBar.close_time`` 是**毫秒** epoch（UTC 当日 23:59:59.999）。

    第一版按秒处理（``fromtimestamp(close_time)``）直接抛
    ``ValueError: year must be in 1..9999, not 56585`` —— 这个坑 R70 探针
    也踩过。这里直接取毫秒、不再转 datetime，避开二次换算。
    """
    raw = int(bar.close_time)
    # 防御：万一上游哪天换成秒级，按量级区分（2020 年的毫秒是 1.5e12）。
    return raw if raw > 10**11 else raw * 1000


def empty_trace(code: str) -> SignalTrace:
    return SignalTrace(code=code, bars_count=0, signals=(), status_counts={}, skipped=0)


def trace_summary(traces: Sequence[SignalTrace]) -> dict[str, Any]:
    """把一批 ``SignalTrace`` 汇总成计数，便于在报告里给一行数字。"""
    totals: dict[str, int] = {}
    transition_totals: dict[str, int] = {}
    skipped = 0
    bars = 0
    for trace in traces:
        bars += trace.bars_count
        skipped += trace.skipped
        for status, cnt in trace.status_counts.items():
            totals[status] = totals.get(status, 0) + cnt
        for sig in trace.signals:
            transition_totals[sig.status] = transition_totals.get(sig.status, 0) + 1
    return {
        "codes": len(traces),
        "bars": bars,
        #: 每天的状态计数（同一天可能被计多次 —— 多个结构并存）。
        "status_days": dict(sorted(totals.items())),
        #: 真正的跃迁次数（同一结构状态不变不记）。
        "transitions": dict(sorted(transition_totals.items())),
        "skipped_bars": skipped,
    }
