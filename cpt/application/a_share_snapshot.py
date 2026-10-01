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
from collections.abc import Callable, Sequence
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
    check_t_plus_one_calendar,
)
from cpt.adapters.a_share_public import TENCENT_KLINE_URL
from cpt.adapters.backend_factory import DEFAULT_BACKEND, resolve_backend
from cpt.adapters.reference_chanlun import ChanlunBackend
from cpt.adapters.validators import validate_ashare_bars
from cpt.application.dashboard_snapshot_v2 import build_dashboard_snapshot_v2
from cpt.application.first_buy_bridge import derive_first_buy_facts, detect_structural_break
from cpt.application.multi_level import build_multi_level, format_multi_level
from cpt.application.replay import compute_domain_structures
from cpt.application.structure_event_recorder import record_structure_events
from cpt.domain.a_share_rules import apply_ashare_tags_to_bis
from cpt.domain.config import RulesConfig
from cpt.domain.models import Bi, CanonicalBar, Signal, ZhongShu
from cpt.domain.signal import assess_first_buy, transition_first_buy, transition_first_sell
from cpt.storage.signal_event_store import (
    latest_status,
    load_previous_signal,
    record_signal_event,
)

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

#: A 股规则标签的合成 id 前缀（``cpt.domain.a_share_rules`` 生成，审计块按它统计）。
_TAG_PREFIX = "ashare:"

#: 标签的数据来源（写进审计块，便于前端把"标签哪来的"和 K 线来源区分开）。
_TAG_SOURCE = "public.derived_bar"


