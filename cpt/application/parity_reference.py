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

    # R45：**委派给 reference 后端**，不再在本模块里自己走那两条路。
    # 此前参照侧是这里的两个私有函数，而它同时又不是一个后端
    # （``BACKEND_CHOICES`` 里没有它），于是「参照侧到底是哪套算法」
    # 只能靠读 application 层的私有代码回答，且存在**两份实现漂移**的风险
    # （parity 走 czsc、离线导出走腾讯）。
    # ⇒ 现在只有一份实现，且**实际用了哪一级由后端如实报告**。
    from cpt.adapters.backend_factory import resolve_backend  # noqa: PLC0415
    from cpt.adapters.reference_backend import (  # noqa: PLC0415
        ReferenceUnavailableError,
    )
    from cpt.adapters.reference_chanlun import ReferenceChanlunConfig  # noqa: PLC0415

    # ⚠️ R45 修：**min_bi_len 不属于 ReferenceChanlunConfig**。
    # 原写法 `ReferenceChanlunConfig(min_bi_len=cfg.min_bi_len)` 直接
    # ``TypeError``（该 dataclass 只有 use_fx_*/zs_wzgx/macd_*/fixed_commit），
    # 而 TypeError **不在** `except ReferenceUnavailableError` 里 ⇒ 抛穿出去。
    # ⇒ 它是**后端级**参数（同 code），走 resolve_backend。
    backend = resolve_backend("reference", min_bi_len=cfg.min_bi_len)
    try:
        # ⚠️ 用 ``compute_domain_structures`` 而不是契约方法 ``compute_structures``：
        # 后者返回 ``ChanlunResult``（存 **bar 索引**），而本层要的是带
        # ``start_time``/``end_time`` 的领域对象 —— 两者量纲不同，
        # 硬转会把时间锚点全丢（``start_bar=0``），面板「点选高亮」就废了。
        ref = backend.compute_domain_structures(list(bars), ReferenceChanlunConfig())
    except ReferenceUnavailableError as exc:
        return build_parity_snapshot(
            available=False,
            reason="reference_unavailable",
            reference="none",
            reference_detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        # ⚠️ R45 补：本函数 docstring 第一句是「**永不抛异常**」，
        # 而原来**只**接 ``ReferenceUnavailableError`` ——
        # 后端抛任何别的异常（czsc 内部炸、腾讯超时、归一化字段缺失…）
        # 都会**直接穿出去**，把整张 A 股快照带崩。
        #
        # 为什么这里该宽泛捕获，而 storage 层却坚持「失败必须抛」：
        #   storage 的返回值**就是业务事实**（因子/信号）⇒ 失败不能伪装成空；
        #   parity 是**可选的交叉验证块** ⇒ 它失败不该影响主流程，
        #   但也**不能静默** —— ���是 ``available=False`` + 写明异常类型与消息。
        # 两者不矛盾，差别在「这个返回值被谁当权威」。
        _LOG.warning("parity 参照侧异常：%s: %s", type(exc).__name__, exc)
        return build_parity_snapshot(
            available=False,
            reason="reference_error",
            reference="none",
            reference_detail=f"{type(exc).__name__}: {exc}",
        )
    source = backend.source or "none"
    if source == "czsc":
        detail = f"同批 K 线、czsc 后端 vs 生产后端 {type(production_backend).__name__}"
    else:
        # 参照侧必须与本地**同窗口**：本地 122 根无缺口，腾讯 800 根含节假日缺口。
        detail = (
            f"本地库结构 vs 腾讯 hfq 同窗口序列"
            f"（{len(bars)} 根，生产后端 {type(production_backend).__name__}）"
        )
    # ⚠️ R46 修：参照侧返回的可能是**领域对象**（czsc 级）也可能是**已归一化 dict**
    # （腾讯级）—— `build_parity_snapshot` 只吃 dict（用 ``.get``），把领域对象直接
    # 传进去会在视图构造处炸 ``'Fractal' object has no attribute 'get'``（线上实测
    # 每只票都刷这条）。此处统一过一遍 ``_normalize``，与 ``cpt_side`` 同口径。
    ref_fractals, ref_bis, ref_zhongshus = ref
    return build_parity_snapshot(
        fractals=cpt_side["fractals"],
        bis=cpt_side["bis"],
        zhongshus=cpt_side["zhongshus"],
        ref_fractals=_normalize(ref_fractals, "fractal"),
        ref_bis=_normalize(ref_bis, "bi"),
        ref_zhongshus=_normalize(ref_zhongshus, "zhongshu"),
        reference=source,
        reference_detail=detail,
    )
