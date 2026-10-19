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

from cpt.application.persist_gate import (
    RUN_STORE_PERSIST_ENV as RUN_STORE_PERSIST_ENV_AUTHORITY,
)
from cpt.application.persist_gate import prod_table_write_enabled
from cpt.storage.run_metric_store import KIND_RUN, RunMetric, append_metrics

_LOG = logging.getLogger(__name__)

__all__ = [
    "HEALTH_DEGRADED",
    "HEALTH_FAILING",
    "HEALTH_OK",
    "MAX_PLAUSIBLE_BAR_LAG_MS",
    "RUN_STORE_PERSIST_ENV",
    "MetricRecorder",
    "fingerprint_from_snapshot",
    "metric_from_snapshot",
    "run_store_persist_enabled",
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

#: 水位行的落库开关。
#:
#: **2026-10-19 起不再是定义处** —— 唯一权威在 :mod:`cpt.application.persist_gate`。
#: 这里保留同名别名，因为 ``cpt/web/app.py`` / ``cpt/web/__main__.py`` 与本模块
#: 的既有 import 都指向它，改指别处只会制造无谓的 diff。
#:
#: 为什么要独立成模块：同一类「测试写生产表」的事故在不同入口各栽过一次
#: （水位行漏了开关；事件流则是在假 client 的 ``_get_conn()`` 返回 ``None`` 时
#: 自己开了生产连接）。开关集中一处，新增旁路写入时才有唯一的地方要问。
RUN_STORE_PERSIST_ENV: Final[str] = RUN_STORE_PERSIST_ENV_AUTHORITY

#: 兼容别名 —— 语义见 :func:`cpt.application.persist_gate.prod_table_write_enabled`。
run_store_persist_enabled = prod_table_write_enabled


#: 一根 K 线的 ``open_time`` 距「现在」的**最大可信差**。
#:
#: 这不是新鲜度 SLA（那是巡检的事，见上面那段注），而是**自证不实**的下界：
#: 序列声称来自本次运行，却带着一个几年前的时间戳，只可能是张冠李戴。
#:
#: 取值依据（2026-10-19 实测生产 `cpt_run_metric` 全表 48216 行，已排除合成样本）：
#:   - ``crypto``/1h：n=47118，lag max **1.00h**
#:   - ``crypto``/1d：n=191，lag max **11.08h**
#:   - ``cn``/1d：n=796，lag max **368.01h**（= 15.3 × interval —— A 股批次是
#:     日频 + 收盘后入库，大滞后**本来合法**，所以阈值不能取「N 倍 interval」）
#:   - 合成样本（`_synthetic_bar`，`:144` ``1_700_000_000_000 + index*interval_ms``）：
#:     lag **1053.5 ~ 1067.9 天**
#: 30 天落在两者之间的空档里，两侧都有 2 倍以上余量。取小会把 A 股判成故障，
#: 取大则漏掉这次这类样本。
MAX_PLAUSIBLE_BAR_LAG_MS: Final[int] = 30 * 24 * 60 * 60 * 1000


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


def _as_of_ms(runtime: dict[str, Any]) -> int:
    """快照**自述**的生成时刻（毫秒），拿不到回 0。

    两个键都是既有生产者写的，不是为本判据新加的：

    - ``runtime.generated_at`` —— 加密轮询路径（``cpt/web/__main__.py:113/233/410``）
    - ``runtime.as_of_ms`` —— A 股快照路径（``cpt/application/a_share_snapshot.py:368``）

    取不到就回 0（调用方据此**放弃**陈旧判定，而不是当成"很旧"）—— 本函数是纯函数
    不能读时钟，没有自述时间戳时唯一诚实的答案就是"不知道"。
    """
    return _num(runtime.get("generated_at")) or _num(runtime.get("as_of_ms"))


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

    # 自证不实：序列声称来自本轮运行，末端却是一个几年前的 ``open_time``。
    #
    # 这不是新鲜度 SLA（那是巡检的策略），而是「这行数据在**内部就对不上**」——
    # 判据只用快照自己的两个字段（自述生成时刻 vs 末根 open_time），**不读时钟**，
    # 所以本函数仍然纯净、仍然好测。
    #
    # 为什么非要这条（2026-10-19，审计 P2-bl）：`_poll_once` 的
    # ``"stale": False`` 是**硬编码**的，而 ``data_source="binance_realtime"``
    # 也是硬编码 —— 于是测试用一个返回合成 bar（``1_700_000_000_000 + i*interval``）
    # 的假 client 顶替真 client 时，落库的标签是「binance_realtime、不陈旧、
    # health=ok」，而实际序列是 2023 年的时间戳。生产库实测被这样污染 **141 行**，
    # 巡检据此报出 21 条「笔数 218 → 0」的假故障。
    #
    # 阈值 :data:`MAX_PLAUSIBLE_BAR_LAG_MS` 的取值依据见其定义处。
    lag_ms = 0
    implausible_bar_time = False
    if last_bar_time > 0:
        as_of = _as_of_ms(runtime)
        if as_of > 0:
            lag_ms = as_of - last_bar_time
            # 取绝对值：**超前**与**滞后**一样不可能是真的。正常的「未收盘那根」
            # 最多超前一个周期（小时级），而 253 天后才开盘的 K 线没有第二种解释。
            implausible_bar_time = abs(lag_ms) > MAX_PLAUSIBLE_BAR_LAG_MS

    if bar_count <= 0:
        health = HEALTH_FAILING
    elif stale or gap_count:
        health = HEALTH_FAILING
    elif implausible_bar_time:
        # 优先级与 ``stale``/``gap_count`` 同级：都是「数据事实就不对」，
        # 而「降级但可用」是下一档（有数据、只是带着降级标记）。
        health = HEALTH_FAILING
    elif bool(runtime.get("degraded")):
        # 2026-10-06 恢复（owner：之前是误删）。信号一直都在 ——
        # ``cpt.web.__main__._degraded_snapshot`` 会往快照的 ``runtime`` 里写
        # ``degraded=True`` 与 ``degraded_reason``（后端/数据源不可用但仍算出了
        # 可展示的结果），只是本函数一直没读它，于是这一态**从未被记录过**。
        #
        # 后果不是「少一个枚举值」：``scripts/run_inspection.py`` 一直在消费它
        # （``elif row["health"] == "degraded"``），常量也还在，唯独生产者没了 ——
        # 于是「降级但可用」被静默记成 ``ok``，夜盘那种 41% 的降级轮次在告警
        # 视角里全是绿的。
        #
        # 优先级放在 ``failing`` 之后：没有数据 / 陈旧 / 有缺口是**硬故障**，
        # 那时说什么都是白说；有数据但带着降级标记才是「可用但别当正常」。
        health = HEALTH_DEGRADED
    else:
        health = HEALTH_OK

    # R59（审计 M24）：``factor_coverage`` 目前**全仓没有生产者** —— 快照里从来
    # 不带这个键。原来的 ``_float(...) or 1.0`` 把「缺失」写成 1.0，等于给巡检
    # 面板报「覆盖率 100%」；这比不报更坏，因为它把「没实现」伪装成「最好」。
    #
    # 为什么「未实现」不能靠 NULL 区分：列是
    # ``factor_coverage numeric NOT NULL DEFAULT 0``（``run_metric_store._DDL``
    # 与迁移 ``2026-10-06_r56_cpt_run_metric.sql``），且 ``append_metrics`` 对
    # ``None`` 会**整列省略**让 DEFAULT 生效 —— 两种写法落库都是 0。所以标记
    # 只能放进 ``detail`` jsonb：数值列如实记 0.0（不再抬成 1.0），来源由
    # ``factor_coverage_source`` 区分。等哪天接了真实生产者，改的就是这里。
    raw_coverage = quality.get("factor_coverage")
    factor_coverage = _float(raw_coverage)

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
        factor_coverage=factor_coverage,
        snapshot_age_ms=_num(runtime.get("snapshot_age_ms")),
        health=health,
        detail=json.dumps(
            {
                "status": runtime.get("status"),
                "data_source": runtime.get("data_source"),
                "interval": runtime.get("interval"),
                "schema_version": snapshot.get("schema_version"),
                # R59（审计 M24）：把「未实现」与「真实 0%」分开的**唯一**位置。
                "factor_coverage": raw_coverage,
                "factor_coverage_source": (
                    "snapshot" if raw_coverage is not None else "unimplemented"
                ),
                # R62：A 股「K 线为什么没跟上」的诊断跟着水位行一起落库 ——
                # 告警通道（``scripts/run_inspection.py``）读的是**水位表**，
                # 不是快照；不把它带下来，告警只能说「stale=True」，说不了
                # 「缺复权因子、滞后 5 个交易日」，而后者才是能直接去修的那句话。
                "ashare_staleness": _ashare_staleness(quality),
                # 判成 failing 时**说清是哪条**：``stale``/``gap_count`` 本来就有列，
                # 而「末端时间戳不可信」只有这里能表达。没有这个键，巡检面板上
                # 那 141 行就只剩一个 ``health=failing`` 加一堆正常的计数字段，
                # 读告警的人无从判断是数据坏了还是判据坏了。
                "bar_lag_ms": lag_ms if last_bar_time > 0 else None,
                "bar_time_implausible": implausible_bar_time,
            },
            ensure_ascii=False,
        ),
    )


