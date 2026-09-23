from __future__ import annotations

import importlib.metadata
from datetime import datetime

import pytest
from scripts.compare_oracle import DEFAULT_DATA_DIR, RANGE_STARTS, _analyze, load_snapshot


def test_frozen_oracle_snapshots_have_expected_contiguous_candles() -> None:
    for start in RANGE_STARTS:
        path = DEFAULT_DATA_DIR / f"btcusdt_5m_{start[:10]}.csv"
        rows, digest = load_snapshot(path)
        assert len(rows) == 1000
        assert len(digest) == 64
        expected_start = int(datetime.fromisoformat(start).timestamp() * 1000)
        assert rows[0]["open_time"] == expected_start
        assert rows[0]["close_time"] - rows[0]["open_time"] == 299_999


def test_rust_oracle_counts_are_reproducible_for_frozen_sample() -> None:
    try:
        version = importlib.metadata.version("chanlun")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("optional chanlun oracle is not installed")
    if version != "2606.73":
        pytest.skip(f"expected optional chanlun==2606.73, found {version}")

    path = DEFAULT_DATA_DIR / "btcusdt_5m_2024-02-01.csv"
    rows, _ = load_snapshot(path)
    first = _analyze(rows)
    second = _analyze(rows)
    assert first == second
    assert first["oracle"]["fractals"] == 58
    assert first["oracle"]["bis"] == 57
    assert first["oracle"]["bi_centers"] == 9
    assert first["placeholder"]["bi_centers"] == 0
