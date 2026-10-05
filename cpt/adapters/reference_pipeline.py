"""参照侧共享算法管线（R46：P0 分层修复，从 application 下沉）。

## 为什么在这个模块

``cpt/adapters/reference_backend.py``（参照侧后端，属 adapters 层）需要：

1. :func:`compute_domain_structures` —— 把后端跑成领域对象三元组；
2. :func:`tencent_structures` —— 腾讯 hfq 回落路。

这两样原先住在 ``cpt/application/replay.py`` 与
``cpt/application/parity_reference.py``。adapters 反向 import application 违反
``.importlinter`` 的 ``layers`` 契约，``lint-imports`` 实测报
``Layered architecture BROKEN``。参照侧的算法/数据管线本就是**基础设施**
（与 czsc / 腾讯 / 后端选择同层），故下沉到 adapters，依赖方向恢复为 allowed
（application → adapters）。

## 迁移方式

函数体由 AST 从原文件**逐字抽取**（见 ``scripts/`` 一次性脚本），非手抄，
以免引入行为漂移。``cpt.application.replay`` 仍 re-export
:func:`compute_domain_structures`，既有 import 点不受影响。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any, Final

from cpt.adapters.reference_chanlun import (
    ChanlunBackend,
    InMemoryChanlunBackend,
    ReferenceChanlunConfig,
    map_bi,
    map_fractal,
    map_zhongshu,
)
from cpt.domain.config import RulesConfig
from cpt.domain.models import Bi, CanonicalBar, Fractal, ZhongShu

_LOG = logging.getLogger(__name__)

__all__ = [
    "compute_domain_structures",
    "default_backend",
    "normalize_structures",
    "tencent_structures",
    "to_ref_config",
]

#: 兜底取数的天数：与 ``factor_backfill`` 的 800 一致（腾讯 count=800 实测拿 801 根）。
_REF_BARS: Final[int] = 800

#: 两侧归一化后要对照的字段白名单。**不能**直接 ``asdict`` 全量对比：领域对象里
#: 有 ``source_ids``（合成 id）、``power_price`` / ``bi_ids`` 等派生字段，全量比会把
#: 「同一条笔」判成 mismatched。字段名按 ``cpt.domain.models`` 的真实定义。
_COMPARE_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "fractal": ("level", "bar_index", "start_time", "end_time", "high", "low"),
    "bi": ("level", "start_time", "end_time", "direction", "high", "low", "length"),
    "zhongshu": ("level", "start_time", "end_time", "high", "low"),
}


def normalize_structures(items: Sequence[Any], kind: str) -> list[dict[str, Any]]:
    """领域对象 → 只含语义字段的 dict（两侧共用同一份白名单与键序）。"""
    fields = _COMPARE_FIELDS[kind]
    out: list[dict[str, Any]] = []
    for item in items:
        raw = asdict(item) if hasattr(item, "__dataclass_fields__") else dict(item)
        out.append({f: raw.get(f) for f in fields})
    return out


def default_backend() -> InMemoryChanlunBackend:
    return InMemoryChanlunBackend()


def to_ref_config(config: RulesConfig) -> ReferenceChanlunConfig:
    """把 CPT RulesConfig 投影到反腐层配置。"""
    return ReferenceChanlunConfig(
        use_fx_qy_middle=config.fx_qy_middle,
        use_fx_qj_ck=config.fx_qj_ck,
        use_bi_type_new=config.bi_type_new,
        zs_wzgx=config.zs_wzgx,
        macd_fast=config.macd_fast,
        macd_slow=config.macd_slow,
        macd_signal=config.macd_signal,
    )


def compute_domain_structures(
    bars: Sequence[CanonicalBar],
    config: RulesConfig,
    backend: ChanlunBackend | None = None,
) -> tuple[tuple[Fractal, ...], tuple[Bi, ...], tuple[ZhongShu, ...]]:
    """算出一遍后端的**领域对象**三元组（未序列化的 dataclass）。

    与 :func:`run_replay` 的分工：

    - :func:`run_replay` 返回 ``export_dataset`` 的 **schema v1 payload** ——
      结构元素在 ``payload["data"]`` 下且已 ``asdict`` 成普通 dict；
    - 本函数返回 ``(fractals, bis, zhongshus)`` 的 dataclass 元组，正是
      :func:`cpt.application.dashboard.build_dashboard_snapshot` 需要的入参形状。

    实测踩过的坑：把 ``run_replay`` 的返回值直接喂给 ``build_dashboard_snapshot_v2``
    会在 ``asdict(f)`` 处炸 ``TypeError: asdict() should be called on dataclass
    instances``（拿到的是 dict）；而写成 ``payload.get("fractals")`` 更糟——静默
    ``None`` → overlays 全空、K 线上一条笔都不画。要 dataclass 就用本函数。

    Args:
        bars: K 线序列（本函数不校验，调用方负责）。
        config: 规则口径配置。
        backend: 结构计算后端；``None`` 时用 ``InMemoryChanlunBackend``。

    Returns:
        ``(fractals, bis, zhongshus)``，三者均为 ``level=config.levels[0]`` 的元组。
    """
    active_backend = backend or default_backend()
    ref_config = to_ref_config(config)
    primary_level = config.levels[0]
    result = active_backend.compute_structures(list(bars), ref_config)
    fractals = tuple(
        map_fractal(fx, level=primary_level, source_ids=(f"b:{fx.bar_index}",), bars=bars)
        for fx in result.fx_list
    )
    bis = tuple(
        map_bi(
            bi,
            level=primary_level,
            source_ids=(f"fx:{bi.start_bar}", f"fx:{bi.end_bar}"),
            bars=bars,
        )
        for bi in result.bi_list
    )
    zhongshus = tuple(
        map_zhongshu(
            zs,
            level=primary_level,
            source_ids=tuple(f"bi:{i}" for i in zs.bi_indices),
            bars=bars,
        )
        for zs in result.zs_list
    )
    return fractals, bis, zhongshus


def tencent_structures(
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
        normalize_structures(fractals, "fractal"),
        normalize_structures(bis, "bi"),
        normalize_structures(zhongshus, "zhongshu"),
    )
