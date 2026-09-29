"""How a Trend PA trade ends: on bars (the backtest) or on a tick (live).

Pure. The virtual trade has one stop and one target and nothing moves them;
the engine is meant to be simple, and a result that depends on management
rules is a result about the management rules.
"""
from __future__ import annotations

from typing import Optional


def resolve_bars(direction: str, entry: float, sl: float, tp: float, bars: list,
                 opened_ts: Optional[float] = None,
                 max_hold_s: Optional[float] = None) -> Optional[dict]:
    """The first exit in `bars`, or None if still open after all of them.

    A bar that spans both the stop and the target is a LOSS: one bar cannot
    say which came first, so the backtest assumes the worse, as the other two
    backtests here do.
    """
    buy = direction == "BUY"
    for b in bars:
        hi, lo = float(b["high"]), float(b["low"])
        hit_sl = lo <= sl if buy else hi >= sl
        hit_tp = hi >= tp if buy else lo <= tp
        if hit_sl:
            return {"outcome": "loss", "exit_price": sl, "exit_ts": b["ts"]}
        if hit_tp:
            return {"outcome": "win", "exit_price": tp, "exit_ts": b["ts"]}
        if (max_hold_s is not None and opened_ts is not None
                and float(b["ts"]) - float(opened_ts) >= max_hold_s):
            return {"outcome": "timeout", "exit_price": float(b["close"]), "exit_ts": b["ts"]}
    return None


def resolve_tick(direction: str, sl: float, tp: float, bid: float, ask: float) -> Optional[str]:
    """"win", "loss" or None. A buy closes on the bid, a sell on the ask."""
    if direction == "BUY":
        if bid <= sl:
            return "loss"
        if bid >= tp:
            return "win"
        return None
    if ask >= sl:
        return "loss"
    if ask <= tp:
        return "win"
    return None


def r_multiple(direction: str, entry: float, exit_price: float, risk: float,
               cost: float = 0.0) -> float:
    """Realised R after `cost` (price units: spread plus commission)."""
    move = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    return (move - cost) / risk
