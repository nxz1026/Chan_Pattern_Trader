"""Research dashboard reproducibility helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


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


def reproducibility_metadata(
    snapshot: dict[str, Any],
    *,
    config: object | None = None,
    rules_version: str = "rules.v0",
    engine_version: str = "cpt.v0",
) -> dict[str, Any]:
    """Build a stable, JSON-compatible reproducibility panel."""
    data = snapshot.get("data", snapshot)
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "dataset_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
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
