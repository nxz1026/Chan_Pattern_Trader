"""Multi-level structure projection for the Dashboard.

Wires the recursive chain (``docs/rules.md`` §3 / §8.3) into a single
producer:

* level 0 structures come from the native backend via
  :func:`cpt.adapters.native_chanlun.NativeChanlunBackend.compute_structures`
  (B1 activation);
* level ≥ 1 structures are derived by feeding low-level trend types
  (:func:`cpt.domain.trend_type.classify_trend`) through the recursion mapper
  (:func:`cpt.domain.recursion.map_trend_types`) and re-running the same
  :func:`cpt.domain.fractal.detect_fractals` / :func:`cpt.domain.bi.build_bis`
  / :func:`cpt.domain.zhongshu.build_zhongshus` pipeline on the higher-level
  candidate elements.

The producer is read-only, deterministic, and side-effect free. The web
provider forwards each level's structures to the Dashboard so the level
selector can request **real** multi-level data instead of merely filtering a
single-level snapshot on the front-end (audit item: "多级别真实叠加数据").
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from cpt.adapters.native_chanlun import NativeChanlunBackend
from cpt.adapters.reference_chanlun import (
    ReferenceChanlunConfig,
    map_bi,
    map_fractal,
    map_zhongshu,
)
from cpt.domain.bi import build_bis
from cpt.domain.config import RulesConfig
from cpt.domain.contain import merge_contained_bars
from cpt.domain.fractal import detect_fractals
from cpt.domain.models import Bi, CanonicalBar, Fractal, ZhongShu
from cpt.domain.recursion import map_trend_types
from cpt.domain.trend_type import classify_trend
from cpt.domain.zhongshu import build_zhongshus


def _ref_config(config: RulesConfig) -> ReferenceChanlunConfig:
    return ReferenceChanlunConfig(
        use_fx_qy_middle=config.fx_qy_middle,
        use_fx_qj_ck=config.fx_qj_ck,
        use_bi_type_new=config.bi_type_new,
        zs_wzgx=config.zs_wzgx,
        macd_fast=config.macd_fast,
        macd_slow=config.macd_slow,
        macd_signal=config.macd_signal,
    )


def _level_zero(
    bars: tuple[CanonicalBar, ...],
    config: RulesConfig,
    backend: NativeChanlunBackend,
    *,
    level: int,
) -> tuple[tuple[Fractal, ...], tuple[Bi, ...], tuple[ZhongShu, ...]]:
    result = backend.compute_structures(list(bars), _ref_config(config))
    fractals = tuple(
        map_fractal(fx, level=level, source_ids=(f"b:{fx.bar_index}",), bars=bars)
        for fx in result.fx_list
    )
    bis = tuple(
        map_bi(bi, level=level, source_ids=(f"fx:{bi.start_bar}", f"fx:{bi.end_bar}"), bars=bars)
        for bi in result.bi_list
    )
    zhongshus = tuple(
        map_zhongshu(zs, level=level, source_ids=tuple(f"bi:{i}" for i in zs.bi_indices), bars=bars)
        for zs in result.zs_list
    )
    return fractals, bis, zhongshus


def _next_level(
    fractals_low: tuple[Fractal, ...],
    bis_low: tuple[Bi, ...],
    zhongshus_low: tuple[ZhongShu, ...],
    *,
    source_level: int,
    target_level: int,
) -> tuple[tuple[Fractal, ...], tuple[Bi, ...], tuple[ZhongShu, ...]]:
    """Recurse one level up via trend-type classification and the recursion mapper."""
    trends = classify_trend(bis_low, zhongshus_low, level=source_level)
    elements = map_trend_types(trends, target_level=target_level)
    fractals_high = detect_fractals(cast(Sequence[Any], elements), level=target_level)
    bis_high = build_bis(fractals_high, level=target_level)
    zhongshus_high = build_zhongshus(bis_high, level=target_level)
    return fractals_high, bis_high, zhongshus_high


def build_multi_level(
    bars: Sequence[CanonicalBar],
    config: RulesConfig,
    backend: NativeChanlunBackend,
    *,
    levels: Sequence[int] = (5, 30),
) -> dict[int, dict[str, tuple[Any, ...]]]:
    """Build real multi-level structures. Returns a mapping keyed by level (minutes).

    The smallest requested level is the seed (computed from the native backend).
    Larger levels are produced by feeding the lower-level structures through
    the recursive chain ``classify_trend → map_trend_types → detect_fractals →
    build_bis → build_zhongshus`` and tagging the result with the requested
    level. Levels are produced strictly in ascending order so each higher level
    only depends on the previously emitted level.

    Passing a single level is allowed; the recursion only kicks in when the
    request contains two or more distinct levels.
    """
    bars_tuple = tuple(bars)
    requested = sorted(set(levels))
    if not requested:
        raise ValueError("at least one level is required")
    if not bars_tuple:
        return {
            level: {"fractals": (), "bis": (), "zhongshus": ()} for level in requested
        }
    out: dict[int, dict[str, tuple[Any, ...]]] = {}
    seed_level = requested[0]
    fractals, bis, zhongshus = _level_zero(bars_tuple, config, backend, level=seed_level)
    out[seed_level] = {"fractals": fractals, "bis": bis, "zhongshus": zhongshus}
    source_level = seed_level
    for target_level in requested[1:]:
        fractals, bis, zhongshus = _next_level(
            fractals,
            bis,
            zhongshus,
            source_level=source_level,
            target_level=target_level,
        )
        out[target_level] = {"fractals": fractals, "bis": bis, "zhongshus": zhongshus}
        source_level = target_level
    return out


def structures_for_level(
    multi: dict[int, dict[str, tuple[Any, ...]]], level: int
) -> tuple[tuple[Fractal, ...], tuple[Bi, ...], tuple[ZhongShu, ...]]:
    """Return ``(fractals, bis, zhongshus)`` for ``level``; fall back to nearest."""
    if level in multi:
        entry = multi[level]
        return (
            cast(tuple[Fractal, ...], entry["fractals"]),
            cast(tuple[Bi, ...], entry["bis"]),
            cast(tuple[ZhongShu, ...], entry["zhongshus"]),
        )
    nearest = min(multi.keys(), key=lambda candidate: abs(candidate - level))
    entry = multi[nearest]
    return (
        cast(tuple[Fractal, ...], entry["fractals"]),
        cast(tuple[Bi, ...], entry["bis"]),
        cast(tuple[ZhongShu, ...], entry["zhongshus"]),
    )


__all__ = ["build_multi_level", "structures_for_level"]