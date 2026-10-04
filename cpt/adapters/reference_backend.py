"""``reference`` 后端 —— 参照侧一等公民（R45 新增）。

## 为什么要有这个模块

R45 之前，**参照侧是一个没有名字的东西**：

- 它不是后端（``BACKEND_CHOICES`` 只有 ``auto`` / ``czsc`` / ``native``）；
- 它私藏在 ``cpt/application/parity_reference.py`` 里，以两个私有函数
  ``_czsc_structures`` / ``_tencent_structures`` 的形式存在；
- 仓内唯一的"第三个实现"是
  :class:`~cpt.adapters.reference_chanlun.InMemoryChanlunBackend` ——
  一个**测试占位**，做的是"bar 局部极值 + 相邻异类连线"，不追求真实缠论精度。

⇒ 想回答「参照侧到底是哪套算法、跑的是哪份数据、为什么降级」，
只能去读 application 层的私有函数；想把它**当成后端跑一遍**（比如离线
导出参照结构做人工比对）则根本做不到。

## 本模块提供什么

一个真正的 :class:`~cpt.adapters.reference_chanlun.ChanlunBackend` 实现
:class:`ReferenceChanlunBackend`，它把参照侧的**两级回落**固化成后端契约：

    czsc（实现对照）→ 腾讯 hfq（数据链路对照）→ 都没有就抛

并且**如实报告实际用了哪一级**（:attr:`ReferenceChanlunBackend.source`
与 :attr:`~.detail`）—— 这是 R45 给「参照侧」补上的、
此前完全没有的能力：以前降级了只有 application 层知道。

## 与 ``parity_reference`` 的关系

``parity_reference.build_parity_snapshot_for`` 改为**委派给本后端**，
不再自己实现那两条路。⇒ 参照侧只有**一份**实现，
不会出现「parity 用 czsc、离线导出用腾讯」这种漂移。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

from cpt.domain.config import RulesConfig
from cpt.domain.models import CanonicalBar
from cpt.domain.types import BarLike

from cpt.adapters.reference_chanlun import (
    BiRaw,
    ChanlunResult,
    FxRaw,
    ReferenceChanlunConfig,
    ZsRaw,
)

_LOG = logging.getLogger(__name__)

__all__ = [
    "ReferenceChanlunBackend",
    "ReferenceUnavailableError",
    "REFERENCE_SOURCE_CZSC",
    "REFERENCE_SOURCE_TENCENT",
]

#: 参照侧实际来源的取值（会写进 parity payload 的 ``reference.source``）。
REFERENCE_SOURCE_CZSC: Final[str] = "czsc"
REFERENCE_SOURCE_TENCENT: Final[str] = "tencent_hfq"


class ReferenceUnavailableError(RuntimeError):
    """两条参照路都拿不到（czsc 没装 **且** 腾讯取不到）。

    ⚠️ 与 :class:`~cpt.adapters.backend_factory.UnknownBackendError` 区分：
    那个是「**档位名写错了**」，这个是「**名字对、但环境不支持**」。
    两者混成一个异常，会让「配置错误」和「依赖缺失」在日志里长得一样。
    """


class ReferenceChanlunBackend:
    """参照侧后端：czsc 优先，回落腾讯 hfq。

    与其它后端的关键差别：它**有两个数据源**，且**会把用了哪一级如实说出来**。

    Attributes:
        source: 最近一次成功使用的来源（``"czsc"`` / ``"tencent_hfq"``），
            失败时为 ``None``。
        detail: 给人看的一句话说明（可直接进 payload）。
    """

    def __init__(self, *, code: str | None = None) -> None:
        """
        Args:
            code: 标的代码。**只腾讯那条路需要它** —— :class:`CanonicalBar`
                **没有 ``code`` 字段**（R45 实测：``CanonicalBar.__init__``
                不接受该 kwarg），所以不能从 bars 里挖。
                而 :class:`ChanlunBackend` 契约只传 ``bars``。
                ⇒ 代码只能作为**后端级属性**显式给出。
                留 ``None`` 时若走到腾讯那一级，直接判定不可用（不猜代码）——
                与 ``factor_from_actions``「宁可少一个台阶，也不用猜的值」同原则。
        """
        self.code = code
        self.source: str | None = None
        self.detail: str = "尚未运行"

    # ── 契约实现 ────────────────────────────────────────────────
    def compute_structures(
        self, bars: list[BarLike], config: ReferenceChanlunConfig
    ) -> ChanlunResult:
        """算参照结构。两条路都拿不到就抛 :class:`ReferenceUnavailableError`。

        ⚠️ 这里**不静默回落**到 native —— 参照侧回落到生产侧等于**自己跟自己
        比**，那是对照面板最没意义的一种「通过」。宁可报「没参照」。
        """
        canonical = [b if isinstance(b, CanonicalBar) else _as_canonical(b) for b in bars]
        result = self._try_czsc(canonical, config)
        if result is not None:
            self.source = REFERENCE_SOURCE_CZSC
            self.detail = "czsc 实现对照"
            return result
        result = self._try_tencent(canonical, config)
        if result is not None:
            self.source = REFERENCE_SOURCE_TENCENT
            self.detail = "腾讯 hfq 同窗口数据链路对照"
            return result
        self.source = None
        self.detail = "czsc 未安装且腾讯 hfq 取不到"
        raise ReferenceUnavailableError(self.detail)

    def _log_debug_no_code(self) -> None:
        """没给 code 就走到腾讯那一级 —— **降为 debug**：这是调用方没传参数，
        不是事件。真要用腾讯路请 ``ReferenceChanlunBackend(code="600519")``。"""
        _LOG.debug("参照侧没给标的代码，腾讯回落不可用（czsc 那级不受影响）")

    # ── 第一级：czsc（实现对照）─────────────────────────────────
    def _try_czsc(
        self, bars: list[CanonicalBar], config: ReferenceChanlunConfig
    ) -> ChanlunResult | None:
        """czsc 不可用返回 ``None``（由调用方决定回落）。

        ⚠️ 降为 **debug**：这是**配置状态**（czsc 没装）而不是事件，
        而这段在**每次** A 股快照都会走 —— R38 实测它一个人占了线上日志的
        41%，把真信号淹掉了。常态由巡检统一汇报。
        """
        from cpt.adapters.backend_factory import resolve_backend
        from cpt.adapters.czsc_chanlun import CzscNotInstalledError, CzscVersionError
        from cpt.application.replay import compute_domain_structures

        try:
            backend = resolve_backend(
                "czsc", min_bi_len=_rules_cfg(config).min_bi_len
            )
        except (CzscNotInstalledError, CzscVersionError) as exc:
            _LOG.debug("参照侧 czsc 不可用，回落腾讯：%s", exc)
            return None
        return _from_domain(
            compute_domain_structures(bars, _rules_cfg(config), backend)
        )

    # ── 第二级：腾讯 hfq（数据链路对照）────────────────────────
    def _try_tencent(
        self, bars: list[CanonicalBar], config: ReferenceChanlunConfig
    ) -> ChanlunResult | None:
        """腾讯取不到返回 ``None``。

        腾讯那条路需要**标的代码**，而 :class:`ChanlunBackend` 契约只传
        ``bars``。参照侧只认带 ``code`` 的 bar；拿不到就返回 ``None``
        （而不是去猜一个代码）—— 与 ``factor_from_actions`` 里
        「宁可少一个台阶，也不用猜的值」同一个原则。
        """
        code = self.code
        if not code:
            self._log_debug_no_code()
            return None
        from cpt.application.parity_reference import _tencent_structures  # noqa: PLC0415

        # 腾讯那条路要**同窗口** —— 本地 N 根无缺口，腾讯 800 根含节假日缺口。
        # 窗口不对齐会造出「本地没有的日期」，那正是 R45 之前的对照口径错误。
        window = (bars[0].open_time, bars[-1].open_time) if len(bars) >= 2 else (0, 0)
        triples = _tencent_structures(code, _rules_cfg(config), window)
        if triples is None:
            return None
        fractals, bis, zhongshus = triples
        return _from_normalized(fractals, bis, zhongshus)


# ── 转换辅助：把两条路各自的数据形状统一成 ChanlunResult ──────────────
def _as_canonical(bar: BarLike) -> CanonicalBar:
    if isinstance(bar, CanonicalBar):
        return bar
    from cpt.domain.models import OHLCV  # noqa: PLC0415

    return CanonicalBar(
        open_time=int(getattr(bar, "open_time", 0)),
        close_time=int(getattr(bar, "close_time", 0)),
        open=float(getattr(bar, "open", 0.0)),
        high=float(getattr(bar, "high", 0.0)),
        low=float(getattr(bar, "low", 0.0)),
        close=float(getattr(bar, "close", 0.0)),
        volume=float(getattr(bar, "volume", 0.0) or 0.0),
    )


def _rules_cfg(config: ReferenceChanlunConfig) -> RulesConfig:
    """把后端口径配置折成 :class:`RulesConfig`（只取两者共有的字段）。"""
    from dataclasses import fields  # noqa: PLC0415

    common = {f.name for f in fields(RulesConfig)} & {
        "macd_fast",
        "macd_slow",
        "macd_signal",
        "min_bi_len",
        "zs_wzgx",
    }
    payload = {k: getattr(config, k) for k in common if hasattr(config, k)}
    return RulesConfig(**payload)


def _from_domain(triples: Sequence[Any]) -> ChanlunResult:
    """领域对象三分组 → :class:`ChanlunResult`。"""
    fractals, bis, zhongshus = triples
    return ChanlunResult(
        fx_list=tuple(
            FxRaw(
                bar_index=int(getattr(f, "bar_index", 0)),
                kind=str(getattr(f, "kind", "")),
                high=float(getattr(f, "high", 0.0)),
                low=float(getattr(f, "low", 0.0)),
                level=int(getattr(f, "level", 0)),
            )
            for f in fractals
        ),
        bi_list=tuple(
            BiRaw(
                direction=int(getattr(b, "direction", 0)),
                start_bar=0,
                end_bar=0,
                high=float(getattr(b, "high", 0.0)),
                low=float(getattr(b, "low", 0.0)),
                level=int(getattr(b, "level", 0)),
                power_price=float(getattr(b, "power_price", 0.0) or 0.0),
                power_volume=float(getattr(b, "power_volume", 0.0) or 0.0),
                length=int(getattr(b, "length", 0) or 0),
            )
            for b in bis
        ),
        zs_list=tuple(
            ZsRaw(
                start_bar=0,
                end_bar=0,
                high=float(getattr(z, "high", 0.0)),
                low=float(getattr(z, "low", 0.0)),
                level=int(getattr(z, "level", 0)),
                bi_indices=(),
            )
            for z in zhongshus
        ),
        level_map={},
    )


def _from_normalized(
    fractals: Sequence[dict[str, Any]],
    bis: Sequence[dict[str, Any]],
    zhongshus: Sequence[dict[str, Any]],
) -> ChanlunResult:
    """已归一化的 dict 三分组 → :class:`ChanlunResult`。

    ⚠️ 腾讯那条路给的是**已 ``asdict`` 的 dict**（`dashboard_parity._normalize`
    的输出），带 ``start_time``/``end_time`` 而不是 bar 索引。
    这里**不伪造 bar 索引** —— 契约里 ``start_bar`` 是索引，
    而腾讯路给的是时间戳，**两者量纲不同**。
    统一用 ``bar_index=0`` 并在 `level_map` 里空着，
    是为了让调用方能区分「没有索引」而不是拿到一个假索引。
    """
    return ChanlunResult(
        fx_list=tuple(
            FxRaw(
                bar_index=int(f.get("bar_index", 0) or 0),
                kind=str(f.get("kind", "")),
                high=float(f.get("high", 0.0) or 0.0),
                low=float(f.get("low", 0.0) or 0.0),
                level=int(f.get("level", 0) or 0),
            )
            for f in fractals
        ),
        bi_list=tuple(
            BiRaw(
                direction=int(b.get("direction", 0) or 0),
                start_bar=int(b.get("bar_index", 0) or 0),
                end_bar=int(b.get("bar_index", 0) or 0),
                high=float(b.get("high", 0.0) or 0.0),
                low=float(b.get("low", 0.0) or 0.0),
                level=int(b.get("level", 0) or 0),
                power_price=float(b.get("power_price", 0.0) or 0.0),
                power_volume=float(b.get("power_volume", 0.0) or 0.0),
                length=int(b.get("length", 0) or 0),
            )
            for b in bis
        ),
        zs_list=tuple(
            ZsRaw(
                start_bar=0,
                end_bar=0,
                high=float(z.get("high", 0.0) or 0.0),
                low=float(z.get("low", 0.0) or 0.0),
                level=int(z.get("level", 0) or 0),
                bi_indices=(),
            )
            for z in zhongshus
        ),
        level_map={},
    )