def _ashare_staleness(quality: Any) -> dict[str, Any] | None:
    """从 ``data_quality`` 里摘出 A 股陈旧诊断；不陈旧（或没有）返回 ``None``。

    ``None`` 而不是空 dict：``detail`` 是 jsonb，空对象会在巡检面板上渲染成一个
    无意义的 ``{}``；缺键则什么都不显示。
    """
    if not isinstance(quality, dict) or not quality.get("stale"):
        return None
    keys = (
        "stale_reason",
        "stale_reason_day",
        "stale_last_bar_date",
        "stale_cutoff_date",
        "stale_lag_days",
        # R64：不复权兜底时口径也要跟着走 —— 否则告警说「滞后 N 个交易日」
        # 而面板上明明有今天的价，读告警的人会先怀疑告警坏了。
        "stale_price_basis",
    )
    out = {key: quality[key] for key in keys if quality.get(key) is not None}
    return out or None


def _len_of(value: Any) -> int:
    return len(value) if isinstance(value, (list, tuple, dict)) else 0


@dataclass
class MetricRecorder:
    """持有连接的水位记录器（连接可注入，测试用假连接即可）。

    2026-10-19（审计 P2-bl）：:meth:`record` 是**两条市场路径的唯一咽喉**
    （加密侧 ``cpt/web/__main__._record_run_metric``、A 股侧
    ``cpt/application/a_share_snapshot.build_ashare_snapshot``），所以落库开关
    也守在这里 —— 调用方各加一次闸门迟早会漏掉新加的那条路径。
    """

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
        # 关掉落库时**照常算出这一行**并返回：本方法有返回值，调用方可能拿它
        # 记日志或断言；凭空从 ``None`` 炸掉才是把「测试不写库」变成「测试跑不动」。
        if not run_store_persist_enabled():
            _LOG.debug("水位行不落库 %s/%s：落库开关已关", market, symbol)
            return row
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
