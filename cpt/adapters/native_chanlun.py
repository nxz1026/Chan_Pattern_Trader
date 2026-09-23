"""Native CPT domain backend for replay and local production runs."""

from __future__ import annotations

from cpt.adapters.reference_chanlun import (
    BiRaw,
    ChanlunResult,
    FxRaw,
    ReferenceChanlunConfig,
    ZsRaw,
)
from cpt.domain.bi import build_bis
from cpt.domain.contain import merge_contained_bars
from cpt.domain.fractal import detect_fractals
from cpt.domain.types import BarLike
from cpt.domain.zhongshu import build_zhongshus


class NativeChanlunBackend:
    """Run CPT's own domain pipeline behind the replay backend contract.

    Accepts :class:`BarLike` per the backend protocol; only ``CanonicalBar`` is
    fully wired through to :func:`merge_contained_bars` because the native
    pipeline needs OHLC + volume fields.  Other ``BarLike`` consumers (e.g.
    recursion feeding low-level trend types back into high-level structure) are
    not routed through this adapter.
    """

    def compute_structures(
        self, bars: list[BarLike], config: ReferenceChanlunConfig
    ) -> ChanlunResult:
        from cpt.domain.models import CanonicalBar

        canonical_bars: list[CanonicalBar] = [bar for bar in bars if isinstance(bar, CanonicalBar)]
        if not canonical_bars:
            return ChanlunResult((), (), (), {})
        merged = merge_contained_bars(canonical_bars)
        fractals = detect_fractals(merged, level=0)
        bis = build_bis(fractals, level=0)
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
        bi_raw = tuple(
            BiRaw(
                direction=bi.direction,
                start_bar=bi_start_raw[bi.start_time],
                end_bar=bi_end_raw[bi.end_time],
                high=bi.high,
                low=bi.low,
                level=bi.level,
            )
            for bi in bis
        )
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
