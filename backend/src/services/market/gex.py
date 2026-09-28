"""Gamma exposure (GEX) arithmetic over an option chain (docs/todo/009).

Pure functions, no I/O. Used by the daily GLD snapshot collector.

**The sign is an assumption.** This is the common "naive GEX": dealers are
taken to be long calls and short puts, so call gamma counts positive and put
gamma negative. Nobody publishes who actually holds what, and for gold the
positioning is institutional rather than retail, so the assumption is weaker
than it is for the S&P. The collector stores the raw chain for exactly this
reason: a study can recompute under another convention.

Units: dollar gamma for a 1% move, gamma * OI * 100 shares * S^2 * 0.01.
Rows: {"strike", "t_years", "call_oi", "put_oi", "call_iv", "put_iv"}.
"""
from __future__ import annotations

import math
from typing import Optional

RISK_FREE = 0.04          # flat; gamma is insensitive to it at these tenors
FLIP_RANGE = 0.15         # search +-15% of spot for the zero crossing
FLIP_STEP = 0.0025        # in 0.25% steps


def _usable(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x)) and float(x) > 0
    except (TypeError, ValueError):
        return False


def bs_gamma(spot: float, strike: float, t_years: float, iv: float, r: float = RISK_FREE) -> float:
    if not (_usable(spot) and _usable(strike) and _usable(t_years) and _usable(iv)):
        return 0.0
    vol_t = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + iv * iv / 2.0) * t_years) / vol_t
    pdf = math.exp(-d1 * d1 / 2.0) / math.sqrt(2.0 * math.pi)
    return pdf / (spot * vol_t)


def _leg(spot: float, row: dict, side: str) -> float:
    oi = float(row.get(f"{side}_oi") or 0)
    if oi <= 0:
        return 0.0
    g = bs_gamma(spot, float(row["strike"]), float(row["t_years"]), row.get(f"{side}_iv"))
    return g * oi * 100.0 * spot * spot * 0.01


def per_strike(rows: list[dict], spot: float) -> list[dict]:
    out = []
    for r in rows:
        call = _leg(spot, r, "call")
        put = -_leg(spot, r, "put")
        out.append({**r, "call_gex": call, "put_gex": put})
    return out


def total_gex(rows: list[dict], spot: float) -> float:
    return sum(r["call_gex"] + r["put_gex"] for r in per_strike(rows, spot))


def _flip(rows: list[dict], spot: float) -> Optional[float]:
    """The price nearest spot at which total GEX changes sign, or None."""
    n = int(FLIP_RANGE / FLIP_STEP)
    grid = [spot * (1 + i * FLIP_STEP) for i in range(-n, n + 1)]
    values = [total_gex(rows, s) for s in grid]
    best = None
    for i in range(1, len(grid)):
        a, b = values[i - 1], values[i]
        if a == 0 or b == 0 or (a < 0) == (b < 0):
            continue
        x = grid[i - 1] + (grid[i] - grid[i - 1]) * (-a) / (b - a)
        if best is None or abs(x - spot) < abs(best - spot):
            best = x
    return round(best, 4) if best is not None else None


def summarise(rows: list[dict], spot: float) -> dict:
    if not rows:
        return {"total_gex": None, "flip_level": None, "call_wall": None,
                "put_wall": None, "n_rows": 0}
    per = per_strike(rows, spot)
    calls: dict[float, float] = {}
    puts: dict[float, float] = {}
    for r in per:
        k = float(r["strike"])
        calls[k] = calls.get(k, 0.0) + r["call_gex"]
        puts[k] = puts.get(k, 0.0) + r["put_gex"]
    call_wall = max(calls, key=calls.get) if any(v > 0 for v in calls.values()) else None
    put_wall = min(puts, key=puts.get) if any(v < 0 for v in puts.values()) else None
    return {
        "total_gex": round(sum(r["call_gex"] + r["put_gex"] for r in per), 2),
        "flip_level": _flip(rows, spot),
        "call_wall": call_wall,
        "put_wall": put_wall,
        "n_rows": len(rows),
    }


def to_xau(level: Optional[float], ratio: Optional[float]) -> Optional[float]:
    """A GLD-space level in gold dollars, by the ratio measured at the snapshot."""
    if level is None or not _usable(ratio):
        return None
    return round(level * ratio, 2)
