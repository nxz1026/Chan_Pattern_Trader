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
    "fingerprint_from_snapshot",
    "metric_from_snapshot",
]

#: 结构变化的原因分类（R38 owner 拍板）。
#:
#: **为什么要有它**：``cpt_structure_event`` 记了 2575 行"什么结构变了"，但没说
#: **为什么**。而这四种原因对 Loop/LLM 的价值天差地别：
#:
#: - ``data``     —— 输入数据变了（K 线/因子），结构随之变，**不是算法问题**
#: - ``config``   —— 规则参数变了（``config_hash`` / ``rules_version``）
#: - ``backend``  —— 结构后端换了（R36 那次"装个 czsc 就静默切生产"就属这类）
#: - ``code``     —— 以上指纹都没变却仍变了 ⇒ **只能归到算法/代码自己**
#:
#: ⚠️ ``code`` 是**残差归因**，不是检测到的："代码变了"没法从数据里读出来 ——
#: 它表示"排除了其他三种，还是变了"。把它和真正检测到的三种混在一列会误导，
#: 所以本模块同时提供 :func:`explain_cause` 说明依据。
CAUSE_BACKEND: Final[str] = "backend"
CAUSE_CODE: Final[str] = "code"
CAUSE_CONFIG: Final[str] = "config"
CAUSE_DATA: Final[str] = "data"
CAUSES: Final[tuple[str, ...]] = (CAUSE_DATA, CAUSE_CONFIG, CAUSE_BACKEND, CAUSE_CODE)


def fingerprint_from_snapshot(snapshot: dict[str, Any], *, backend: str) -> dict[str, str]:
    """快照 → 可比较的指纹（四个字段，缺就是空串）。

    ``backend`` 只能由调用方给：它是**运行配置**（``resolve_backend`` 的结果），
    不在快照里 —— R36 的教训正是"后端换了但没人知道"。
    """
    repro = snapshot.get("reproducibility") or {}
    return {
        "config_hash": str(repro.get("config_hash", "")),
        "dataset_hash": str(repro.get("dataset_hash", "")),
        "rules_version": str(repro.get("rules_version", "")),
        "backend": str(backend or ""),
    }


def explain_cause(previous: dict[str, Any] | None, current: dict[str, Any]) -> str:
    """这一轮结构变了，原因最可能是什么（纯函数，好测）。

    判据按「确定性」从高到低：
    1. ``backend`` 变了 —— 换实现，最确定；
    2. ``config`` 变了（``config_hash`` 或 ``rules_version``）—— 换参数；
    3. ``data`` 变了（``dataset_hash``）—— 换输入；
    4. 都没有 ⇒ ``code``（残差：算法自己）。

    没有上一轮（``previous is None``）时返回 ``""`` —— **不猜**。首次运行本来
    就该是"全 created"，把它归到 ``code`` 会凭空指控算法。
    """
    if not previous:
        return ""
    if previous.get("backend") != current.get("backend"):
        return CAUSE_BACKEND
    if previous.get("config_hash") != current.get("config_hash") or previous.get(
        "rules_version"
    ) != current.get("rules_version"):
        return CAUSE_CONFIG
    if previous.get("dataset_hash") != current.get("dataset_hash"):
        return CAUSE_DATA
    return CAUSE_CODE


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
        # 不 commit：边界归调用方。
        #
        # ⚠️ R45 补 rollback：``append_metrics`` 是「吞异常」型（见其
        # ``# gate: allow-silent``），语句失败时它回 0 而不抛，连接就停在
        # ``current transaction is aborted`` 态。这里原来既不 catch 也不
        # rollback，而 ``record_and_commit`` 紧接着的 ``commit()`` 在 aborted
        # 事务上**不抛、等于 ROLLBACK** ⇒ 一次写失败会静默变成「什么都没记」，
        # 且这条连接后续全废。
        wrote = append_metrics(self.conn, [row])
        if not wrote:
            try:
                self.conn.rollback()
            except Exception as exc:  # noqa: BLE001 — 救不回来也别带崩主流程
                _LOG.warning("run_metric 落库失败后 rollback 也失败 %s/%s: %s", market, symbol, exc)
        return row

    def record_and_commit(self, snapshot: dict[str, Any], **kwargs: Any) -> RunMetric:
        row = self.record(snapshot, **kwargs)
        self.conn.commit()
        return row
