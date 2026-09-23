"""Research dashboard parity view-model helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def _key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("kind"),
        item.get("start_time", item.get("bar_index")),
        item.get("end_time"),
        item.get("direction"),
    )


def build_parity_view(
    cpt_items: Sequence[dict[str, Any]],
    oracle_items: Sequence[dict[str, Any]],
    *,
    kind: str,
) -> dict[str, Any]:
    """Pair two normalized structure sequences into a deterministic parity view."""
    cpt_by_key = {_key(item): item for item in cpt_items}
    oracle_by_key = {_key(item): item for item in oracle_items}
    rows: list[dict[str, Any]] = []
    for key in sorted(set(cpt_by_key) | set(oracle_by_key), key=str):
        cpt = cpt_by_key.get(key)
        oracle = oracle_by_key.get(key)
        if cpt is None:
            status = "missing"
        elif oracle is None:
            status = "extra"
        else:
            fields = sorted(set(cpt) | set(oracle))
            differences = [field for field in fields if cpt.get(field) != oracle.get(field)]
            status = "matched" if not differences else "mismatched"
        rows.append(
            {
                "kind": kind,
                "status": status,
                "key": list(key),
                "cpt": cpt,
                "oracle": oracle,
                "difference_fields": differences if cpt is not None and oracle is not None else [],
            }
        )
    matched = sum(row["status"] == "matched" for row in rows)
    return {
        "summary": {
            "matched": matched,
            "missing": sum(row["status"] == "missing" for row in rows),
            "extra": sum(row["status"] == "extra" for row in rows),
            "mismatched": sum(row["status"] == "mismatched" for row in rows),
            "total": len(rows),
            "match_rate": matched / len(rows) if rows else 1.0,
        },
        "items": tuple(rows),
    }


def build_parity_snapshot(
    *,
    fractals: tuple[dict[str, Any], ...] = (),
    bis: tuple[dict[str, Any], ...] = (),
    zhongshus: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Build the dashboard parity object from three normalized comparisons."""
    return {
        "fractals": build_parity_view(fractals, (), kind="fractal"),
        "bis": build_parity_view(bis, (), kind="bi"),
        "zhongshus": build_parity_view(zhongshus, (), kind="zhongshu"),
    }
