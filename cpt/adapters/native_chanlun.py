"""Native CPT domain backend for replay and local production runs."""

from __future__ import annotations

from typing import Any

from cpt.adapters.reference_chanlun import (
    BiRaw,
    ChanlunResult,
    FxRaw,
    ReferenceChanlunConfig,
    ZsRaw,
)
from cpt.domain.bi import build_bis
from cpt.domain.contain import merge_contained_bars
from cpt.domain.first_buy import round_to_2_digit
from cpt.domain.fractal import detect_fractals
from cpt.domain.types import BarLike
from cpt.domain.zhongshu import build_zhongshus


def require_all_canonical_bars(
    bars: list[BarLike], canonical: type[Any], *, backend: str
) -> list[Any]:
    """把 ``bars`` 原样交给流水线（**不筛**），混合输入则抛。

    ``czsc_chanlun`` 共用这一份实现 —— 两个后端的锚点口径必须一致，抄两份
    必然漂移（这正是 R45 那类「两份实现各有一份 bug」的成因）。

    ## 为什么混合输入必须抛，而不是「筛掉非 CanonicalBar 继续算」

    两个后端产出的 ``FxRaw.bar_index`` / ``BiRaw.start_bar`` / ``end_bar`` /
    ``ZsRaw.*_bar`` 全是**下标**，下游
    :func:`cpt.adapters.reference_chanlun._resolve_time` 拿它们去**调用方给的
    原始 ``bars``** 里取 ``open_time``。一旦这里先筛一遍再按下标发出去，
    两个列表长度就不同了 ⇒ 下标**整体错位** ⇒ 时间锚点指向别的 K 线，
    而结构照常「算出来了」，不抛任何异常。

    生产目前只传 ``CanonicalBar``，所以这是潜在而非已发生的错位；但错位的结果
    是**看起来正常的假结构**，比直接报错贵得多 —— 所以这里宁可响亮地拒绝。
    """
    if all(isinstance(bar, canonical) for bar in bars):
        return list(bars)
    offenders = [type(bar).__name__ for bar in bars if not isinstance(bar, canonical)]
    raise ValueError(
        f"{backend} backend 只接受 {canonical.__name__}，收到混合输入 "
        f"{len(offenders)}/{len(bars)} 根非 {canonical.__name__}"
        f"（{sorted(set(offenders))}）：下标会被下游按**原始 bars** 解析，"
        f"筛掉它们会让所有 bar_index 错位"
    )


