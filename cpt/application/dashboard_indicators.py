"""Read-only indicators used by Dashboard watch mode."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cpt.domain.config import RulesConfig
from cpt.domain.models import CanonicalBar


def macd_series(bars: Sequence[CanonicalBar], config: RulesConfig) -> tuple[dict[str, Any], ...]:
    """Return deterministic EMA/MACD values for the supplied close series."""
    fast = config.macd_fast
    slow = config.macd_slow
    signal_period = config.macd_signal
    if not bars:
        return ()
    fast_alpha = 2.0 / (fast + 1)
    slow_alpha = 2.0 / (slow + 1)
    signal_alpha = 2.0 / (signal_period + 1)
    fast_ema = slow_ema = signal_ema = bars[0].close
    result: list[dict[str, Any]] = []
    for bar in bars:
        fast_ema = (bar.close - fast_ema) * fast_alpha + fast_ema
        slow_ema = (bar.close - slow_ema) * slow_alpha + slow_ema
        macd = fast_ema - slow_ema
        signal = (macd - signal_ema) * signal_alpha + signal_ema
        result.append(
            {
                "open_time": bar.open_time,
                "macd": macd,
                "signal": signal,
                "histogram": macd - signal,
            }
        )
    return tuple(result)
