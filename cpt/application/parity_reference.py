"""R35：parity 的**参照侧生产者**（czsc 优先，回落腾讯）。

单独一个模块而不是塞进 ``a_share_snapshot``，是因为它有**两条**独立失败路径
（czsc 缺依赖 / 腾讯取不到），每条都要能单独降级，且降级原因要如实写进 payload。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any, Final

from cpt.domain.config import RulesConfig
from cpt.domain.models import CanonicalBar

_LOG = logging.getLogger(__name__)

#: 兜底取数的天数：与 ``factor_backfill`` 的 800 一致（腾讯 count=800 实测拿 801 根），
#: 但对照只用**本地库窗口**那一段（见 :func:`build_parity_snapshot_for`），所以多取无害。
_REF_BARS: Final[int] = 800

#: 两侧归一化后要对照的字段白名单。**不能**直接 ``asdict`` 全量对比：领域对象里
#: 有 ``source_ids``（合成 id，含 level/index，与结构语义无关）、``power_price`` /
#: ``bi_ids`` 等派生字段，全量比会把"同一条笔"判成 mismatched。挑出结构**语义**字段。
#: 字段名按 ``cpt.domain.models`` 的真实定义（Fractal 有 ``bar_index`` 无
#: ``direction``；Bi 无 ``kind``；三者都有 ``level``）。
_COMPARE_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "fractal": ("level", "bar_index", "start_time", "end_time", "high", "low"),
    "bi": ("level", "start_time", "end_time", "direction", "high", "low", "length"),
    "zhongshu": ("level", "start_time", "end_time", "high", "low"),
}


def _normalize(items: Sequence[Any], kind: str) -> list[dict[str, Any]]:
    """领域对象 → 只含语义字段的 dict。

    两侧都用同一份白名单与同一份键序 —— 否则「字段顺序不同」会被
    ``difference_fields`` 判成不一致，而那不是结构差异。
    """
    fields = _COMPARE_FIELDS[kind]
    out: list[dict[str, Any]] = []
    for item in items:
        raw = asdict(item) if hasattr(item, "__dataclass_fields__") else dict(item)
        out.append({f: raw.get(f) for f in fields})
    return out


def _czsc_structures(
    bars: Sequence[CanonicalBar], config: RulesConfig
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] | None:
    """参照侧 = czsc 后端；不可用返回 ``None``（由调用方决定回落）。

    直接返回**归一化后的 dict**（而不是领域对象），这样两条参照路
    （czsc 给 dataclass、腾讯给已 ``asdict`` 的 dict）类型一致。
    """
    from cpt.adapters.backend_factory import resolve_backend
    from cpt.adapters.czsc_chanlun import CzscNotInstalledError, CzscVersionError
    from cpt.application.replay import compute_domain_structures

    try:
        backend = resolve_backend("czsc", min_bi_len=config.min_bi_len)
    except (CzscNotInstalledError, CzscVersionError) as exc:
        # R38：**降为 debug**。这是**配置状态**（czsc 没装），不是事件 ——
        # 而这段在**每次** A 股快照都会走一遍，于是它一个人就占了线上日志的
        # 41%（24h 内 22/53 条），把真信号淹掉了。
        # 常态由 `scripts/run_inspection.py` 统一汇报（ccxt / czsc 都归 degraded），
        # 日志流只留"不该发生"的东西。
        _LOG.debug("parity 参照侧 czsc 不可用，回落公开源：%s", exc)
        return None
    fractals, bis, zhongshus = compute_domain_structures(bars, config, backend)
    return (
        _normalize(fractals, "fractal"),
        _normalize(bis, "bi"),
        _normalize(zhongshus, "zhongshu"),
    )


def _tencent_structures(
    code: str, config: RulesConfig, window: tuple[int, int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]] | None:
    """回落参照侧 = 腾讯 hfq 序列 + **生产后端**算出的结构。

    这条路比 czsc 贵（一次 HTTP），但它对照的是**数据链路**（本地库 vs 公开源），
    与 czsc 那种**实现对照**是两种互补的检查，所以仍有独立价值。

    ## ⚠️ 必须用 **A 股专用校验**，不能用 ``replay_bars``（R35 真机两次失败）

    第一版直接 ``replay_bars(800 根)``，被数据守卫生效：

        bars[1] open_time=1686873600000 -> bars[2] open_time=1687132800000 存在缺口

    切到本地 122 根窗口后**仍然**失败，而且缺口就在窗口内：

        bars[3] open_time=1775779200000 -> bars[4] open_time=1776038400000（清明 3 天）

    根因：``replay_bars`` 走的是**通用** ``validate_canonical_bars``，它按固定
    86,400,000ms 判缺口 —— 那是 BTC 24/7 的假设。而 ``validate_ashare_bars`` 的
    第 4 条就是「**忽略缺口**」（A 股节假日缺口合法）。本地路径用的正是后者，
    所以本地能过、参照侧过不了。**换成同一个校验器**才谈得上对照。

    口径方面两者是**可比的**：本地是 ``raw × hfq_factor``，腾讯是 hfq，R31 实测两者
    在 600519 上吻合到小数点后 4 位（8886.536），同一基准。

    :param window: 本地快照的 ``(first_open_time, last_open_time)``。
    :returns: 三个已 ``asdict`` 的序列，失败返回 ``None``。
    """
    from cpt.adapters.a_share_public import ASharePublicError, TencentKlineClient
    from cpt.adapters.backend_factory import DEFAULT_BACKEND, resolve_backend
    from cpt.adapters.validators import validate_ashare_bars
    from cpt.application.replay import compute_domain_structures

    try:
        fetched = TencentKlineClient().fetch_daily_bars(code, limit=_REF_BARS)
    except ASharePublicError as exc:
        _LOG.info("parity 参照侧腾讯取数失败：%s", exc)
        return None
    start, end = window
    aligned = [b for b in fetched if start <= b.open_time <= end]
    if len(aligned) < 2:
        _LOG.info("parity 参照侧腾讯序列与本地窗口无重叠（%s）", code)
        return None
    try:
        validated = validate_ashare_bars(aligned)
        # 用生产后端算公开源的结构：两侧只差"数据从哪来"这一个变量。
        fractals, bis, zhongshus = compute_domain_structures(
            list(validated), config, resolve_backend(DEFAULT_BACKEND)
        )
    except Exception as exc:  # noqa: BLE001 — 参照侧失败不该拖垮快照
        _LOG.info("parity 参照侧计算失败 %s（窗口 %d 根）: %s", code, len(aligned), exc)
        return None
    return (
        _normalize(fractals, "fractal"),
        _normalize(bis, "bi"),
        _normalize(zhongshus, "zhongshu"),
    )


def build_parity_snapshot_for(
    *,
    code: str,
    bars: Sequence[CanonicalBar],
    fractals: Sequence[Any],
    bis: Sequence[Any],
    zhongshus: Sequence[Any],
    production_backend: Any,
    config: RulesConfig | None = None,
) -> dict[str, Any]:
    """算出 A 股快照的 ``parity`` 块（**永不抛异常**）。

    路径：czsc（实现对照）→ 腾讯 hfq（数据链路对照）→ 都没有就 ``available:false``
    并写明原因。两条参照路都拿不到时**不给空壳** —— 宁可说"没参照"，也不要一个
    看上去齐全、实际两边都是空数组的对照块。
    """
    from cpt.application.dashboard_parity import build_parity_snapshot

    cfg = config if config is not None else RulesConfig()
    cpt_side = {
        "fractals": _normalize(fractals, "fractal"),
        "bis": _normalize(bis, "bi"),
        "zhongshus": _normalize(zhongshus, "zhongshu"),
    }

    ref = _czsc_structures(bars, cfg)
    source, detail = "czsc", f"同批 K 线、czsc 后端 vs 生产后端 {type(production_backend).__name__}"
    if ref is None:
        # 参照侧必须与本地**同窗口**：本地 122 根无缺口，腾讯 800 根含节假日缺口。
        window = (bars[0].open_time, bars[-1].open_time) if len(bars) >= 2 else (0, 0)
        ref = _tencent_structures(code, cfg, window)
        source = "tencent_hfq"
        detail = (
            f"本地库结构 vs 腾讯 hfq 同窗口序列"
            f"（{window[1] and len(bars)} 根，生产后端 {type(production_backend).__name__}）"
        )
    if ref is None:
        return build_parity_snapshot(
            available=False,
            reason="reference_unavailable",
            reference="none",
            reference_detail="czsc 未安装，且腾讯 hfq 取数失败",
        )
    return build_parity_snapshot(
        fractals=cpt_side["fractals"],
        bis=cpt_side["bis"],
        zhongshus=cpt_side["zhongshus"],
        ref_fractals=ref[0],
        ref_bis=ref[1],
        ref_zhongshus=ref[2],
        reference=source,
        reference_detail=detail,
    )
