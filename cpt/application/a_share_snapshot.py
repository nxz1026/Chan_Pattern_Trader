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
from dataclasses import asdict
from datetime import UTC, date, datetime
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
from cpt.application.parity_reference import build_parity_snapshot_for
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


def _rollback_quietly(client: Any, context: str) -> None:
    """出错后把连接从 **aborted 态**拉回来，否则同一条连接后续 SQL 全废。

    psycopg 的事务语义：一条语句在事务内报错，整条事务立刻进 aborted 态，
    此后**任何**语句都抛 ``InFailedSqlTransaction``，直到 rollback/rollback-to-
    savepoint 解除。而 ``AShareLocalClient`` 全程复用**同一条**连接（``_get_conn``
    只在首次调用时建连），于是一条 SQL 失败会连锁毒掉后面所有查询 ——
    实测表现为「一只查失败，后面几十只全部降级/skip」。

    这与 R23 漏 ``commit``、R25 漏 ``commit`` 是同一个「事务边界」家族的坑：
    提交/回滚义务在**调用方**，store 层一律不碰。这里就是那个调用方。

    全程 best-effort：回滚本身再失败也只记 debug，绝不让降级路径抛出 ——
    它的存在目的就是**别让异常处理本身制造新异常**。

    :param context: 记进 debug 日志的场景名，便于定位是哪条路径 poisoned 连接。
    """
    conn = _active_client_conn(client)
    rollback = getattr(conn, "rollback", None)
    if not callable(rollback):
        return
    try:
        rollback()
    except Exception as exc:  # noqa: BLE001 — 回滚失败无能为力，但不能因此抛出
        _LOG.debug("回滚失败 %s: %s", context, exc)


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
        # ⚠️⚠️ R45：下面整段包在自己的 try 里。**必要**，因为这里已经在
        # ``except AShareNoFactorError`` 块**内部** —— Python 里 except 块中
        # 抛出的异常**不会被同一 try 的兄弟 handler 接住**（第一版修法就踩了
        # 这个：以为「冒到下面的 except Exception 就行」，结果异常直接逃出
        # ``build_ashare_snapshot``，比原来更糟）。
        try:
            outcome = _try_on_demand_factors(code, active_client, start_ms, end_ms, ensurer)
            if outcome is not None and outcome.fetched:
                # 重读失败几乎必然是 DB 问题（因子已成功落库）⇒ 如实报 db_error，
                # 绝不报 no_factor 把排查方向带偏。
                try:
                    result = active_client.fetch_validated_klines(code, start_ms, end_ms)
                    canonical = list(result.bars)
                except Exception as retry_exc:  # noqa: BLE001
                    _LOG.warning("按需补因子后重读仍失败 %s: %s", code, retry_exc)
                    _rollback_quietly(active_client, f"ashare_reread:{code}")
                    snapshot = empty_ashare_snapshot(
                        code,
                        f"db_error:{type(retry_exc).__name__}",
                        name=security_name,
                        board=security_board,
                    )
                    _attach_factor_fetch(snapshot, outcome)
                    return snapshot
            else:
                snapshot = empty_ashare_snapshot(
                    code, _reason_for_failure(outcome), name=security_name, board=security_board
                )
                _attach_factor_fetch(snapshot, outcome)
                return snapshot
        except Exception as inner_exc:  # noqa: BLE001
            # 按需补因子这一步自己炸了（多为 DB）—— 如实报，别装成缺因子。
            _LOG.warning("按需补因子失败 %s: %s", code, inner_exc)
            _rollback_quietly(active_client, f"ashare_ondemand:{code}")
            return empty_ashare_snapshot(
                code,
                f"db_error:{type(inner_exc).__name__}",
                name=security_name,
                board=security_board,
            )
    except AShareNoDataError as exc:
        _LOG.info("A 股无行情 %s: %s", code, exc)
        return empty_ashare_snapshot(code, "no_data", name=security_name, board=security_board)
    except Exception as exc:  # noqa: BLE001
        # DB 不可达 → 返回 degraded snapshot，**不静默成 OK**
        _LOG.warning("A 股 DB 拉取失败 %s: %s", code, exc)
        # ⚠️ R45 补 rollback：走到这里说明**有一条 SQL 已经在事务里失败了**，
        # 连接停在 aborted 态。``owns_client=False`` 时这是**共享连接**，
        # 不救回来就会连锁毒掉后面所有查询（见 :func:`_rollback_quietly`
        # 里记的实测：「一只查失败，后面几十只全部降级/skip」）。
        if not owns_client:
            _rollback_quietly(active_client, f"ashare_snapshot:{code}")
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
    snapshot = build_dashboard_snapshot_v2(
        config=RulesConfig(),
        bars=validated,
        fractals=fractals,
        bis=bis,
        zhongshus=zhongshus,
        trend_types=(),
        signal=signal,
        signal_first_sell=signal_first_sell,
        events=(),  # R39：事件在建快照**之后**才算（为了拿指纹归因），下面回填
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
    # 结构事件流（R26 接线）：diff 出本轮的变化并 append 到
    # `public.cpt_structure_event`，同时把事件喂进 snapshot.events。
    # 此前 `snapshot.events` 在生产里**恒为 []**（没有生产者），这一段是它的
    # 第一个真实出口。best-effort：事件流失败只降级，不影响快照本体。
    #
    # ⚠️ R39：这里**在建快照之后**才调用，而 R26 时是在之前。原因是「变化原因」
    # 归因（`payload["cause"]`）要比对本轮的算法指纹，而指纹由
    # `build_dashboard_snapshot_v2` 写进 `snapshot["reproducibility"]` —— 快照
    # 还不存在时拿不到它。
    #
    # 为什么回填是安全的：`build_dashboard_snapshot_v2` 对 `events` 只做
    # `[asdict(e) for e in events]`（`cpt/application/dashboard.py:220`），
    # 它是**纯输出**，不参与 `reproducibility`/`data_quality`/任何计算 ——
    # 所以先传空、后回填，最终快照里的 events 与原来完全一致。
    structure_events = record_structure_events(
        market="cn",
        fractals=fractals,
        bis=bis,
        zhongshus=zhongshus,
        # 与上面的 build_dashboard_snapshot_v2 对齐：A 股刻意不算走势类型
        # （见上方注释），事件流与快照必须记同一批结构，否则两边会漂。
        trend_types=(),
        conn=_active_client_conn(active_client),
        symbol=code,
        fingerprint=_fingerprint_for(snapshot, backend=active_backend),
    )
    if structure_events:
        snapshot["events"] = [asdict(event) for event in structure_events]
    # R35：parity 的**参照侧**（czsc 优先，回落腾讯）。此前这个块从上线起就恒为
    # ``{"available": false, "reason": "oracle_reference_unavailable"}`` —— 6 个
    # build_dashboard_snapshot_v2 调用点没有一个传 parity=，即**压根没有生产者**。
    # 现在有了：同批 K 线下，生产侧结构 vs 参照侧结构的逐项对照。
    # 失败只降级 parity 一块（best-effort），绝不影响快照本体。
    try:
        snapshot["parity"] = build_parity_snapshot_for(
            code=code,
            bars=validated,
            fractals=fractals,
            bis=raw_bis,
            zhongshus=zhongshus,
            production_backend=active_backend,
            config=RulesConfig(),
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.info("parity 对照失败 %s: %s", code, exc)
        snapshot["parity"] = {
            "available": False,
            "reason": "parity_error",
            "reference": {"source": "none", "detail": f"{type(exc).__name__}: {exc}"},
        }
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
    _attach_factor_epoch(snapshot, active_client)   # R45：口径纪元随快照下发
    _attach_dual_compare(snapshot, code, active_client)
    _attach_calendar_gaps(snapshot, active_client)
    # R38：A 股侧也落「运行水位 + 算法指纹」。加密侧那行在"只有走到这里才算成功
    # 发布的快照"之后；这里同理 —— 上面几条 except 分支返回的是**降级/空**快照
    # （``empty_ashare_snapshot``），记进去就等于把降级记成正常水位。
    # 复用 ``active_client`` 的连接、**不 commit**：事务边界归调用方（路由）。
    conn = _active_client_conn(active_client)
    if conn is None:
        # 拿不到连接就**不记** —— 记不进去的水位比没有水位更坏：
        # 巡检会以为「A 股侧在跑」，而其实一行都没落。
        _LOG.warning("A 股运行水位跳过：%s 拿不到数据库连接", code)
    else:
        try:
            from cpt.application.run_metric import MetricRecorder  # noqa: PLC0415

            MetricRecorder(conn).record(
                snapshot,
                market="cn",
                symbol=code,
                backend=type(active_backend).__name__,
                fractal_count=len(fractals),
                bi_count=len(raw_bis),
                zhongshu_count=len(zhongshus),
                # A 股刻意不算走势类型（见上面 build_dashboard_snapshot_v2 的注释）
                trend_type_count=0,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("A 股运行水位记录失败 %s：%s: %s", code, type(exc).__name__, exc)
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
    #
    # 读失败**不能**冒出去：上一状态缺失只意味着「本轮当成首次评估」，
    # 信号照常产出；而冒出去会让整个快照 500，并且把连接留在 aborted 态
    # 连累后面所有查询（见 _rollback_quietly）。
    previous: Signal | None = None
    getter = getattr(client, "_get_conn", None)
    conn = getter() if callable(getter) else None
    if conn is not None:
        signal_id = f"first_buy:{level}:{structure_id}"
        try:
            previous = load_previous_signal(conn, signal_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("加载一买历史失败 %s: %s", code, exc)
            _rollback_quietly(client, f"load_previous:first_buy:{code}")

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
        try:
            record_signal_event(conn, signal, prev_status, code, event_time)
            conn.commit()  # ← store 层不 commit，边界在这里
        except Exception as exc:
            _LOG.warning("提交信号事件失败 %s: %s", code, exc)
            _rollback_quietly(client, f"first_buy:{code}")

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
        try:
            previous = load_previous_signal(conn, signal_id)
        except Exception as exc:  # noqa: BLE001 — 同 first_buy：读失败只丢历史，不冒泡
            _LOG.warning("加载一卖历史失败 %s: %s", code, exc)
            _rollback_quietly(client, f"load_previous:first_sell:{code}")

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
        try:
            record_signal_event(conn, signal, prev_status, code, event_time)
            conn.commit()  # ← store 层不 commit，边界在这里
        except Exception as exc:
            _LOG.warning("提交信号事件失败 %s: %s", code, exc)
            _rollback_quietly(client, f"first_sell:{code}")

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
        _rollback_quietly(client, f"daily_tags:{code}")
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


def _fingerprint_for(snapshot: dict[str, Any], *, backend: Any) -> dict[str, str]:
    """本轮的算法指纹四件套（快照已存在时才有意义）。

    薄薄一层包装：真正的口径在 :func:`cpt.application.run_metric.fingerprint_from_snapshot`，
    加密侧与 A 股侧共用同一份，避免两边算出不同的 ``dataset_hash`` 却互相比对。
    """
    from cpt.application.run_metric import fingerprint_from_snapshot  # noqa: PLC0415

    return fingerprint_from_snapshot(snapshot, backend=type(backend).__name__)


def _attach_calendar_gaps(snapshot: dict[str, Any], client: Any) -> None:
    """按 **A 股交易日历** 重算 ``data_quality.gap_count``。

    ## 为什么必须覆盖

    ``dashboard_quality.quality_report`` 的缺口判据是「下一根的间隔 == 上一根自身
    的周期」。那是**加密口径**：7×24 连续交易，任何跳空都是真丢数据。
    但日线 A 股跨周末/法定休市（国庆、春节、清明…一年约 25 天）天然就不满足，
    于是每轮都被记成 ``gap_count=25``。

    实测后果（R39 真机跑出来）：cn 侧落的 3 行水位 **全部** ``health=failing``，
    而 crypto 侧 73 行全部 ``ok``。照这样，``run_inspection`` 每天都会为 A 股发一条
    假告警 —— 那等于没有告警。

    ## 正确口径

    仓里已经有对的实现：``cpt.adapters.source_registry`` 用
    ``open_days_between`` 列出区间内**全部开市日**，再与实际 bar 日期求差集。
    这里复用同一套，不另造轮子。

    只在**已覆盖区间内**比对（``first_bar_date..last_bar_date``），所以「今天的
    bar 还没出来」不会被误判成缺口。

    取不到日历时**不动**原值 —— 宁可保留一个可疑数字，也不把「查不到」写成「没有」。
    """
    from cpt.adapters.a_share_local import open_days_between

    quality = snapshot.get("data_quality")
    if not isinstance(quality, dict):
        return
    candles = snapshot.get("candles") or []
    if len(candles) < 2:
        return

    def _bar_date(c: Any) -> str:
        return datetime.fromtimestamp(c["open_time"] / 1000, tz=UTC).date().isoformat()

    try:
        getter = getattr(client, "_get_conn", None)
        conn = getter() if callable(getter) else None
        if conn is None:
            return
        dates = sorted(
            {_bar_date(c) for c in candles if isinstance(c, dict) and c.get("open_time")}
        )
        if len(dates) < 2:
            return
        expected = open_days_between(
            conn, date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
        )
        if not expected:
            _LOG.debug("交易日历为空，保留原 gap_count（%s）", dates[-1])
            return
        missing = sorted(d for d in expected if d.isoformat() not in set(dates))
        quality["gap_count"] = len(missing)
        quality["gaps"] = [{"date": d} for d in missing]
        quality["gap_basis"] = "trade_calendar"
        quality["expected_trade_days"] = len(expected)
        quality["severity"] = (
            "stale"
            if quality.get("stale")
            else "gap"
            if missing or quality.get("out_of_order_count")
            else "ok"
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("按交易日历重算缺口失败：%s: %s", type(exc).__name__, exc)
        _rollback_quietly(client, "calendar_gaps")


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
        _rollback_quietly(client, "close_countdown")
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
        _rollback_quietly(client, "signal_change")
        snapshot["summary"]["signal_changed"] = False
        snapshot["summary"]["signal_change_type"] = None


def _attach_factor_epoch(snapshot: dict[str, Any], client: Any) -> None:
    """把因子口径纪元挂到 ``summary.factor_epoch``（R45）。

    ## 为什么前端需要这个

    2026-10-03 切表后，**切表前**记录的信号里有 29 条从 ``structure_ready``
    变成 ``invalidated``。那不是「信号失败了」，而是「结构在换口径后重算，
    与旧口径判断不一致」。

    **两件事在没有标记时长得一模一样** —— 用户/排查者看到「信号突然失效」，
    合理推断是系统坏了。没有这个字段就无法证伪。

    契约：``known=False`` 表示「本仓尚未登记切换点」，此时前端**不要**显示
    任何口径提示，而不是显示「旧口径」（那会凭空把一切说成旧口径）。
    """
    getter = getattr(client, "_get_conn", None)
    conn = getter() if callable(getter) else None
    payload: dict[str, Any] = {"known": False}
    if conn is not None:
        from cpt.storage.factor_epoch_store import current_epoch  # noqa: PLC0415

        ep = current_epoch(conn)
        payload = {
            "known": ep.known,
            "switched_at": ep.switched_at.isoformat() if ep.switched_at else None,
            "old_source": ep.old_source,
            "new_source": ep.new_source,
            "old_match_rate": (
                float(ep.old_match_rate) if ep.old_match_rate is not None else None
            ),
            "new_match_rate": (
                float(ep.new_match_rate) if ep.new_match_rate is not None else None
            ),
        }
    snapshot["summary"]["factor_epoch"] = payload


def _attach_dual_compare(snapshot: dict[str, Any], code: str, client: Any) -> None:
    """双数据集同步对比：CPT 本地（**后复权**）vs 上游实时行情。

    ## R35：换源 + 修口径（两件事，都因为一个实测结论）

    **换源**：原实现直连 ``push2.eastmoney.com``。2026-10-02 在生产机（大阪）实测
    **HTTP 502** —— 仓里 ``source_registry.KNOWN_DEAD_ENDPOINTS`` 早就记着
    「本机（大阪）实测 502」。于是这个面板从上线起就一直是
    ``{"available": false, "reason": "realtime_unavailable"}``。改用**新浪快照**
    （``hq.sinajs.cn``，同机实测 200，且 ``adapters.SinaQuoteClient`` 早就存在）。

    **修口径**：原实现拿上游的**不复权**现价直接比 CPT 的**后复权**收盘价。
    这个错一直被 502 挡着没人看见 —— 一旦换源就会显形：600519 / 2026-09-30 实测，
    库里不复权 1258.62、因子 7.06053932、快照收盘 8886.536，直接相比是 **−85.84%**
    （读起来像「市场跌了 86%」）。所以先把现价乘上**同一根 bar 的因子**再比，
    实测同口径下是 **+0.0000%**。

    换源顺带解决了一个分层问题：IO 从 application 层挪回了 adapters
    （原来的 ``urllib`` 调用写在 application 里，SQL 分层门禁管不到它）。

    取的是**最后一根 bar 当日**的因子：盘中因子不变，跨日才会变，而面板比的就是
    「今日 vs 上一根收盘」。
    """
    from cpt.adapters.a_share_local import hfq_factor_on
    from cpt.adapters.a_share_public import ASharePublicError, SinaQuoteClient

    candles = snapshot.get("candles", [])
    last_close = float(candles[-1].get("close") or 0.0) if candles else 0.0
    last_date = None
    if candles:
        raw_open = candles[-1].get("open_time")
        if isinstance(raw_open, (int, float)):
            last_date = datetime.fromtimestamp(raw_open / 1000, tz=UTC).date()

    try:
        quote = SinaQuoteClient().fetch_quote(code)
    except ASharePublicError as exc:
        _LOG.debug("双数据集对比失败 %s: %s", code, exc)
        snapshot["dual_compare"] = {"available": False, "reason": "realtime_unavailable"}
        return
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("双数据集对比未知错误 %s: %s", code, exc)
        snapshot["dual_compare"] = {"available": False, "reason": "realtime_error"}
        return

    raw_price = quote.get("last")
    factor: float | None = None
    if last_date is not None and client is not None:
        try:
            factor = hfq_factor_on(_active_client_conn(client), code, last_date)
        except Exception as exc:  # noqa: BLE001 — 查不到因子不该让整个面板消失
            _LOG.debug("取后复权因子失败 %s %s: %s", code, last_date, exc)
    if raw_price is None:
        snapshot["dual_compare"] = {"available": False, "reason": "realtime_empty"}
        return
    if not factor:
        # 没有因子就没法同口径比。**宁可说不知道**，也不给一个 −85% 的假数字。
        snapshot["dual_compare"] = {
            "available": False,
            "reason": "factor_unavailable",
            "realtime_raw_price": raw_price,
        }
        return

    price_hfq = float(raw_price) * factor
    snapshot["dual_compare"] = {
        "available": True,
        "cpt_close": last_close,
        # 与 cpt_close **同口径**（后复权）的上游现价
        "realtime_price": price_hfq,
        "divergence_pct": (
            round((price_hfq - last_close) / last_close * 100, 4) if last_close > 0 else None
        ),
        "realtime": {
            "price": price_hfq,
            "raw_price": raw_price,
            "hfq_factor": factor,
            "open": _hfq(quote.get("open"), factor),
            "high": _hfq(quote.get("high"), factor),
            "low": _hfq(quote.get("low"), factor),
            "prev_close": _hfq(quote.get("prev_close"), factor),
            "volume": quote.get("volume"),
            "change_pct": _pct_from(quote.get("last"), quote.get("prev_close")),
            "source": "sina",
            "quote_date": quote.get("date"),
            "quote_time": quote.get("time"),
        },
    }


def _hfq(value: Any, factor: float) -> float | None:
    """不复权价 → 后复权价（``None`` 透传，不伪造 0）。"""
    return None if value is None else float(value) * factor


def _pct_from(last: Any, prev_close: Any) -> float | None:
    """涨跌幅（%）：由现价与昨收算，而不是依赖上游给的那一个字段。"""
    if last is None or prev_close is None:
        return None
    prev = float(prev_close)
    if prev <= 0:
        return None
    return round((float(last) - prev) / prev * 100, 4)


def _attach_t_plus_one(snapshot: dict[str, Any], client: Any) -> None:
    """查 ``public.trade_calendar`` 判断今日是否可买（T+1 日历约束）。

    只读日历，不涉及持仓/账户（roadmap「明确不做持仓」）。失败只降级、不搞挂快照。
    """
    try:
        calendar = check_t_plus_one_calendar(client)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("T+1 日历查询失败 %s: %s", snapshot.get("market", {}).get("symbol"), exc)
        _rollback_quietly(client, "t_plus_one")
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
        _rollback_quietly(client, f"security_name:{code}")
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
    # ⚠️ R45：这里**原来**是 ``except Exception: return None``，注释还写着
    # 「DB 类问题不该在这里吞掉，交给外层」—— 而 ``return None`` 恰恰就是吞掉。
    # 后果有三条：
    #   1. 不记日志，DB 抖动完全不可见；
    #   2. 返回 ``None`` → 调用方 ``_reason_for_failure(None)`` 报成 **no_factor**，
    #      而本文件第 202 行的注释白纸黑字写着「必须与『没数据』和『DB 挂了』
    #      分开报 —— 报成 db_error 会把排查方向带偏（实测踩过）」。自己违反自己；
    #   3. **不 rollback** → 连接留在 aborted 态。``owns_client=False`` 时这是
    #      **共享连接**，一条 SQL 失败会连锁毒掉后面所有查询
    #      （见 :func:`_rollback_quietly` 的实测描述）。
    #
    # 修法：**不吞**，让异常冒到外层 ``except Exception`` —— 那里做的正是
    # 对的事情（记 warning + 报 ``db_error:<类型>``）。
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
