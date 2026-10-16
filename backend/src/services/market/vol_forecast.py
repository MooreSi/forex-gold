"""Forecast-volatility stops (docs/todo/019 B).

Risk > "Where a template's stop and targets land" > vol_forecast_stops_enabled,
OFF by default. On, `template_levels.template_atr` hands every dynamic-ATR
template this forecast instead of the ATR(14) of the last 20 M5 bars. That
moves the stop, TP1 and (through the stop) the lot size of real orders:
owner sign-off and a demo session first.

**Expected to change the stop's width, not the results.** `030-what-actually-
manages-a-trade-well.md` measured the short M5 ATR against a 14-hour ATR on
real fills and found nearly the same outcome. The owner chose to build it
anyway (2026-10-16); the spec records why that is a known risk.

The model is HAR (Corsi 2009) on hourly true ranges: the next hour's range
regressed on the last hour, the mean of the last day (24 h) and of the last
week (120 h), fitted by least squares over the last FIT_BARS closed H1 bars.
The forecast is then put into the units the template multipliers were tuned
on (M5 true range), and clamped to [CLAMP_LO, CLAMP_HI] x the current M5 ATR
so one bad fit cannot put a stop somewhere absurd.

Pure Python (no numpy, same choice as barrier_fit.py). Nothing here reaches
a broker.
"""
from __future__ import annotations

from typing import Optional, Sequence

SETTING_KEY = "vol_forecast_stops_enabled"
DAY_H = 24
WEEK_H = 120
FIT_BARS = 600
MIN_ROWS = 200
SCALE_H = 48          # hours of both series used to convert H1 -> M5 units
CLAMP_LO, CLAMP_HI = 0.5, 2.0


def enabled(rs: Optional[dict] = None) -> bool:
    """The switch. Off when it cannot be read."""
    try:
        if rs is None:
            from backend.src.db import database as _db
            rs = _db.get_risk_settings()
        return bool(int(rs.get(SETTING_KEY, 0) or 0))
    except Exception:
        return False


def true_ranges(bars: Sequence[dict]) -> list[float]:
    out = []
    prev = None
    for b in bars:
        h, lo = float(b["high"]), float(b["low"])
        out.append(h - lo if prev is None else max(h - lo, abs(h - prev), abs(lo - prev)))
        prev = float(b["close"])
    return out


def _solve(a: list[list[float]], y: list[float]) -> Optional[list[float]]:
    """Gaussian elimination with partial pivoting; None when singular."""
    n = len(y)
    m = [row[:] + [y[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = c
        for r in range(c + 1, n):
            if abs(m[r][c]) > abs(m[p][c]):
                p = r
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * z for x, z in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def _regressors(tr: Sequence[float], t: int) -> list[float]:
    return [1.0, tr[t], sum(tr[t - DAY_H + 1:t + 1]) / DAY_H,
            sum(tr[t - WEEK_H + 1:t + 1]) / WEEK_H]


def har_fit(tr: Sequence[float]) -> Optional[list[float]]:
    """[const, hour, day, week] by least squares, or None with fewer than
    MIN_ROWS rows or a singular fit."""
    rows = [(_regressors(tr, t), tr[t + 1]) for t in range(WEEK_H - 1, len(tr) - 1)]
    if len(rows) < MIN_ROWS:
        return None
    xtx = [[sum(x[i] * x[j] for x, _ in rows) for j in range(4)] for i in range(4)]
    xty = [sum(x[i] * y for x, y in rows) for i in range(4)]
    return _solve(xtx, xty)


def har_forecast(h1_bars: Sequence[dict]) -> Optional[float]:
    """Next hour's true range from CLOSED H1 bars, or None."""
    tr = true_ranges(h1_bars)
    coef = har_fit(tr)
    if coef is None:
        return None
    f = sum(c * x for c, x in zip(coef, _regressors(tr, len(tr) - 1)))
    return f if f > 0 else None


def to_m5_units(forecast_h1: float, h1_bars: Sequence[dict],
                m5_bars: Sequence[dict]) -> Optional[float]:
    """An H1 range in the units of an M5 true range: scaled by the ratio of
    their means over the last SCALE_H hours."""
    h1 = true_ranges(h1_bars)[-SCALE_H:]
    m5 = true_ranges(m5_bars)[-SCALE_H * 12:]
    if not h1 or not m5:
        return None
    mh, mm = sum(h1) / len(h1), sum(m5) / len(m5)
    return forecast_h1 * mm / mh if mh > 0 and mm > 0 else None


def forecast_atr(h1_bars: Sequence[dict], m5_bars: Sequence[dict],
                 m5_atr: float) -> Optional[float]:
    """The figure a dynamic-ATR template is sized from, or None to keep the
    M5 ATR."""
    f = har_forecast(h1_bars)
    if f is None:
        return None
    f = to_m5_units(f, h1_bars, m5_bars)
    if f is None or f <= 0:
        return None
    return min(max(f, CLAMP_LO * m5_atr), CLAMP_HI * m5_atr)
