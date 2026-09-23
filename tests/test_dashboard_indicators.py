from __future__ import annotations

from cpt.application.dashboard_indicators import macd_series
from cpt.domain.config import RulesConfig
from cpt.domain.models import make_canonical_bar


def test_macd_series_is_deterministic_and_sized_to_bars() -> None:
    bars = tuple(
        make_canonical_bar(
            open_time=index * 300000,
            close_time=index * 300000 + 299999,
            open=10.0 + index,
            high=12.0 + index,
            low=8.0 + index,
            close=11.0 + index,
        )
        for index in range(40)
    )
    first = macd_series(bars, RulesConfig())
    assert first == macd_series(bars, RulesConfig())
    assert len(first) == len(bars)
    assert {"macd", "signal", "histogram"}.issubset(first[-1])
