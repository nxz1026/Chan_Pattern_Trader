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

        def bar_index(timestamp: int) -> int:
            return next(
                (index for index, bar in enumerate(canonical_bars) if bar.open_time == timestamp),
                0,
            )

        bi_raw = tuple(
            BiRaw(
                direction=bi.direction,
                start_bar=bar_index(bi.start_time),
                end_bar=bar_index(bi.end_time),
                high=bi.high,
                low=bi.low,
                level=bi.level,
            )
            for bi in bis
        )
        zs_raw = tuple(
            ZsRaw(
                start_bar=bar_index(zhongshu.start_time),
                end_bar=bar_index(zhongshu.end_time),
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
