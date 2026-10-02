"""把**一轮计算的水位 + 指纹**落成一行（``cpt_run_metric``）。

## 为什么要在 application 层单独一个模块

两处产出快照（``cpt/web/__main__.py`` 的加密侧、A 股路由）都要记同一份东西，
而 SQL 必须留在 storage。于是：

- **行怎么算**（从快照里抽哪些字段、health 怎么判）= application 的事 → 本模块；
- **行怎么写**（SQL、事务）= storage 的事 → :mod:`cpt.storage.run_metric_store`；
- **连接从哪来**：照 :mod:`cpt.application.structure_event_recorder` 的做法，
  加密侧本身没有 PG 连接，由这里现开一条（观测失败绝不能拖垮计算路径）。

## health 是三态不是两态（R38 的核心结论之一）

- ``ok``：水位正常；
- ``degraded``：**配置导致的已知降级**（czsc 未装、ccxt 未装、wind 跳过探测）——
  这是**常态**，不是事件，R38 实测它占日志 41%，把真信号淹了；
- ``failing``：数据不完整（缺口 / 过期 / 没有 bar）—— 这一档才该叫人起床。

判据只认**数据事实**（``data_quality`` 与 bar 数），不认"某个依赖没装"。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final

from cpt.storage.run_metric_store import KIND_RUN, RunMetric, append_metrics

_LOG = logging.getLogger(__name__)

__all__ = [
    "HEALTH_DEGRADED",
    "HEALTH_FAILING",
    "HEALTH_OK",
    "MetricRecorder",
    "metric_from_snapshot",
]

HEALTH_OK: Final[str] = "ok"
HEALTH_DEGRADED: Final[str] = "degraded"
HEALTH_FAILING: Final[str] = "failing"

# 注：「水位流断了多久算异常」的阈值不在这里 —— 那是**巡检的策略**（要看轮询周期与
# 冗余），放在 :mod:`scripts.run_inspection`；本模块只负责如实记一行水位。


def _num(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def metric_from_snapshot(
    snapshot: dict[str, Any],
    *,
    market: str,
    symbol: str,
    backend: str,
    fractal_count: int = 0,
    bi_count: int = 0,
    zhongshu_count: int = 0,
    trend_type_count: int = 0,
) -> RunMetric:
    """从快照抽出一行水位 + 指纹（**纯函数**，不碰 DB，好测）。"""
    runtime = snapshot.get("runtime") or {}
    quality = snapshot.get("data_quality") or {}
    repro = snapshot.get("reproducibility") or {}
    candles = snapshot.get("candles") or []
    overlays = snapshot.get("overlays") or {}

    bar_count = len(candles) if candles else _num(runtime.get("buffer_size"))
    last_bar_time = _num(candles[-1].get("open_time")) if candles else 0
    gap_count = _num(quality.get("gap_count"))
    stale = bool(quality.get("stale")) or bool(runtime.get("stale"))

    # 结构计数：优先用显式传入的（快照里 overlays 是 asdict 后的 dict，不好数）
    if not bi_count:
        bi_count = _len_of(overlays.get("bis"))
    if not fractal_count:
        fractal_count = _len_of(overlays.get("fractals"))
    if not zhongshu_count:
        zhongshu_count = _len_of(overlays.get("zhongshus"))
    if not trend_type_count:
        trend_type_count = _len_of(overlays.get("trend_types"))

    if bar_count <= 0:
        health = HEALTH_FAILING
    elif stale or gap_count:
        health = HEALTH_FAILING
    else:
        health = HEALTH_OK

    return RunMetric(
        kind=KIND_RUN,
        market=market,
        symbol=str(symbol),
        config_hash=str(repro.get("config_hash", "")),
        dataset_hash=str(repro.get("dataset_hash", "")),
        rules_version=str(repro.get("rules_version", "")),
        # R36 补的第一块：没有它，"装个 czsc 就静默切生产后端"这种事查不出来
        backend=str(backend),
        bar_count=bar_count,
        fractal_count=fractal_count,
        bi_count=bi_count,
        zhongshu_count=zhongshu_count,
        trend_type_count=trend_type_count,
        last_bar_time=last_bar_time,
        gap_count=gap_count,
        stale=stale,
        factor_coverage=_float(quality.get("factor_coverage")) or 1.0,
        snapshot_age_ms=_num(runtime.get("snapshot_age_ms")),
        health=health,
        detail=json.dumps(
            {
                "status": runtime.get("status"),
                "data_source": runtime.get("data_source"),
                "interval": runtime.get("interval"),
                "schema_version": snapshot.get("schema_version"),
            },
            ensure_ascii=False,
        ),
    )


def _len_of(value: Any) -> int:
    return len(value) if isinstance(value, (list, tuple, dict)) else 0


@dataclass
class MetricRecorder:
    """持有连接的水位记录器（连接可注入，测试用假连接即可）。"""

    conn: Any

    def record(
        self,
        snapshot: dict[str, Any],
        *,
        market: str,
        symbol: str,
        backend: str,
        **counts: int,
    ) -> RunMetric:
        row = metric_from_snapshot(
            snapshot, market=market, symbol=symbol, backend=backend, **counts
        )
        append_metrics(self.conn, [row])  # 不 commit：边界归调用方
        return row

    def record_and_commit(self, snapshot: dict[str, Any], **kwargs: Any) -> RunMetric:
        row = self.record(snapshot, **kwargs)
        self.conn.commit()
        return row