def _active_client_conn(client: Any) -> Any | None:
    """拿 A 股客户端的连接；拿不到就返回 None（recorder 会自己开/跳过）。

    刻意不抛：A 股客户端是**这个市场的数据源**，事件流是旁路，源出问题不该
    由这里放大。
    """
    getter = getattr(client, "_get_conn", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("结构事件：取 A 股连接失败 %s", exc)
        return None


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
    # 窗口提到 try 外：K 线与 A 股规则标签必须查**同一个区间**，否则笔的末日
    # 落在标签区间外 → 标签静默失效（提出来也让下面 except 分支引用它是安全的）。
    end_ms = int(datetime.now(UTC).timestamp() * 1000)
    # 多预留 60 根以保证缠论结构稳定
    start_ms = end_ms - (width_k + 60) * INTERVAL_MS
    try:
        result = active_client.fetch_validated_klines(code, start_ms, end_ms)
        canonical = list(result.bars)
        if not canonical or _skipped_no_factor(result):
            # 本地因子不全（因子表只覆盖热门池并集，缺是常态）→ 按需补一次再重读。
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
        # 最常见的一种失败（因子表只覆盖热门池并集）。必须与"没数据"和
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
    fractals, raw_bis, zhongshus = compute_domain_structures(
        validated, RulesConfig(), active_backend
    )
    # A 股规则标签（R19 接线）：涨停/跌停/炸板/一字板的**端点可信度低**，
    # 把合成 id 挂到笔的 ``source_ids`` 上，让上层知道该把这笔画虚线。
    # 只注入 id、不改数值 —— 判据见 tests/test_a_share_rules.py::test_tags_do_not_change_structure。
    bis, tags_audit = _apply_daily_tags(active_client, code, start_ms, end_ms, raw_bis)
    signal = _derive_first_buy_signal(
        bis,
        zhongshus,
        validated,
        client=active_client,
        code=code,
    )
    signal_first_sell = _derive_first_sell_signal(
        bis,
        zhongshus,
        validated,
        client=active_client,
        code=code,
    )
    # multi_level 结构递归（R21 接线）：levels=(1,2,3) 表示日线/周线/月线结构递归。
    # 递归失败只降级 multi_level 为 unavailable，不搞挂快照。
    multi_level_data = _compute_multi_level_safe(validated, active_backend)
    # 结构预警（R21 扩展）：笔序列首次跌破前低 → structural_alert
    structural_alert = detect_structural_break(bis)
    # 结构事件流（R26 接线）：diff 出本轮的变化并 append 到
    # `public.cpt_structure_event`，同时把事件喂进 snapshot.events。
    # 此前 `snapshot.events` 在生产里**恒为 []**（没有生产者），这一段是它的
    # 第一个真实出口。best-effort：事件流失败只降级，不影响快照本体。
    structure_events = record_structure_events(
        fractals=fractals,
        bis=bis,
        zhongshus=zhongshus,
        conn=_active_client_conn(active_client),
    )
    snapshot = build_dashboard_snapshot_v2(
        config=RulesConfig(),
        bars=validated,
        fractals=fractals,
        bis=bis,
        zhongshus=zhongshus,
        trend_types=(),
        signal=signal,
        signal_first_sell=signal_first_sell,
        events=structure_events,
        multi_level=multi_level_data,
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
    snapshot["summary"]["structural_alert"] = structural_alert
    _attach_factor_fetch(snapshot, outcome)
    _attach_ashare_tags(snapshot, tags_audit)
    _attach_t_plus_one(snapshot, active_client)
    _attach_close_countdown(snapshot, active_client)
    _attach_signal_change(snapshot, active_client)
    _attach_dual_compare(snapshot, code, active_client)
    return snapshot


def _derive_first_buy_signal(
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    bars: Sequence[CanonicalBar],
    *,
    client: Any = None,
    code: str = "",
) -> Signal | None:
    """由结构对象推导一买信号（``None`` = 当前不评估）。

    **趋势方向从数据推导，不硬编码**：取本级别**最后一笔**的方向作为「当前走势
    方向」。最后一笔向上时一买无意义，桥返回 ``None``，本函数也就**不产信号**——
    这比无脑传 ``-1`` 保守得多：后者会让任何时候都产出一个 ``confirmed`` 或
    ``invalidated``，面板上的信号栏将永远非空，失去「当下是否处于一买结构」的
    信息量。

    ``source_revision`` 固定 ``0``：A 股快照每次都全量重算，没有可回溯的
    revision 序列；状态机 docstring 明确该字段「原样记录、不自增」。

    **信号历史持久化（R21）**：当 ``client`` 传入时，从
    ``public.cpt_signal_event`` 加载同 ``signal_id`` 的最新事件作为
    ``previous``，让状态机跨轮询推进；评估完成后若 status 变化则 append 一条
    事件。``client=None`` 时退化为 ``previous=None``（首次评估），不写事件。
    """
    config = RulesConfig()
    level = config.levels[0] if config.levels else 0
    level_bis = [bi for bi in bis if bi.level == level]
    if not level_bis:
        return None
    facts = derive_first_buy_facts(
        level=level,
        trend_direction=level_bis[-1].direction,
        bis=bis,
        zhongshus=zhongshus,
    )
    if facts is None:
        return None
    last_bar = bars[-1] if bars else None
    structure_id = facts.structure_id or f"level{level}:empty"
    event_time = int(last_bar.close_time) if last_bar is not None else 0

    # 加载上一状态（R21 信号历史持久化）
    previous: Signal | None = None
    getter = getattr(client, "_get_conn", None)
    conn = getter() if callable(getter) else None
    if conn is not None:
        signal_id = f"first_buy:{level}:{structure_id}"
        previous = load_previous_signal(conn, signal_id)

    signal = assess_first_buy(
        level=level,
        structure_id=structure_id,
        center_ids=facts.center_ids,
        trend_direction=level_bis[-1].direction,
        has_two_centers=facts.has_two_centers,
        has_divergence_leg=facts.has_divergence_leg,
        has_reversal_bi=facts.has_reversal_bi,
        divergence_status=facts.divergence_status,
        price=float(last_bar.close) if last_bar is not None else 0.0,
        source_revision=0,
        event_time=event_time,
        previous=previous,
    )

    # 状态推进（R21 扩展）：有反向笔时从 structure_ready 推进到 confirmed
    if signal is not None and facts.has_reversal_bi and signal.status == "structure_ready":
        signal = transition_first_buy(
            signal,
            reversal_closed=True,
            structure_valid=True,
            event_time=event_time,
        )

    # 记录状态跃迁（status 变化时才 append）
    if conn is not None and signal is not None:
        prev_status = previous.status if previous is not None else None
        record_signal_event(conn, signal, prev_status, code, event_time)
        try:
            conn.commit()
        except Exception as exc:
            _LOG.warning("提交信号事件失败 %s: %s", code, exc)

    return signal


def _derive_first_sell_signal(
    bis: Sequence[Bi],
    zhongshus: Sequence[ZhongShu],
    bars: Sequence[CanonicalBar],
    *,
    client: Any = None,
    code: str = "",
) -> Signal | None:
    """由结构对象推导一卖信号（``_derive_first_buy_signal`` 的镜像）。

    趋势方向从数据推导：取本级别**最后一笔**的方向。最后一笔向下时一卖
    无意义，桥返回 ``None``。
    """
    from cpt.application.first_buy_bridge import derive_first_sell_facts
    from cpt.domain.signal import assess_first_sell

    config = RulesConfig()
    level = config.levels[0] if config.levels else 0
    level_bis = [bi for bi in bis if bi.level == level]
    if not level_bis:
        return None
    facts = derive_first_sell_facts(
        level=level,
        trend_direction=level_bis[-1].direction,
        bis=bis,
        zhongshus=zhongshus,
    )
    if facts is None:
        return None
    last_bar = bars[-1] if bars else None
    structure_id = facts.structure_id or f"level{level}:empty"
    event_time = int(last_bar.close_time) if last_bar is not None else 0

    # 加载上一状态（R21 信号历史持久化）
    previous: Signal | None = None
    getter = getattr(client, "_get_conn", None)
    conn = getter() if callable(getter) else None
    if conn is not None:
        signal_id = f"first_sell:{level}:{structure_id}"
        previous = load_previous_signal(conn, signal_id)

    signal = assess_first_sell(
        level=level,
        structure_id=structure_id,
        center_ids=facts.center_ids,
        trend_direction=level_bis[-1].direction,
        has_two_centers=facts.has_two_centers,
        has_divergence_leg=facts.has_divergence_leg,
        has_reversal_bi=facts.has_reversal_bi,
        divergence_status=facts.divergence_status,
        price=float(last_bar.close) if last_bar is not None else 0.0,
        source_revision=0,
        event_time=event_time,
        previous=previous,
    )

    # 状态推进（R21 扩展）：有反向笔时从 structure_ready 推进到 confirmed
    if signal is not None and facts.has_reversal_bi and signal.status == "structure_ready":
        signal = transition_first_sell(
            signal,
            reversal_closed=True,
            structure_valid=True,
            event_time=event_time,
        )

    # 记录状态跃迁
    if conn is not None and signal is not None:
        prev_status = previous.status if previous is not None else None
        record_signal_event(conn, signal, prev_status, code, event_time)
        try:
            conn.commit()
        except Exception as exc:
            _LOG.warning("提交信号事件失败 %s: %s", code, exc)

    return signal


def _apply_daily_tags(
    client: Any,
    code: str,
    start_ms: int,
    end_ms: int,
    bis: Sequence[Bi],
) -> tuple[Sequence[Bi], dict[str, Any]]:
    """把 A 股衍生标签挂到笔上；返回 ``(打了标签的笔, 审计块)``。

    标签是**纯展示增强**，任何一步失败都只降级标签、不影响出图：DB 不可达时
    照样返回原笔 + ``reason="tag_fetch_failed"``，绝不把整个快照搞成 degraded。

    审计块刻意区分三种"没有标签"的原因，否则面板上「没画虚线」分不清是
    **今天真没有涨停**还是**功能没接上**：

    - ``client_unsupported`` —— 注入的客户端没实现 ``fetch_daily_tags``（测试替身）
    - ``tag_fetch_failed`` —— 查了但失败（DB 挂）
    - ``available=True, tagged_bis=0`` —— 查通了，区间内确实没有极端日
    """
    audit: dict[str, Any] = {
        "available": False,
        "reason": "client_unsupported",
        "total_bis": len(bis),
    }
    getter = getattr(client, "fetch_daily_tags", None)
    if not callable(getter):
        return bis, audit
    try:
        tags = getter(code, start_ms, end_ms)
    except Exception as exc:  # noqa: BLE001
        _LOG.info("A 股规则标签查询失败 %s: %s", code, exc)
        return bis, {**audit, "reason": "tag_fetch_failed"}
    if not tags:
        return bis, {**audit, "available": True, "tagged_bis": 0, "source": _TAG_SOURCE}
    tagged = apply_ashare_tags_to_bis(bis, tags)
    n_tagged = sum(1 for b in tagged if any(i.startswith(_TAG_PREFIX) for i in b.source_ids))
    return tagged, {
        "available": True,
        "tagged_bis": n_tagged,
        "total_bis": len(bis),
        "source": _TAG_SOURCE,
    }


def _attach_ashare_tags(snapshot: dict[str, Any], audit: dict[str, Any]) -> None:
    """把审计块塞进 ``data_quality``（与 :func:`_attach_factor_fetch` 同款，零 schema 变更）。"""
    snapshot.setdefault("data_quality", {})["ashare_tags"] = audit


def _attach_close_countdown(snapshot: dict[str, Any], client: Any) -> None:
    """计算距 A 股收盘秒数（15:00），接 ``public.trade_calendar``。

    非交易日 / 收盘后 / 查询失败 → ``available=False``，前端隐藏倒计时。
    """
    import datetime as _dt

    from cpt.adapters.a_share_local import is_trade_day

    today = _dt.date.today()
    try:
        getter = getattr(client, "_get_conn", None)
        conn = getter() if callable(getter) else None
        if conn is None:
            raise RuntimeError("no db conn")
        # R24：SQL 下沉到 adapters.is_trade_day
        is_open = is_trade_day(conn, today.isoformat())
        if not is_open:
            snapshot["close_countdown"] = {
                "available": False,
                "reason": "not_a_trade_day" if is_open is not None else "calendar_unknown",
            }
            return
        now = _dt.datetime.now()
        close_time = now.replace(hour=15, minute=0, second=0, microsecond=0)
        remaining = max(0, int((close_time - now).total_seconds()))
        snapshot["close_countdown"] = {
            "available": True,
            "seconds_to_close": remaining,
            "close_time": "15:00:00",
            "is_open": remaining > 0,
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("收盘倒计时查询失败 %s: %s", snapshot.get("market", {}).get("symbol"), exc)
        snapshot["close_countdown"] = {
            "available": False,
            "reason": "countdown_check_failed",
        }


def _attach_signal_change(snapshot: dict[str, Any], client: Any) -> None:
    """检测 signal status 跨轮询变化 → 写 ``summary.signal_changed`` / ``signal_change_type``。

    前端据此弹「信号到达/变化」提醒。无 signal 或无变化 → ``signal_changed=False``。
    """
    signal = snapshot.get("signal")
    if not isinstance(signal, dict) or not signal.get("status"):
        snapshot["summary"]["signal_changed"] = False
        snapshot["summary"]["signal_change_type"] = None
        return
    try:
        getter = getattr(client, "_get_conn", None)
        conn = getter() if callable(getter) else None
        if conn is None:
            raise RuntimeError("no db conn")
        # R24：SQL 下沉到 storage 层。同时**修掉一个活 bug** ——
        # 原实现在这里内联 `ORDER BY event_time`，而 public.cpt_signal_event
        # **没有 event_time 列**（真实列是 id / transition_time / created_time …），
        # PG 报 `column "event_time" does not exist`，被 except 吞掉且只记 debug。
        # 后果：「信号状态跨轮询变化」这个功能自 R21 起一直是死的，
        # 前端永远拿不到 signal_changed=True。
        # 现在走 storage 的 latest_status（1 列投影，ORDER BY id DESC）。
        prev_status = latest_status(conn, str(signal.get("signal_id", "")))
        cur_status = signal.get("status")
        changed = prev_status is not None and prev_status != cur_status
        snapshot["summary"]["signal_changed"] = changed
        if changed:
            snapshot["summary"]["signal_change_type"] = f"{prev_status}→{cur_status}"
        elif prev_status is None:
            snapshot["summary"]["signal_change_type"] = "new"
        else:
            snapshot["summary"]["signal_change_type"] = None
    except Exception as exc:  # noqa: BLE001
        # 记 warning 而不是 debug：这条失败意味着「信号变化检测」整个失效，
        # 前端会一直显示"没变化"。debug 级在生产 journalctl 里等于不存在。
        _LOG.warning("信号变化检测失败 %s: %s", snapshot.get("market", {}).get("symbol"), exc)
        snapshot["summary"]["signal_changed"] = False
        snapshot["summary"]["signal_change_type"] = None


def _attach_dual_compare(snapshot: dict[str, Any], code: str, client: Any) -> None:
    """双数据集同步对比：CPT 本地 vs 东财实时行情（直连 eastmoney，无 akshare 依赖）。

    比较最新收盘价 / 成交量 / 最高 / 最低。东财不可用时降级为 unavailable。
    """
    import json as _json
    import urllib.request as _url
    from urllib.error import URLError as _URLError

    prefix = "1" if code.startswith(("6", "9")) else "0"
    secid = f"{prefix}.{code[:6]}"
    fields = "f43,f44,f45,f46,f47,f48,f57,f58,f60,f170"
    url = (
        f"https://push2.eastmoney.com/api/qt/stock/get"
        f"?secid={secid}&fields={fields}&_={int(__import__('time').time() * 1000)}"
    )
    try:
        req = _url.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        )
        with _url.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode())
        d = data.get("data") or {}
        if not d or d.get("f43") is None:
            raise ValueError(f"东财返回空数据: {data}")

        # 价格单位：分 → 元（f43/f44/f45/f46/f60）；涨跌幅单位：百分之一（f170）
        def _c(v: Any) -> float | None:
            return float(v) / 100 if v is not None and v != "-" else None

        def _pct(v: Any) -> float | None:
            return float(v) / 100 if v is not None and v != "-" else None

        realtime = {
            "price": _c(d.get("f43")),
            "high": _c(d.get("f44")),
            "low": _c(d.get("f45")),
            "open": _c(d.get("f46")),
            "volume": float(d["f47"]) if d.get("f47") and d["f47"] != "-" else None,
            "turnover": float(d["f48"]) if d.get("f48") and d["f48"] != "-" else None,
            "prev_close": _c(d.get("f60")),
            "change_pct": _pct(d.get("f170")),
        }
        candles = snapshot.get("candles", [])
        last_close = float(candles[-1].get("close") or 0) if candles else 0.0
        snapshot["dual_compare"] = {
            "available": True,
            "cpt_close": last_close,
            "realtime_price": realtime["price"],
            "divergence_pct": (
                round((realtime["price"] - last_close) / last_close * 100, 4)
                if last_close > 0 and realtime["price"] is not None
                else None
            ),
            "realtime": realtime,
        }
    except (_URLError, ValueError, KeyError, TypeError) as exc:
        _LOG.debug("双数据集对比失败 %s: %s", code, exc)
        snapshot["dual_compare"] = {
            "available": False,
            "reason": "realtime_unavailable",
        }
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("双数据集对比未知错误 %s: %s", code, exc)
        snapshot["dual_compare"] = {
            "available": False,
            "reason": "realtime_error",
        }


def _attach_t_plus_one(snapshot: dict[str, Any], client: Any) -> None:
    """查 ``public.trade_calendar`` 判断今日是否可买（T+1 日历约束）。

    只读日历，不涉及持仓/账户（roadmap「明确不做持仓」）。失败只降级、不搞挂快照。
    """
    try:
        calendar = check_t_plus_one_calendar(client)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("T+1 日历查询失败 %s: %s", snapshot.get("market", {}).get("symbol"), exc)
        calendar = {"available": False, "reason": "calendar_check_failed"}
    snapshot["t_plus_one"] = calendar


def _compute_multi_level_safe(
    bars: Sequence[CanonicalBar],
    backend: ChanlunBackend,
) -> dict[str, Any] | None:
    """安全调用 ``build_multi_level``，失败时返回 None（降级为 unavailable）。

    ``levels=(1,2,3)`` 表示结构递归（非分钟语义），奎爷 R21 拍板。
    """
    try:
        multi = build_multi_level(bars, RulesConfig(), backend, levels=(1, 2, 3))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("multi_level 计算失败: %s", exc)
        return None
    return format_multi_level(multi)


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
