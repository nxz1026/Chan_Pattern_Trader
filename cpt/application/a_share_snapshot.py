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
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cpt.adapters.a_share_factor import (
    DEFAULT_FACTOR_DAYS,
    FactorEnsureResult,
    OnDemandFactorFetcher,
    fetch_factor_rows,
    upsert_factor_rows,
)
from cpt.adapters.a_share_local import (
    AShareLocalClient,
    AShareNoDataError,
    AShareNoFactorError,
)
from cpt.adapters.a_share_public import TENCENT_KLINE_URL
from cpt.adapters.backend_factory import DEFAULT_BACKEND, resolve_backend
from cpt.adapters.reference_chanlun import ChanlunBackend
from cpt.adapters.validators import validate_ashare_bars
from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.application.replay import compute_domain_structures
from cpt.domain.config import RulesConfig

__all__ = [
    "DEFAULT_WIDTH_K",
    "ENV_ONDEMAND_FACTOR",
    "FactorEnsurer",
    "build_ashare_snapshot",
    "factor_ensurer_from_env",
    "empty_ashare_snapshot",
    "reset_factor_fetcher",
]

#: 按需补因子的开关（``0``/``false``/``off`` 关闭）。
ENV_ONDEMAND_FACTOR: str = "CPT_ASHARE_ONDEMAND_FACTOR"

