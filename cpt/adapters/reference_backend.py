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
from collections.abc import Mapping, Sequence
from typing import Any, Final

from cpt.adapters.reference_chanlun import (
    BiRaw,
    ChanlunResult,
    FxRaw,
    ReferenceChanlunConfig,
    ZsRaw,
)
from cpt.domain.config import RulesConfig
from cpt.domain.models import CanonicalBar
from cpt.domain.types import BarLike

_LOG = logging.getLogger(__name__)

__all__ = [
    "ReferenceChanlunBackend",
    "IncompleteReferenceError",
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


class IncompleteReferenceError(RuntimeError):
    """参照侧走到了**只有 parity 语义**的那条路，不满足后端契约。

    与 :class:`ReferenceUnavailableError` 的区别很重要：

    - ``ReferenceUnavailableError`` = **环境不支持**（czsc 没装、腾讯取不到）⇒
      可以回落到另一条路；
    - ``IncompleteReferenceError`` = **这条路能出数据，但形状不满足契约**
      ⇒ 回落也救不了，只能换接口。

    混成一个异常，会让调用方以为「再等等就好」或「再试一次就好」。
    """


class ReferenceChanlunBackend:
    """参照侧后端：czsc 优先，回落腾讯 hfq。

    与其它后端的关键差别：它**有两个数据源**，且**会把用了哪一级如实说出来**。

    Attributes:
        source: 最近一次成功使用的来源（``"czsc"`` / ``"tencent_hfq"``），
            失败时为 ``None``。
        detail: 给人看的一句话说明（可直接进 payload）。
    """

    def __init__(self, *, code: str | None = None, min_bi_len: int | None = None) -> None:
        """
        Args:
            min_bi_len: 笔的最小跨度，**只喂 czsc 那一级**。
                ⚠️ 它**不属于** :class:`ReferenceChanlunConfig` ——
                实测（R45 补测试时撞见）传 ``ReferenceChanlunConfig(min_bi_len=…)``
                会直接 ``TypeError``，因为那个 dataclass 没有这个字段。
                理由与 ``code`` 同源：两者都是**标的/口径级**信息，
                而 :class:`ChanlunBackend` 契约只传 ``bars``。
            code: 标的代码。**只腾讯那条路需要它** —— :class:`CanonicalBar`
                **没有 ``code`` 字段**（R45 实测：``CanonicalBar.__init__``
                不接受该 kwarg），所以不能从 bars 里挖。
                而 :class:`ChanlunBackend` 契约只传 ``bars``。
                ⇒ 代码只能作为**后端级属性**显式给出。
                留 ``None`` 时若走到腾讯那一级，直接判定不可用（不猜代码）——
                与 ``factor_from_actions``「宁可少一个台阶，也不用猜的值」同原则。
        """
        self.code = code
        self.min_bi_len = min_bi_len
        self.source: str | None = None
        self.detail: str = "尚未运行"

    # ── 契约实现 ────────────────────────────────────────────────
    def compute_structures(
        self, bars: list[BarLike], config: ReferenceChanlunConfig
    ) -> ChanlunResult:
        """算参照结构（**契约方法**）。两条路都拿不到就抛 :class:`ReferenceUnavailableError`。

        ⚠️ 这里**不静默回落**到 native —— 参照侧回落到生产侧等于**自己跟自己
        比**，那是对照面板最没意义的一种「通过」。宁可报「没参照」。

        ## ⚠️ 走腾讯那一级时**直接抛**，不给残缺结果（R45 P1-3）

        腾讯路返回的是**已归一化的 dict**（字段是 ``start_time``/``end_time``，
        毫秒），而 :class:`ChanlunResult` 的 ``BiRaw``/``ZsRaw`` 要的是
        **bar 索引**（``start_bar``/``end_bar``）—— **两者量纲不同**。

        第一版硬转，把 ``start_bar`` 全设成 0，于是这个后端**看起来能用**，
        实际交出去的是**没有时间锚点的数据**。拿它画图或做点选定位都会静默错位。

        ⇒ 宁可**响亮拒绝**。要走腾讯对照，用
        :meth:`compute_domain_structures`（parity 层走的就是那条），
        它返回领域对象、不经过这层有损转换。
        """
        triples = self.compute_domain_structures(bars, config)
        if self.source == REFERENCE_SOURCE_TENCENT:
            raise IncompleteReferenceError(
                "腾讯回落路只服务 parity 对照：它给的是时间锚点（毫秒），"
                "而 ChanlunResult 要的是 bar 索引，硬转会丢锚点。"
                "请改用 compute_domain_structures()（parity 层就走那条）。"
            )
        return _from_domain(triples, bars)

    # ── 领域对象三元组（parity 层直接用，**不丢时间锚点**）────────
    def compute_domain_structures(
        self, bars: list[BarLike], config: ReferenceChanlunConfig
    ) -> tuple[list[Any], list[Any], list[Any]]:
        """返回 ``(fractals, bis, zhongshus)``。

        ⚠️ **为什么另开一个方法**而不是让调用方从 :class:`ChanlunResult` 反推：
        ``BiRaw`` / ``ZsRaw`` 存的是 **bar 索引**，而 parity 层要的是
        ``start_time`` / ``end_time``（毫秒时间戳）。
        两者**量纲不同** —— 我第一版硬转，结果把时间锚点全丢了
        （``start_bar=0``），parity 面板的「点选高亮到对应位置」就废了。
        ⇒ 参照侧把领域对象原样透出，转换交给需要它的那一层。
        """
        canonical = [b if isinstance(b, CanonicalBar) else _as_canonical(b) for b in bars]
        triples = self._try_czsc(canonical, config)
        if triples is not None:
            self.source = REFERENCE_SOURCE_CZSC
            self.detail = "czsc 实现对照"
            return triples
        triples = self._try_tencent(canonical, config)
        if triples is not None:
            self.source = REFERENCE_SOURCE_TENCENT
            self.detail = "腾讯 hfq 同窗口数据链路对照"
            return triples
        self.source = None
        self.detail = "czsc 未安装且腾讯 hfq 取不到"
        raise ReferenceUnavailableError(self.detail)

    def _min_bi_len(self, config: ReferenceChanlunConfig) -> int | None:
        """后端级 ``min_bi_len`` 优先；没给就退回 config（如果有这个字段）。"""
        if self.min_bi_len is not None:
            return self.min_bi_len
        return _rules_cfg(config).min_bi_len

    def _log_debug_no_code(self) -> None:
        """没给 code 就走到腾讯那一级 —— **降为 debug**：这是调用方没传参数，
        不是事件。真要用腾讯路请 ``ReferenceChanlunBackend(code="600519")``。"""
        _LOG.debug("参照侧没给标的代码，腾讯回落不可用（czsc 那级不受影响）")

    # ── 第一级：czsc（实现对照）─────────────────────────────────
    def _try_czsc(
        self, bars: list[CanonicalBar], config: ReferenceChanlunConfig
    ) -> tuple[list[Any], list[Any], list[Any]] | None:
        """czsc 不可用返回 ``None``（由调用方决定回落）。

        ⚠️ 降为 **debug**：这是**配置状态**（czsc 没装）而不是事件，
        而这段在**每次** A 股快照都会走 —— R38 实测它一个人占了线上日志的
        41%，把真信号淹掉了。常态由巡检统一汇报。
        """
        from cpt.adapters.backend_factory import resolve_backend
        from cpt.adapters.czsc_chanlun import CzscNotInstalledError, CzscVersionError
        from cpt.adapters.reference_pipeline import compute_domain_structures

        try:
            backend = resolve_backend("czsc", min_bi_len=self._min_bi_len(config))
        except (CzscNotInstalledError, CzscVersionError) as exc:
            _LOG.debug("参照侧 czsc 不可用，回落腾讯：%s", exc)
            return None
        fracs, bis, zss = compute_domain_structures(bars, _rules_cfg(config), backend)
        return list(fracs), list(bis), list(zss)

    # ── 第二级：腾讯 hfq（数据链路对照）────────────────────────
    def _try_tencent(
        self, bars: list[CanonicalBar], config: ReferenceChanlunConfig
    ) -> tuple[list[Any], list[Any], list[Any]] | None:
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
        from cpt.adapters.reference_pipeline import tencent_structures  # noqa: PLC0415

        # 腾讯那条路要**同窗口** —— 本地 N 根无缺口，腾讯 800 根含节假日缺口。
        # 窗口不对齐会造出「本地没有的日期」，那正是 R45 之前的对照口径错误。
        window = (bars[0].open_time, bars[-1].open_time) if len(bars) >= 2 else (0, 0)
        triples = tencent_structures(code, _rules_cfg(config), window)
        if triples is None:
            return None
        return triples  # 已是 dict 三分组，_normalize 直接吃


# ── 转换辅助：把两条路各自的数据形状统一成 ChanlunResult ──────────────
def _as_canonical(bar: BarLike) -> CanonicalBar:
    """把任意 ``BarLike`` 转成 :class:`CanonicalBar`。

    :class:`ChanlunBackend` 契约只说「典型为 ``CanonicalBar``」，
    所以非 ``CanonicalBar`` 的输入是**允许的**。

    ⚠️ R45 真机跑抓到两个**潜伏 bug**（这段在测试里从没被执行过 ——
    假实现全都直接返回 ``CanonicalBar``）：

    1. ``from cpt.domain.models import OHLCV`` —— **该符号不存在**，
       而且**根本用不到**（我写的时候误加了）⇒ 一旦传入非 CanonicalBar
       就 ImportError。
    2. 少传 5 个**必填**字段（``quote_volume`` / ``trade_count`` /
       ``taker_buy_base_volume`` / ``taker_buy_quote_volume`` /
       ``is_closed``）⇒ 修好 1 之后紧接着 TypeError。

    ⇒ 教训与今天第 N 次相同：**测试里走不到的分支就是没有测过**，
    「契约允许」不等于「实现支持」。
    """
    if isinstance(bar, CanonicalBar):
        return bar

    # ⚠️ **dict 也要能读**：``getattr(dict, "open_time", 0)`` 恒为 0 ——
    # 真机跑实测：传 dict 进来，全部字段静默变成 0，czsc 随后报
    # 「中位间隔 0 ms」这种**看不出根因**的错。⇒ 两种取法都支持。
    def _get(name: str, default: object = None) -> Any:
        if isinstance(bar, dict):
            return bar.get(name, default)
        return getattr(bar, name, default)

    def _f(name: str, default: float = 0.0) -> float:
        v = _get(name, default)
        return float(v) if v is not None else default

    def _i(name: str, default: int = 0) -> int:
        v = _get(name, default)
        return int(v) if v is not None else default

    # ⚠️ 判「**键在不在**」而不是「值是不是 0」—— 第一版写 `if not open_ms`
    # 会把 ``open_time=0`` 也当缺失。真机跑时它把 i=0 那根正常构造的 K 线
    # 误判成「缺 open_time」。
    if _get("open_time", None) is None:
        # 宁可炸也不要交出全 0 的 K 线 —— 那会让下游算出「间隔 0ms」之类的谜之错。
        raise ValueError(f"bar 缺少 open_time：{bar!r:.120}")
    open_ms = _i("open_time")

    return CanonicalBar(
        open_time=open_ms,
        # close_time 必须**严格大于** open_time（CanonicalBar.__post_init__ 会查），
        # 而多数 BarLike 只带 open_time ⇒ 缺省 +1ms。
        close_time=_i("close_time") if _get("close_time", None) is not None else open_ms + 1,
        open=_f("open"),
        high=_f("high"),
        low=_f("low"),
        close=_f("close"),
        volume=_f("volume"),
        quote_volume=_f("quote_volume"),
        trade_count=_i("trade_count"),
        taker_buy_base_volume=_f("taker_buy_base_volume"),
        taker_buy_quote_volume=_f("taker_buy_quote_volume"),
        is_closed=bool(_get("is_closed", True)),
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


def _time_of(obj: Any, field: str) -> int:
    """取结构对象上的 ``start_time`` / ``end_time``，**属性对象和 dict 都认**。

    与 :func:`_open_time_of` 同一个理由：这条链路上的结构既可能是领域对象
    （:class:`~cpt.domain.models.Bi` / ``ZhongShu``），也可能是经
    ``asdict`` 序列化后的 ``dict``（本仓多处投影都走这条路）。两种形态都支持，
    免得调用方为了换算锚点先做一次无谓的转换。
    """
    if isinstance(obj, Mapping):
        return int(obj[field])
    return int(getattr(obj, field))


def _open_time_of(bar: Any) -> int:
    """取 bar 的 ``open_time``，**属性对象和 dict 都认**。

    :class:`BarLike` 协议只承诺属性访问，但这条链路上 bars 实际有两种形态：
    ``CanonicalBar`` 之类的属性对象，和经过 :mod:`cpt.application._bar_dict`
    序列化后的 ``dict``（测试与部分调用方直接喂 dict）。两种都支持，别让调用方
    为了满足锚点换算而多做一次转换。
    """
    if isinstance(bar, Mapping):
        return int(bar["open_time"])
    return int(bar.open_time)


def _bar_index_by_open_time(bars: Sequence[BarLike]) -> dict[int, int]:
    """``bar.open_time -> bar 下标``。

    参照侧的结构对象（:class:`~cpt.domain.models.Bi` / ``ZhongShu``）只带
    ``start_time`` / ``end_time``，而 :class:`BiRaw` / :class:`ZsRaw` 要的是
    **bar 下标**，所以必须有一张时间 → 下标的表。

    同一 ``open_time`` 出现两次属于数据异常（时间轴不唯一），此时**保留较早的**
    下标并在 :func:`_anchor` 找不到时直接抛 —— 不静默取后者。
    """
    out: dict[int, int] = {}
    for idx, bar in enumerate(bars):
        out.setdefault(_open_time_of(bar), idx)
    return out


def _anchor(
    index_of: Mapping[int, int], time_ms: Any, *, what: str
) -> int:
    """把 ``start_time`` / ``end_time`` 解析成 bar 下标，**解析不到就抛**。

    绝不回落到 0：那正是本函数原来做的事，而 0 不是「缺省位置」，它是**第一根
    K 线**——所有笔和中枢都被锚到同一根上，画出来的图看着「有结构」但每个位置
    都错，比直接报错难查得多（R45 对腾讯路径就是这么处理的）。
    """
    key = int(time_ms)
    try:
        return index_of[key]
    except KeyError:
        raise IncompleteReferenceError(
            f"{what} 的时间锚点 {key} 不在 bars 里，无法换算成 bar 下标。"
            f"参照侧结构与 bars 不同源，不能硬猜下标 —— 请改用 "
            f"compute_domain_structures()，它直接给带时间的结构。"
        ) from None


def _ids_of(obj: Any, field: str) -> tuple[str, ...]:
    """取 ``source_ids`` / ``bi_ids``，属性对象和 dict 都认（同 :func:`_time_of`）。"""
    if isinstance(obj, Mapping):
        return tuple(obj.get(field) or ())
    return tuple(getattr(obj, field, ()) or ())


def _member_bi_indices(bis: Sequence[Any], z: Any) -> tuple[int, ...]:
    """中枢成员笔在**笔序列中的位置**（与 native / czsc 后端同一语义）。

    判定沿用 :meth:`cpt.adapters.native_chanlun.NativeChanlunBackend` 的做法 ——
    拿 ``ZhongShu.bi_ids`` 与 ``Bi.source_ids[0]`` 做 **ID 匹配**，不比较时间窗，
    也不返回 bar 下标（``bi_indices`` 一直是「第几笔」，下游 ``map_zhongshu``
    按它拼 ``bi:{i}``）。这里唯一实现，别再写第二套判定。

    原来这里是写死的 ``()``，与 ``start_bar=0`` 是同一类缺陷：中枢不知道由哪几笔
    构成，``tests/test_czsc_backend.py`` 断言的「中枢至少吸 3 笔」对参照侧无效。
    """
    wanted = set(_ids_of(z, "bi_ids"))
    return tuple(
        index
        for index, b in enumerate(bis)
        if (ids := _ids_of(b, "source_ids")) and ids[0] in wanted
    )


def _from_domain(triples: Sequence[Any], bars: Sequence[BarLike]) -> ChanlunResult:
    """领域对象三分组 → :class:`ChanlunResult`（**带真实 bar 锚点**）。

    ## 2026-10-06 修：czsc 分支的「丢时间锚点」

    原来这里对 ``bis`` / ``zhongshus`` 一律写 ``start_bar=0`` / ``end_bar=0``、
    ``bi_indices=()``，而 ``fx_list`` 却用了真实的 ``fractal.bar_index``。R45
    发现并修掉了**腾讯**路径（改成直接抛 :class:`IncompleteReferenceError`），
    **czsc 路径却照旧落进来**，于是 ``--backend reference`` 走 czsc 时产出的是
    一个「每笔每中枢都锚在第 0 根 K 线上」的 :class:`ChanlunResult`：下游
    ``map_bi(bars=...)`` 把所有笔与中枢的时间解析成 ``bars[0].open_time``，
    图看着有结构、位置全错。

    现在两条路径一致：能算出下标就算，算不出就抛。
    """
    fractals, bis, zhongshus = triples
    index_of = _bar_index_by_open_time(bars)

    bi_raw = tuple(
        BiRaw(
            direction=int(getattr(b, "direction", 0)),
            start_bar=_anchor(index_of, _time_of(b, "start_time"), what=f"第 {n} 笔的起点"),
            end_bar=_anchor(index_of, _time_of(b, "end_time"), what=f"第 {n} 笔的终点"),
            high=float(getattr(b, "high", 0.0)),
            low=float(getattr(b, "low", 0.0)),
            level=int(getattr(b, "level", 0)),
            power_price=float(getattr(b, "power_price", 0.0) or 0.0),
            power_volume=float(getattr(b, "power_volume", 0.0) or 0.0),
            length=int(getattr(b, "length", 0) or 0),
        )
        for n, b in enumerate(bis)
    )

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
        bi_list=bi_raw,
        zs_list=tuple(
            ZsRaw(
                start_bar=_anchor(index_of, _time_of(z, "start_time"), what=f"第 {n} 个中枢的起点"),
                end_bar=_anchor(index_of, _time_of(z, "end_time"), what=f"第 {n} 个中枢的终点"),
                high=float(getattr(z, "high", 0.0)),
                low=float(getattr(z, "low", 0.0)),
                level=int(getattr(z, "level", 0)),
                bi_indices=_member_bi_indices(bis, z),
            )
            for n, z in enumerate(zhongshus)
        ),
        level_map={},
    )
