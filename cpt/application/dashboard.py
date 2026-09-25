"""Dashboard 只读 ViewModel（``dashboard.v1``，``docs/dashboard-plan.md`` D1）。

把一次计算的内部 dataclass（K 线 + 分型/笔/中枢/走势类型 + 一买信号 + 结构事件）
投影为**稳定、可 JSON 化、带 schema 版本**的 ``dict``，供上层 HTTP 层直接返回，
前端不接触领域对象。D0 已确认本仓库是纯 Python、无 Web 框架，故这里只交付
application service，不引入任何 HTTP/第三方依赖。

冻结契约（``docs/dashboard-plan.md`` D1）：

* 顶层固定 8 个键：``schema_version`` / ``market`` / ``candles`` / ``overlays`` /
  ``signal`` / ``events`` / ``data_quality`` / ``runtime``；
* 时间统一 Unix 毫秒，价格/量沿用 ``CanonicalBar`` 原始单位，不做换单位；
* 结构对象保留 ``level`` / ``source_ids``（``asdict`` 原样透出），
  走势类型额外保留 ``kind`` / ``direction``；
* 未收盘状态不得伪装成 ``confirmed``：``status`` 由调用方按未收盘 ``"alert"`` /
  已收盘 ``"confirmed"`` 传入（本模块只透传，不推断）；
* 缺口、stale 必须显式返回，不用空数组掩盖；
* 空输入（无 K 线）返回结构完整、可 JSON 序列化的载荷，不做异常分支。

只读性：所有输入仅被读取，函数不修改任何传入对象或序列；输出为全新 ``dict``，
不含对输入 dataclass 的引用。

示例：

.. code-block:: python

    from cpt.application.dashboard import build_dashboard_snapshot, dashboard_json

    snapshot = build_dashboard_snapshot(config, bars, mode="realtime", status="alert")
    body = dashboard_json(snapshot)  # 稳定：同输入两次调用字节相同
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from itertools import pairwise
from typing import Final

from cpt.domain.config import RulesConfig
from cpt.domain.models import (
    Bi,
    CanonicalBar,
    Fractal,
    Signal,
    StructureEvent,
    TrendType,
    ZhongShu,
)

__all__ = [
    "DASHBOARD_SCHEMA_VERSION",
    "DEFAULT_SYMBOL",
    "build_dashboard_snapshot",
    "dashboard_json",
]

#: Dashboard snapshot schema 版本号，写入每个 snapshot 的顶层字段。
DASHBOARD_SCHEMA_VERSION: Final[str] = "dashboard.v1"

#: 默认交易对：``CanonicalBar`` 不含 symbol，故由调用方显式覆盖。
DEFAULT_SYMBOL: Final[str] = "BTCUSDT"

#: 毫秒/分钟换算：``RulesConfig.levels`` 的单位是分钟。
_MS_PER_MINUTE: Final[int] = 60_000


def _bar_to_dict(bar: CanonicalBar) -> dict[str, object]:
    """``CanonicalBar`` → dashboard candle 对象（补 ``direction`` 派生字段）。"""
    data: dict[str, object] = asdict(bar)
    data["direction"] = bar.direction
    return data


def _infer_interval_ms(bars: Sequence[CanonicalBar], config: RulesConfig) -> int:
    """推断契约周期（毫秒）。

    取相邻 ``open_time`` 的**最小正间隔**：缺口表现为更大的间隔，取最小值
    才不会把缺口误当成周期；不足两根（无正间隔）时回落到配置的名义周期
    ``config.levels[0]`` 分钟 —— 此时没有任何相邻关系可推断。
    """
    positive_gaps = [
        current.open_time - previous.open_time
        for previous, current in pairwise(bars)
        if current.open_time > previous.open_time
    ]
    if positive_gaps:
        return min(positive_gaps)
    return config.levels[0] * _MS_PER_MINUTE


def _market(bars: Sequence[CanonicalBar], config: RulesConfig, symbol: str) -> dict[str, object]:
    """行情概览：symbol / 周期 / 最新价 / 时间范围 / 根数。

    ``last_price`` 取序列末根的 ``close``（调用方保证按 ``open_time`` 递增）；
    空序列时价格与时间为 ``None``，由前端渲染 ``empty`` 状态，而不是伪造 0。
    """
    return {
        "symbol": symbol,
        "kind": "crypto",  # 默认加密；A 股侧由 cpt/web/a_share.py 覆盖为 "a_share"
        "interval_ms": _infer_interval_ms(bars, config),
        "last_price": bars[-1].close if bars else None,
        "first_open_time": bars[0].open_time if bars else None,
        "last_open_time": bars[-1].open_time if bars else None,
        "bar_count": len(bars),
    }


def _overlays(
    fractals: Sequence[Fractal],
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    trend_types: Sequence[TrendType],
) -> dict[str, object]:
    """缠论结构叠加层：四类结构各按 ``asdict`` 原样透出（含 ``level`` / ``source_ids``）。"""
    return {
        "fractals": [asdict(f) for f in fractals],
        "bis": [asdict(b) for b in bis],
        "zhongshus": [asdict(z) for z in zhongshus],
        "trend_types": [asdict(t) for t in trend_types],
    }


def _data_quality(bars: Sequence[CanonicalBar], stale: bool, gap: bool) -> dict[str, object]:
    """数据质量：外部判定标记 + 序列内已收盘/未收盘根数。"""
    bar_count = len(bars)
    closed_bar_count = sum(1 for bar in bars if bar.is_closed)
    return {
        "stale": stale,
        "gap": gap,
        "closed_bar_count": closed_bar_count,
        "unclosed_bar_count": bar_count - closed_bar_count,
    }


def _runtime(
    mode: str,
    status: str,
    data_source: str,
    runtime: dict[str, object] | None,
) -> dict[str, object]:
    """运行态：显式三字段 + 调用方附加信息（同键覆盖，不同键追加）。"""
    merged: dict[str, object] = {
        "mode": mode,
        "status": status,
        "data_source": data_source,
    }
    if runtime is not None:
        merged.update(runtime)
    return merged


def build_dashboard_snapshot(
    config: RulesConfig,
    bars: Sequence[CanonicalBar],
    fractals: Sequence[Fractal] = (),
    bis: Sequence[Bi] = (),
    zhongshus: Sequence[ZhongShu] = (),
    trend_types: Sequence[TrendType] = (),
    signal: Signal | None = None,
    events: Sequence[StructureEvent] = (),
    mode: str = "offline",
    status: str = "confirmed",
    data_source: str = "fixture",
    stale: bool = False,
    gap: bool = False,
    runtime: dict[str, object] | None = None,
    symbol: str = DEFAULT_SYMBOL,
) -> dict[str, object]:
    """构造 ``dashboard.v1`` 只读 snapshot（稳定 JSON 兼容 ``dict``）。

    Args:
        config: 规则口径配置；仅用于在 K 线不足以推断周期时取名义周期。
        bars: 规范化 K 线，按 ``open_time`` 递增，可含未收盘尾根。
        fractals: 分型叠加层，默认空。
        bis: 笔叠加层，默认空。
        zhongshus: 笔中枢叠加层，默认空。
        trend_types: 走势类型叠加层，默认空。
        signal: 一买信号；``None`` 表示尚未产生，输出 JSON ``null``。
        events: 结构事件时间线，默认空。
        mode: 运行模式（如 ``"offline"`` / ``"realtime"``），透传不改写。
        status: 数据状态，未收盘必须传 ``"alert"``。
        data_source: 数据来源标记（如 ``"fixture"`` / ``"binance_futures"``）。
        stale: 数据是否陈旧（由调用方判定）。
        gap: 序列是否存在缺口（由调用方判定）。
        runtime: 附加运行态字段；同键覆盖 ``mode`` / ``status`` / ``data_source``。
        symbol: 交易对标识；``CanonicalBar`` 不含 symbol，默认 ``BTCUSDT``。

    Returns:
        顶层固定 8 键的 ``dict``；不含时间戳/随机量，同一输入产出同一结果。

    Note:
        本函数**不校验** K 线契约（连续性/缺口/OHLC）。需要守卫时先经
        :func:`cpt.adapters.validators.validate_canonical_bars` 或
        :func:`cpt.application.replay.replay_bars` 处理。
    """
    return {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "market": _market(bars, config, symbol),
        "candles": [_bar_to_dict(bar) for bar in bars],
        "overlays": _overlays(fractals, bis, zhongshus, trend_types),
        "signal": asdict(signal) if signal is not None else None,
        "events": [asdict(event) for event in events],
        "data_quality": _data_quality(bars, stale, gap),
        "runtime": _runtime(mode, status, data_source, runtime),
    }


def dashboard_json(snapshot: dict[str, object]) -> str:
    """把 snapshot 序列化为 JSON 字符串（键排序、不转义非 ASCII、2 空格缩进）。

    重复调用同一 snapshot 产出字节相同；``allow_nan=False`` 使非法浮点
    （NaN/inf）直接报错而非写出非法 JSON。
    """
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
