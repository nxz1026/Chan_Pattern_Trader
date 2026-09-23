"""Read-only RulesConfig comparison projection."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, cast


def compare_configs(left: object, right: object) -> tuple[dict[str, Any], ...]:
    """Return deterministic field-level config differences."""
    if not is_dataclass(left) or not is_dataclass(right):
        raise TypeError("config values must be dataclass instances")
    left_values = asdict(cast(Any, left))
    right_values = asdict(cast(Any, right))
    return tuple(
        {
            "field": field,
            "left": left_values.get(field),
            "right": right_values.get(field),
        }
        for field in sorted(set(left_values) | set(right_values))
        if left_values.get(field) != right_values.get(field)
    )
