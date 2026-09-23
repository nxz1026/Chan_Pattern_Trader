"""Read-only indicators used by Dashboard watch mode."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cpt.domain.config import RulesConfig
from cpt.domain.models import CanonicalBar


def macd_series(bars: Sequence[CanonicalBar], config: RulesConfig) -> tuple[dict[str, Any], ...]:
    """Return deterministic EMA/MACD values for the supplied close series.

    EMA seed = first close (standard practice for trend-following indicators).
    The signal EMA is initialized to 0 and is only populated once enough MACD
    samples exist (the first ``signal_period - 1`` rows have ``signal=None``
    /``histogram=None`` to avoid the bias that would result from seeding the
    signal EMA with the close price).
    """
    fast = config.macd_fast
    slow = config.macd_slow
    signal_period = config.macd_signal
    if not bars:
        return ()
    fast_alpha = 2.0 / (fast + 1)
    slow_alpha = 2.0 / (slow + 1)
    signal_alpha = 2.0 / (signal_period + 1)
    fast_ema = slow_ema = bars[0].close
    macd_values: list[float] = []
    for bar in bars:
        fast_ema = (bar.close - fast_ema) * fast_alpha + fast_ema
        slow_ema = (bar.close - slow_ema) * slow_alpha + slow_ema
        macd_values.append(fast_ema - slow_ema)
    result: list[dict[str, Any]] = []
    signal_ema: float | None = None
    for index, bar in enumerate(bars):
        macd = macd_values[index]
        if index + 1 >= signal_period:
            if signal_ema is None:
                seed_window = macd_values[index + 1 - signal_period : index + 1]
                signal_ema = sum(seed_window) / signal_period
            else:
                signal_ema = (macd - signal_ema) * signal_alpha + signal_ema
            histogram = macd - signal_ema
            result.append(
                {
                    "open_time": bar.open_time,
                    "macd": macd,
                    "signal": signal_ema,
                    "histogram": histogram,
                }
            )
        else:
            result.append(
                {
                    "open_time": bar.open_time,
                    "macd": macd,
                    "signal": None,
                    "histogram": None,
                }
            )
    return tuple(result)
