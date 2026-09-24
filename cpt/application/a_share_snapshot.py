"""A 股 dashboard snapshot 构造（R16 落地，R17-3 从 ``cpt.web.a_share`` 迁出）。

放在 application 层的原因：这份构造逻辑有**两个**调用方 —— 独立的 A 股服务
（``python -m cpt.web.a_share``，默认 8011）和主看板 app 的
``/api/dashboard/a-share/snapshot`` 路由。留在 web 层会让两个 web 模块互相
import，也会让 application 依赖 web（import-linter 的 Engine 契约不允许）。

## 数据流
1. ``AShareLocalClient.fetch_validated_klines(code, start_ms, end_ms)`` → 不复权
   原始 ``CanonicalBar`` 列表（本地库口径实测 = 腾讯 ``bfq``）；
2. ``validate_ashare_bars`` → 允许周末/节假日/停牌的日历缺口；
3. ``compute_domain_structures`` → 缠论结构（分型/笔/中枢，**dataclass** 对象）；
4. ``build_dashboard_snapshot_v2`` → 与加密侧**完全同构**的 v2 snapshot。

第 4 步的同构性是关键：主看板的四个画布（R16）只认 v2 snapshot，所以 A 股接进来
**不需要改画布任何一行代码**。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from cpt.adapters.a_share_local import (
    AShareLocalClient,
    AShareNoDataError,
    AShareNoFactorError,
)
from cpt.adapters.backend_factory import DEFAULT_BACKEND, resolve_backend
from cpt.adapters.reference_chanlun import ChanlunBackend
from cpt.adapters.validators import validate_ashare_bars
from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.application.replay import compute_domain_structures
from cpt.domain.config import RulesConfig

__all__ = [
    "DEFAULT_WIDTH_K",
    "build_ashare_snapshot",
    "empty_ashare_snapshot",
]

_LOG = logging.getLogger("cpt.application.a_share_snapshot")

#: A 股日线固定 1d 间隔（24h）。与 ``BinanceFuturesClient.INTERVAL_MS["1d"]`` 同值。
INTERVAL_MS: int = 24 * 3600 * 1000

#: A 股展示周期默认根数。
#:
#: 30 根（≈1.5 个月）实测只能出 2 笔（``000002``），笔/中枢的形态根本看不出来；
#: 120 根（≈半年）才是缠论结构的可读尺度。实测带因子的 61 只**全部**有 ≥250 根
#: 历史，所以调大默认值零覆盖损失。
DEFAULT_WIDTH_K: int = 120


def build_ashare_snapshot(
    code: str,
    *,
    width_k: int = DEFAULT_WIDTH_K,
    client: AShareLocalClient | None = None,
    backend: ChanlunBackend | None = None,
) -> dict[str, Any]:
    """为 ``code`` 构造 dashboard snapshot（v2 schema，与加密侧同）。

    Args:
        code: A 股 6 位裸码（如 ``"600519"``）。
        width_k: 最近多少根 K 线（默认 120，≈半年）。
        client: 可选注入的 :class:`AShareLocalClient`；不传则 lazy 默认连接
            （需要运行 venv 装 psycopg，``pip install -e ".[db]"``）。
        backend: 缠论后端；``None`` 时走 ``auto`` 档（装了 czsc 就用 czsc）。

    Returns:
        v2 snapshot；任何失败都返回 **degraded 占位快照**而不是抛异常 —— 但
        ``data_quality.reason`` 会写明原因，绝不静默成"正常但没数据"。
    """
    owns_client = client is None
    if owns_client:
        active_client: AShareLocalClient = AShareLocalClient()
    else:
        assert client is not None  # type assertion only — mypy 收窄
        active_client = client
    try:
        end_ms = int(datetime.now(UTC).timestamp() * 1000)
        # 多预留 60 根以保证缠论结构稳定
        start_ms = end_ms - (width_k + 60) * INTERVAL_MS
        result = active_client.fetch_validated_klines(code, start_ms, end_ms)
        canonical = list(result.bars)
        if not canonical:
            # 缺复权因子是最常见的原因（全库 5225 只只有 94 只有因子），
            # 必须把"为什么没有"带到前端，而不是画一张空图。
            reason = "no_factor" if getattr(result, "skipped_no_factor", ()) else "no_data"
            return empty_ashare_snapshot(code, reason)
    except AShareNoFactorError as exc:
        # 最常见的一种失败（全库 5225 只只有 94 只有因子）。必须与"没数据"和
        # "DB 挂了"分开报 —— 报成 db_error 会把排查方向带偏（实测踩过）。
        _LOG.info("A 股缺因子 %s: %s", code, exc)
        return empty_ashare_snapshot(code, "no_factor")
    except AShareNoDataError as exc:
        _LOG.info("A 股无行情 %s: %s", code, exc)
        return empty_ashare_snapshot(code, "no_data")
    except Exception as exc:  # noqa: BLE001
        # DB 不可达 → 返回 degraded snapshot，**不静默成 OK**
        _LOG.warning("A 股 DB 拉取失败 %s: %s", code, exc)
        return empty_ashare_snapshot(code, f"db_error:{type(exc).__name__}")
    finally:
        if owns_client:
            active_client.close()

    # A 股走专用校验（允许周末/节假日/停牌的日历缺口，见
    # cpt.adapters.validators.validate_ashare_bars）。
    # 不能用 replay_bars —— 它的连续性契约是加密市场假设，A 股会误报 DataGapError。
    try:
        validated = validate_ashare_bars(canonical, interval_ms=INTERVAL_MS)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("A 股序列校验失败 %s: %s", code, exc)
        return empty_ashare_snapshot(code, f"invalid_bars:{type(exc).__name__}")

    # 用 ``compute_domain_structures`` 拿 **dataclass** 结构对象（分型/笔/中枢）。
    # 不能改用 ``run_replay``：它返回 ``export_dataset`` 的 schema v1 payload，
    # 结构元素在 ``payload["data"]`` 下且已 ``asdict`` 成普通 dict —— 直接喂给
    # ``build_dashboard_snapshot_v2`` 会在 ``asdict(f)`` 处炸
    # ``TypeError: asdict() should be called on dataclass instances``；写成
    # ``payload.get("fractals")`` 更糟：静默 ``None`` → overlays 全空、K 线上
    # 一条笔都不画（实测两者都踩过）。
    # 走势类型（``trend_types``）是 CPT 自研、尚未接入 replay，保持空元组。
    active_backend = backend or resolve_backend(
        DEFAULT_BACKEND, min_bi_len=RulesConfig().min_bi_len
    )
    fractals, bis, zhongshus = compute_domain_structures(validated, RulesConfig(), active_backend)
    snapshot = build_dashboard_snapshot_v2(
        config=RulesConfig(),
        bars=validated,
        fractals=fractals,
        bis=bis,
        zhongshus=zhongshus,
        trend_types=(),
        mode="watch",
        status="confirmed",
        data_source="db_local",
        runtime={
            "data_source": "db_local",
            "symbol": code,
            "interval": "1d",
            "status": "confirmed",
            "interval_ms": INTERVAL_MS,
            "as_of_ms": int(datetime.now(UTC).timestamp() * 1000),
        },
    )
    # 显式覆盖（v1 默认 BTCUSDT）— A 股代码在 market.symbol
    snapshot["market"]["symbol"] = code
    snapshot["market"]["kind"] = "a_share"
    snapshot["market"]["interval"] = "1d"
    return snapshot


def empty_ashare_snapshot(code: str, reason: str) -> dict[str, Any]:
    """DB 真空 / 缺因子时的占位 snapshot（前端可识别为"无数据"）。

    ``reason`` 是给用户看的，必须具体：``no_factor``（缺复权因子）、``no_data``、
    ``db_error:*``、``invalid_bars:*``。
    """
    return {
        "schema_version": "dashboard.v2",
        "market": {
            "symbol": code,
            "kind": "a_share",
            "interval_ms": INTERVAL_MS,
            "bar_count": 0,
        },
        "candles": [],
        # 形状必须与真实路径一致（dict of 4 keys），否则前端在
        # degraded 分支会崩（实测踩到：空快照曾返回 []）
        "overlays": {"fractals": [], "bis": [], "zhongshus": [], "trend_types": []},
        "signal": None,  # 真实路径无信号时也是 None（保持一致）
        "events": [],
        "data_quality": {"degraded": True, "reason": reason},
        "runtime": {
            "as_of_ms": int(datetime.now(UTC).timestamp() * 1000),
            "interval_ms": INTERVAL_MS,
            "interval": "1d",
            "degraded": True,
            "degraded_reason": reason,
            "data_source": "db_local",
            "status": "empty",
        },
        "config": RulesConfig().to_dict(),
    }
