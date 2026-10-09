"""Dashboard v2 composition service for research and watch views."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from cpt.application.dashboard import build_dashboard_snapshot
from cpt.application.dashboard_indicators import macd_series
from cpt.application.dashboard_levels import level_tree
from cpt.application.dashboard_reproducibility import (
    RULES_VERSION,
    reproducibility_metadata,
)
from cpt.application.dashboard_runtime import runtime_panel
from cpt.application.dashboard_watch import watch_metrics
from cpt.domain.config import RulesConfig
from cpt.domain.models import Bi, CanonicalBar, Fractal, Signal, StructureEvent, TrendType, ZhongShu

SCHEMA_VERSION = "dashboard.v2"


def _watch_payload(bars: Sequence[CanonicalBar]) -> dict[str, Any]:
    """看盘窗口指标：无 K 线时显式不可用，绝不返回一堆 ``None`` 冒充有效数据。

    上游 ``watch_metrics`` 对空序列也返回全 ``None`` 的字典，但前端无法区分
    "没数据" 和 "有数据但算不出"；这里用 ``available`` 明确表态。
    """
    if not bars:
        return {"available": False, "reason": "bars_unavailable"}
    return {**watch_metrics(bars), "available": True}


def _level_tree_payload(
    fractals: Sequence[Fractal],
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    trend_types: Sequence[TrendType],
) -> dict[str, Any]:
    """级别递归树：四类结构（``asdict`` 后）交给 ``level_tree`` 按 ``level`` 分组。

    四类结构全空即整块不可用 —— 空树与"没有叠加层"是两码事，不能混为一谈。
    ``level_tree`` 自身会跳过 ``level`` 非 int 的元素，故此处只需给出原始序列。
    """
    # 四类结构**分别**展开：写成一个元组再迭代会让 mypy 把元素并集退化成
    # ``object``，``asdict`` 于是在 ``DataclassInstance`` 重载上失配。
    structures: list[dict[str, Any]] = []
    structures.extend(asdict(item) for item in fractals)
    structures.extend(asdict(item) for item in bis)
    structures.extend(asdict(item) for item in zhongshus)
    structures.extend(asdict(item) for item in trend_types)
    if not structures:
        return {"available": False, "reason": "overlays_unavailable"}
    return {"available": True, "levels": list(level_tree(structures))}


def build_dashboard_snapshot_v2(
    config: RulesConfig,
    bars: Sequence[CanonicalBar],
    *,
    fractals: Sequence[Fractal] = (),
    bis: Sequence[Bi] = (),
    zhongshus: Sequence[ZhongShu] = (),
    trend_types: Sequence[TrendType] = (),
    signal: Signal | None = None,
    signal_first_sell: Signal | None = None,
    events: Sequence[StructureEvent] = (),
    mode: str = "research",
    status: str = "confirmed",
    data_source: str = "fixture",
    parity: dict[str, Any] | None = None,
    market_24h: dict[str, Any] | None = None,
    multi_level: dict[str, Any] | None = None,
    config_compare: dict[str, Any] | None = None,
    runtime: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Compose v2 while retaining all stable v1 fields."""
    v1 = build_dashboard_snapshot(
        config,
        bars,
        fractals,
        bis,
        zhongshus,
        trend_types,
        signal,
        events,
        mode,
        status,
        data_source,
        runtime=runtime,
    )
    v2: dict[str, Any] = dict(v1)
    v2["schema_version"] = SCHEMA_VERSION
    v2["indicators"] = {"macd": list(macd_series(bars, config))}
    v2["market_24h"] = market_24h or {
        "available": False,
        "reason": "upstream_aggregate_unavailable",
    }
    # R59（审计 M26）：**显式**传入口径号（``rules.v{SCHEMA_VERSION}``），
    # 不再依赖函数默认值 —— 原默认值硬编码 "rules.v0"，而领域规则已是 v1。
    # 显式传参让「这行依赖领域 SCHEMA_VERSION」在调用点就看得见。
    v2["reproducibility"] = reproducibility_metadata(v2, config=config, rules_version=RULES_VERSION)
    v2["parity"] = parity or {"available": False, "reason": "oracle_reference_unavailable"}
    v2["runs"] = []
    v2["multi_level"] = multi_level or {
        "available": False,
        "reason": "multi_level_unavailable",
    }
    # 三条活路径（fixture/realtime/A 股）都汇聚到本函数，故在此统一补齐
    # watch_metrics / level_tree —— 放 v1 会拿不到已归类的结构序列。
    v2["watch_metrics"] = _watch_payload(bars)
    v2["level_tree"] = _level_tree_payload(fractals, bis, zhongshus, trend_types)
    v2["config_compare"] = config_compare or {
        "available": False,
        "reason": "config_compare_unavailable",
    }
    runtime_v1 = v1["runtime"] if isinstance(v1["runtime"], dict) else {}
    v2["runtime"] = {**runtime_v1, "mode": mode, "status": status, "data_source": data_source}
    v2["engine_state"] = runtime_panel(v2["runtime"])
    v2["summary"] = {
        "structure_counts": {
            "fractals": len(fractals),
            "bis": len(bis),
            "zhongshus": len(zhongshus),
            "trend_types": len(trend_types),
        },
        "signal": asdict(signal) if signal else None,
        "signal_first_sell": asdict(signal_first_sell) if signal_first_sell else None,
    }
    return v2