#: 按需补因子的钩子：给一个代码，返回拉取结果。``False`` 语义的关闭用 ``None`` 表达。
FactorEnsurer = Callable[[str], FactorEnsureResult]

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
    ensure_factors: FactorEnsurer | None = None,
) -> dict[str, Any]:
    """为 ``code`` 构造 dashboard snapshot（v2 schema，与加密侧同）。

    Args:
        code: A 股 6 位裸码（如 ``"600519"``）。
        width_k: 最近多少根 K 线（默认 120，≈半年）。
        client: 可选注入的 :class:`AShareLocalClient`；不传则 lazy 默认连接
            （需要运行 venv 装 psycopg，``pip install -e ".[db]"``）。
        backend: 缠论后端；``None`` 时走 ``auto`` 档（装了 czsc 就用 czsc）。
        ensure_factors: 按需补因子的钩子。``None``（默认）表示**不补** ——
            直接调用本函数永远不会联网或写库。生产入口传
            ``factor_ensurer_from_env(default=True)``。

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
    # 证券名称（如 600519 → 贵州茅台）。**纯展示信息**，取不到就不显示，
    # 绝不允许它把快照搞挂 —— 所以单独 try 住，且失败只记 debug。
    security = _resolve_security_name(active_client, code)
    security_name = security.name if security is not None else ""
    security_board = security.board if security is not None else None
    # 注意：``ensure_factors`` 为 ``None`` 时**不做**按需补因子（安全默认）。
    # 生产入口显式传入，见 factor_ensurer_from_env 的注释。
    ensurer = ensure_factors
    outcome: FactorEnsureResult | None = None
    try:
        end_ms = int(datetime.now(UTC).timestamp() * 1000)
        # 多预留 60 根以保证缠论结构稳定
        start_ms = end_ms - (width_k + 60) * INTERVAL_MS
        result = active_client.fetch_validated_klines(code, start_ms, end_ms)
        canonical = list(result.bars)
        if not canonical or _skipped_no_factor(result):
            # 本地因子不全（全库 5225 只只有 94 只有因子）→ 按需补一次再重读。
            # 触发条件同时覆盖"整段缺"和"部分缺"：部分缺会让 K 线序列出现空洞，
            # 画出来的笔/中枢是错的，比整段缺更隐蔽。
            outcome = _try_on_demand_factors(code, active_client, start_ms, end_ms, ensurer)
            if outcome is not None:
                result = active_client.fetch_validated_klines(code, start_ms, end_ms)
                canonical = list(result.bars)
        if not canonical:
            reason = "no_factor" if _skipped_no_factor(result) else "no_data"
            if outcome is not None and outcome.reason:
                reason = _reason_for_failure(outcome)
            snapshot = empty_ashare_snapshot(code, reason, name=security_name, board=security_board)
            _attach_factor_fetch(snapshot, outcome)
            return snapshot
    except AShareNoFactorError as exc:
        # 最常见的一种失败（全库 5225 只只有 94 只有因子）。必须与"没数据"和
        # "DB 挂了"分开报 —— 报成 db_error 会把排查方向带偏（实测踩过）。
        _LOG.info("A 股缺因子 %s: %s", code, exc)
        outcome = _try_on_demand_factors(code, active_client, start_ms, end_ms, ensurer)
        if outcome is not None and outcome.fetched:
            try:
                result = active_client.fetch_validated_klines(code, start_ms, end_ms)
                canonical = list(result.bars)
            except Exception as retry_exc:  # noqa: BLE001
                _LOG.warning("按需补因子后重读仍失败 %s: %s", code, retry_exc)
                snapshot = empty_ashare_snapshot(
                    code, "no_factor", name=security_name, board=security_board
                )
                _attach_factor_fetch(snapshot, outcome)
                return snapshot
        else:
            snapshot = empty_ashare_snapshot(
                code, _reason_for_failure(outcome), name=security_name, board=security_board
            )
            _attach_factor_fetch(snapshot, outcome)
            return snapshot
    except AShareNoDataError as exc:
        _LOG.info("A 股无行情 %s: %s", code, exc)
        return empty_ashare_snapshot(code, "no_data", name=security_name, board=security_board)
    except Exception as exc:  # noqa: BLE001
        # DB 不可达 → 返回 degraded snapshot，**不静默成 OK**
        _LOG.warning("A 股 DB 拉取失败 %s: %s", code, exc)
        return empty_ashare_snapshot(
            code, f"db_error:{type(exc).__name__}", name=security_name, board=security_board
        )
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
        return empty_ashare_snapshot(
            code, f"invalid_bars:{type(exc).__name__}", name=security_name, board=security_board
        )

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
    snapshot["market"]["name"] = security_name
    snapshot["market"]["board"] = security_board
    _attach_factor_fetch(snapshot, outcome)
    return snapshot


def _resolve_security_name(client: Any, code: str) -> Any:
    """尽力取证券名称；**任何失败都返回 None**（名字是装饰，不该影响出图）。

    用 ``getattr`` 探测而不是写进 ``AShareLocalClient`` 的协议里：测试注入的假
    客户端没有这个方法，于是名字自然缺席 —— 这正是我们要的（测试不该连真库）。
    """
    getter = getattr(client, "fetch_security_name", None)
    if not callable(getter):
        return None
    try:
        return getter(code)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("证券名称查询失败 %s: %s", code, exc)
        return None


# --------------------------------------------------------------- 按需补因子（R17-3）


def _skipped_no_factor(result: Any) -> tuple[str, ...]:
    """取 ``AShareFetchResult.skipped_no_factor``（对 duck-typed 假客户端也安全）。

    不用 ``getattr(result, ..., ())``：``result`` 已被标注成 ``AShareFetchResult``，
    mypy 会拿属性类型 ``tuple[str, ...]`` 去校验默认值 ``tuple[()]`` 并报错。
    """
    skipped = getattr(result, "skipped_no_factor", None)
    return tuple(skipped) if skipped else ()


def factor_ensurer_from_env(*, default: bool = False) -> FactorEnsurer | None:
    """按 ``CPT_ASHARE_ONDEMAND_FACTOR`` 决定是否启用按需补因子。

    Args:
        default: 环境变量**未设置**时的取值。

    ``default=False``（本函数的默认）意味着：**直接调用
    ``build_ashare_snapshot`` 不会联网、不会写库**。只有显式开启才生效 —— 这一点
    是踩出来的：早期版本把默认写成"开"，结果跑一次 ``pytest`` 就让
    ``test_snapshot_reason_no_factor_is_not_db_error`` 真的去腾讯拉了 600519 的
    800 行因子并写进了生产库（因子表 94 → 95 只）。

    生产入口（``cpt.web.a_share_routes``）用 ``default=True`` 显式打开。
    """
    raw = os.getenv(ENV_ONDEMAND_FACTOR)
    if raw is None:
        return _ensure_factors_and_persist if default else None
    if raw.strip().lower() in {"", "0", "false", "no", "off"}:
        return None
    return _ensure_factors_and_persist


def _ensure_factors_and_persist(code: str) -> FactorEnsureResult:
    """拉一次因子并**落库**（幂等），返回结果。

    落库而不是只放内存：这样它同时是一个**自愈缓存** —— 拉过一次以后都快，
    而且每日的 ``scripts/factor_backfill.py --mode incremental`` 会顺带刷新。
    代价是 GET 快照会写 DB，所以整条路径由 ``CPT_ASHARE_ONDEMAND_FACTOR`` 兜底开关。
    """
    fetcher = _fetcher()
    outcome = fetcher.ensure(code)
    if not outcome.fetched:
        return outcome
    try:
        rows = fetch_factor_rows(code, days=DEFAULT_FACTOR_DAYS)
    except Exception as exc:  # noqa: BLE001 — 落库失败不该让看板 500
        _LOG.warning("按需因子落库前重取失败 %s: %s", code, exc)
        return FactorEnsureResult(
            code=code, fetched=False, rows=0, reason="error", detail=f"{type(exc).__name__}: {exc}"
        )
    client = AShareLocalClient()
    try:
        written = upsert_factor_rows(client._get_conn(), rows, source_url=TENCENT_KLINE_URL)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("按需因子落库失败 %s: %s", code, exc)
        return FactorEnsureResult(
            code=code,
            fetched=False,
            rows=0,
            reason="persist_failed",
            detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        client.close()
    _LOG.info("按需因子落库成功 %s: %d 行", code, written)
    return FactorEnsureResult(code=code, fetched=True, rows=written)


_FETCHER: OnDemandFactorFetcher | None = None


def _fetcher() -> OnDemandFactorFetcher:
    """进程内单例：冷却状态必须跨请求保留，否则冷却形同虚设。"""
    global _FETCHER  # noqa: PLW0603
    if _FETCHER is None:
        _FETCHER = OnDemandFactorFetcher()
    return _FETCHER


def reset_factor_fetcher() -> None:
    """清掉冷却状态（测试用）。"""
    global _FETCHER  # noqa: PLW0603
    _FETCHER = None


def _try_on_demand_factors(
    code: str,
    client: AShareLocalClient,
    start_ms: int,
    end_ms: int,
    ensurer: FactorEnsurer | None,
) -> FactorEnsureResult | None:
    """本地因子不全时补一次；返回结果（``None`` = 没启用/不该补）。"""
    if ensurer is None:
        return None
    # 行情本身都没有的代码不必去拉因子（省一次腾讯往返）
    try:
        result = client.fetch_validated_klines(code, start_ms, end_ms)
    except AShareNoDataError:
        return None
    except AShareNoFactorError:
        pass  # 正是要补的情形
    except Exception:  # noqa: BLE001 — DB 类问题不该在这里吞掉，交给外层
        return None
    else:
        if not _skipped_no_factor(result):
            return None
    return ensurer(code)


def _reason_for_failure(outcome: FactorEnsureResult | None) -> str:
    """把按需补因子的失败原因翻成前端能懂且**能据此行动**的 reason。

    ``unsupported`` 与 ``network`` 必须分开：前者是"这只票腾讯就没有"，重试无意义；
    后者是"这次没连上"，重试有意义。混成一个会让用户白点。
    """
    if outcome is None:
        return "no_factor"
    if outcome.reason == "unsupported":
        return "no_factor_unsupported"
    if outcome.reason == "cooldown":
        return "no_factor_cooldown"
    if outcome.reason in {"network", "error", "persist_failed"}:
        return f"no_factor_fetch_failed:{outcome.reason}"
    return "no_factor"


def _attach_factor_fetch(snapshot: dict[str, Any], outcome: FactorEnsureResult | None) -> None:
    """把按需拉取的结果挂到快照上（审计用：这次到底拉没拉、拉了多少）。"""
    if outcome is None:
        return
    quality = snapshot.setdefault("data_quality", {})
    if isinstance(quality, dict):
        quality["factor_fetch"] = {
            "attempted": True,
            "fetched": outcome.fetched,
            "rows": outcome.rows,
            "reason": outcome.reason,
            "detail": outcome.detail,
        }
    runtime = snapshot.setdefault("runtime", {})
    if isinstance(runtime, dict):
        runtime["factor_fetch"] = quality.get("factor_fetch") if isinstance(quality, dict) else None


def empty_ashare_snapshot(
    code: str,
    reason: str,
    *,
    name: str = "",
    board: str | None = None,
) -> dict[str, Any]:
    """DB 真空 / 缺因子时的占位 snapshot（前端可识别为"无数据"）。

    ``reason`` 是给用户看的，必须具体：``no_factor``（缺复权因子）、``no_data``、
    ``db_error:*``、``invalid_bars:*``。

    ``name``/``board`` 由调用方**已经查好**传进来（本函数不碰 DB）：降级时反而更
    需要知道"这是哪只票"，否则用户对着空图只有一个六位数字。
    """
    return {
        "schema_version": "dashboard.v2",
        "market": {
            "symbol": code,
            "kind": "a_share",
            "name": name,
            "board": board,
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