class NativeChanlunBackend:
    """Run CPT's own domain pipeline behind the replay backend contract.

    Accepts :class:`BarLike` per the backend protocol; only ``CanonicalBar`` is
    fully wired through to :func:`merge_contained_bars` because the native
    pipeline needs OHLC + volume fields.  Other ``BarLike`` consumers (e.g.
    recursion feeding low-level trend types back into high-level structure) are
    not routed through this adapter.
    """

    def __init__(self, *, min_bi_len: int | None = None) -> None:
        """``min_bi_len`` = 底层笔的最少跨度（去包含后 K 线根数）。

        2026-10-06 接线：此前 ``resolve_backend(..., min_bi_len=...)`` 只往 czsc
        传，native 分支**静默丢弃**——而生产用的就是 native（``DEFAULT_BACKEND``
        就是它），所以这条门槛在生产里从未生效。backend_factory 的 docstring
        当时写的是「native 不参与（见后文）」，后文并没有实现，属于声明超前。

        ⚠️ **那份 docstring 直到今天（2026-10-06 上线前审计）才改掉** ——
        也就是说，门槛接线生效之后的**整整一天**里，``resolve_backend`` 的文档
        仍在说「native 忽略本参数」。谁照着它读，就会得出「生产门槛没生效」
        的**反向结论**，进而怀疑整套定档工作。文档撒谎的代价与它描述的
        那个 bug 同量级，而且更隐蔽：真 bug 会让测试红，撒谎的文档只会
        让人做出错误判断而不留任何痕迹。
        """
        if min_bi_len is not None and min_bi_len < 1:
            raise ValueError(f"min_bi_len 必须 >= 1 或 None, 实测 {min_bi_len}")
        self._min_bi_len = min_bi_len

    def compute_structures(
        self, bars: list[BarLike], config: ReferenceChanlunConfig
    ) -> ChanlunResult:
        from cpt.domain.models import CanonicalBar

        canonical_bars = require_all_canonical_bars(bars, CanonicalBar, backend="native")
        if not canonical_bars:
            return ChanlunResult((), (), (), {})
        merged = merge_contained_bars(canonical_bars)
        fractals = detect_fractals(merged, level=0)
        bis = build_bis(fractals, level=0, min_bi_len=self._min_bi_len)
        zhongshus = build_zhongshus(bis, level=0)
        fx_raw = tuple(
            FxRaw(
                bar_index=fractal.bar_index,
                kind=fractal.kind,
                high=fractal.high,
                low=fractal.low,
                level=fractal.level,
            )
            for fractal in fractals
        )

        # 分型的时间来自**包含处理后**的 K 线：``start_time`` 是该合并 K 线的
        # ``open_time``、``end_time`` 是它的 ``close_time``。笔的时间直接继承两端
        # 分型，因此必须用分型时间反查原始下标——拿 close_time 去匹配原始
        # ``open_time`` 会全部落空，退化成"所有笔 end_bar=0"的假结构（2026-09-23 实测）。
        fractal_start_raw = {fractal.start_time: fractal.bar_index for fractal in fractals}
        fractal_end_raw = {fractal.end_time: fractal.bar_index for fractal in fractals}

        def resolve(mapping: dict[int, int], key: int, what: str) -> int:
            value = mapping.get(key)
            if value is None:
                raise ValueError(
                    f"native backend 无法把{what}时间 {key} 映射回原始 K 线下标"
                    f"（分型 {len(fractals)} 个 / 笔 {len(bis)} 条）"
                )
            return value

        bi_start_raw = {
            bi.start_time: resolve(fractal_start_raw, bi.start_time, "笔起点") for bi in bis
        }
        bi_end_raw = {bi.end_time: resolve(fractal_end_raw, bi.end_time, "笔终点") for bi in bis}

        # 力度度量（一买/一卖背驰比较用，见 cpt.domain.first_buy）。口径与 czsc
        # 对齐：``length`` 是笔的**去包含后** K 线根数、``power_volume`` 是中间
        # K 线的成交量之和（不含两端）、``power_price`` 是两端价差保留 2 位。
        # 分型的 ``bar_index`` 是**原始**下标，必须先反查回包含处理后的下标，
        # 否则 length/volume 会按原始 K 线算，与 czsc 不同量纲。
        merged_index = {bar.source_indices[0]: index for index, bar in enumerate(merged)}

        bi_raw_list: list[BiRaw] = []
        for bi in bis:
            start = merged_index[bi_start_raw[bi.start_time]]
            end = merged_index[bi_end_raw[bi.end_time]]
            bi_raw_list.append(
                BiRaw(
                    direction=bi.direction,
                    start_bar=bi_start_raw[bi.start_time],
                    end_bar=bi_end_raw[bi.end_time],
                    high=bi.high,
                    low=bi.low,
                    level=bi.level,
                    power_price=round_to_2_digit(bi.high - bi.low),
                    power_volume=sum(merged[i].volume for i in range(start + 1, end)),
                    length=end - start + 1,
                )
            )
        bi_raw = tuple(bi_raw_list)
        zs_raw = tuple(
            ZsRaw(
                start_bar=bi_start_raw[zhongshu.start_time],
                end_bar=bi_end_raw[zhongshu.end_time],
                high=zhongshu.high,
                low=zhongshu.low,
                level=zhongshu.level,
                bi_indices=tuple(
                    index
                    for index, bi in enumerate(bis)
                    if bi.source_ids and bi.source_ids[0] in zhongshu.bi_ids
                ),
            )
            for zhongshu in zhongshus
        )
        return ChanlunResult(fx_raw, bi_raw, zs_raw, {})
