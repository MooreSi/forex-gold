"""Shared indicator maths (2026-08-04).

EMA and RSI previously existed only as private helpers inside
ui/pages/chart.py. The signal-snapshot capture needs the same numbers, and
two copies of indicator maths is exactly the kind of thing that silently
drifts apart -- one gets a bug fix, the other does not, and then the chart
and the recorded data disagree about what the market looked like.

ATR and ADX are NOT duplicated here: dpm_engine already owns those and is
already the shared implementation. Import them from there.
"""
from __future__ import annotations

from typing import Optional


def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    """Standard EMA, one output per input index. None until `period` samples
    have been seen, so a caller can never mistake a warm-up value for a
    settled one."""
    if not values:
        return []
    out: list[Optional[float]] = []
    alpha = 2.0 / (period + 1)
    ema: Optional[float] = None
    warm = 0
    for v in values:
        if ema is None:
            ema, warm = v, 1
        else:
            ema = alpha * v + (1 - alpha) * ema
            warm += 1
        out.append(round(ema, 2) if warm >= period else None)
    return out


def ema_last(values: list[float], period: int) -> Optional[float]:
    s = ema_series(values, period)
    return s[-1] if s else None


def rsi_series(closes: list[float], period: int = 14) -> list[Optional[float]]:
    """RSI using Wilder's smoothing. Returns one value per close, with the
    first `period` entries None."""
    if len(closes) < period + 1:
        return [None] * len(closes)
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out: list[Optional[float]] = [None] * period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out.append(100.0 if avg_l == 0 else
                   round(100.0 - 100.0 / (1.0 + avg_g / avg_l), 2))
    return out


def rsi_last(closes: list[float], period: int = 14) -> Optional[float]:
    s = rsi_series(closes, period)
    return s[-1] if s else None
