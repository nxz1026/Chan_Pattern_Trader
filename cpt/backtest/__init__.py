"""回测层（``cpt/backtest``）。**R70 之后才建**，用法见 :mod:`cpt.backtest.panel`。

## 为什么到 R70 才有这一层

R70 用 10 个临时探针跑了 688,496 次评估，得到三条结论，也踩了三条坑。
这一层是把那些探针**固化成可复用的东西**，重点不在于「证明策略好」，
而在于**让「证明它不好」变得便宜**：

1. **口径必须可复现**。探针每次重写一遍加载逻辑，同一份数据两次跑出不同
   样本量（R70 首轮 4,129 个 structure_ready，重跑 4,132 个）。
2. **不可成交的样本必须能排除**。A 股涨停买不进、跌停卖不出。R70 的基线
   +0.479%（20 日）里混着涨停次日的 +2.00% —— 那不是可执行收益。
   本层用 ``public.derived_bar`` 的 ``is_limit_up`` / ``is_limit_down`` 打标，
   **默认排除**（见 :func:`cpt.backtest.panel.load_panel` 的 ``tradable_only``）。
3. **统计单元要一致**。R70 报过的「structure_ready 20 日 +0.481%」是样本
   单元错配（基线 130 万根 K，事件组 4 千个）—— 本层一律用**事件样本**，
   不拿全量 K 线当基线。

## 什么时候不该用这一层

- 想验证「某个结构之后会不会涨」→ **不要**用。R70 已实测一买无 alpha，
  这类问题的答案不是回测能救的。
- 想跑实盘 → 不能。本层没有撮合、没有滑点、没有仓位。
"""

from __future__ import annotations

from cpt.backtest.panel import HORIZONS, BarPanel, load_panel
from cpt.backtest.runner import (
    A_STATUS,
    B_STATUS,
    BacktestResult,
    StatusSamples,
    collect_status_samples,
    run_backtest,
    status_samples,
)
from cpt.backtest.signals import SignalAt, SignalTrace, structure_signals, trace_summary
from cpt.backtest.stats import (
    Comparison,
    Sample,
    Summary,
    compare,
    excess_over_market,
    paired,
    summarise,
)

__all__ = [
    "A_STATUS",
    "B_STATUS",
    "HORIZONS",
    "BacktestResult",
    "BarPanel",
    "Comparison",
    "Sample",
    "SignalAt",
    "SignalTrace",
    "StatusSamples",
    "Summary",
    "collect_status_samples",
    "compare",
    "excess_over_market",
    "load_panel",
    "paired",
    "run_backtest",
    "status_samples",
    "structure_signals",
    "summarise",
    "trace_summary",
]
