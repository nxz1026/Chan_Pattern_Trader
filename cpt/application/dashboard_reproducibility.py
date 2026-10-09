"""Research dashboard reproducibility helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Final

from cpt.domain.config import SCHEMA_VERSION

#: 快照 ``reproducibility.rules_version`` 的唯一口径来源。
#:
#: R59（审计 M26）：原值硬编码 ``"rules.v0"``，而领域配置
#: ``cpt.domain.config.SCHEMA_VERSION`` 早在 R38 就升到 ``"v1"`` —— 于是快照
#: 对外宣称的口径号比实际规则老了一代。``cpt/domain/config.py`` 明写
#: v0/v1 结构不可比（``from_dict`` 会拒绝不匹配的 ``config_version``），
#: 所以这不是「标签写错」，是**拿旧口径号描述新规则**。
#: 从 SCHEMA_VERSION 派生而不是再写一遍字面量：下次升 v2 时不会漏改这里。
RULES_VERSION: Final[str] = f"rules.{SCHEMA_VERSION}"


def config_hash(config: object) -> str:
    """Return a stable SHA-256 hash for a config-like object."""
    if hasattr(config, "to_dict"):
        value = config.to_dict()
    elif isinstance(config, dict):
        value = config
    else:
        raise TypeError("config must provide to_dict() or be a dict")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _closed_bars_subset(
    snapshot: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(closed_bars, unclosed_bars)`` slices of the candle series.

    The full ``dataset_hash`` includes the live tail (the still-updating bar),
    which makes the hash unstable across polls for the same window. Hashing
    only the closed bars gives a stable fingerprint that downstream
    "可复现性" comparisons can rely on, while leaving the live tail
    hashable separately under ``unclosed_tail_hash``.
    """
    candles = snapshot.get("candles") or []
    closed: list[dict[str, Any]] = []
    unclosed: list[dict[str, Any]] = []
    for bar in candles:
        if not isinstance(bar, Mapping):
            continue
        is_closed = bool(bar.get("is_closed", bar.get("isClosed", False)))
        if is_closed:
            closed.append(dict(bar))
        else:
            unclosed.append(dict(bar))
    return closed, unclosed


def reproducibility_metadata(
    snapshot: dict[str, Any],
    *,
    config: object | None = None,
    rules_version: str = RULES_VERSION,
    engine_version: str = "cpt.v0",
) -> dict[str, Any]:
    """Build a stable, JSON-compatible reproducibility panel.

    ``dataset_hash`` is computed over the closed-bars subset only (the
    reproducible slice). The still-live tail is hashed separately as
    ``unclosed_tail_hash`` so callers can see exactly which bytes are
    excluded from the stable fingerprint. ``dataset_scope`` documents the
    policy in the payload so consumers don't have to guess.
    """
    closed_bars, unclosed_bars = _closed_bars_subset(snapshot)
    encoded_closed = _stable_payload(closed_bars)
    encoded_unclosed = _stable_payload(unclosed_bars)
    return {
        "dataset_hash": hashlib.sha256(encoded_closed.encode("utf-8")).hexdigest(),
        "unclosed_tail_hash": hashlib.sha256(encoded_unclosed.encode("utf-8")).hexdigest(),
        "dataset_scope": "closed_bars_only",
        "closed_bar_count": len(closed_bars),
        "unclosed_bar_count": len(unclosed_bars),
        "config_hash": config_hash(config) if config is not None else None,
        "rules_version": rules_version,
        "schema_version": snapshot.get("schema_version"),
        "engine_version": engine_version,
        "generated_at": snapshot.get("runtime", {}).get("generated_at"),
        "source": snapshot.get("runtime", {}).get("data_source"),
    }


def snapshot_diff(left: dict[str, Any], right: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return deterministic top-level field differences between snapshots."""
    differences: list[dict[str, Any]] = []
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            differences.append({"field": key, "left": left.get(key), "right": right.get(key)})
    return tuple(differences)
