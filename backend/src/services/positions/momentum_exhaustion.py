"""Momentum-exhaustion / rejection re-check -- run right before a signal
that has been sitting pending actually fires, alongside a fresh re-score of
whichever local ML model that engine already trains (Breakout Engine's
bo_ml, Reversal Engine's re_ml). Neither engine previously re-examined the
most recently closed candle at fill time; both simply trusted the
creation-time conditions however stale the signal had gone.

Deliberately a fast, local, deterministic check -- no ML/AI call -- since
this runs on the same tick that decides whether to place a real order, and
a network round-trip (LLM or otherwise) would itself reintroduce the exact
staleness this exists to catch. See Breakout Engine's own long-standing
"anti-exhaustion guard" in signal_generator.check_breakout_go (candle body
vs ATR, unrelated to direction) for the same idea applied at creation time
-- this is that same idea, direction-aware, re-applied at fill time.
"""
from __future__ import annotations

_EXHAUSTION_ATR_MULT = 1.4   # with-direction: body this many ATRs -- move already happened
_AGAINST_ATR_MULT     = 1.0   # against-direction: a smaller body already counts as a red flag
_REJECTION_WICK_FRAC  = 0.55  # wick this fraction of the candle's range counts as rejection


def check_momentum_exhaustion(direction: str, candles: list[dict], atr: float) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means the most recently closed candle
    already shows the move (or a reversal against it) having happened --
    firing now would chase an extended move or fight a fresh rejection,
    exactly what a stale pending signal risks doing.

    direction: "BUY" or "SELL" -- the trade about to be entered.
    candles: recent OHLC candles, oldest first, LAST entry the most
    recently CLOSED candle (matches every other caller's convention in
    this codebase, e.g. signal_generator.check_breakout_go).
    atr: current ATR, same price units as candle high/low.
    """
    if not candles or atr <= 0:
        return True, ""
    last = candles[-1]
    try:
        o, h, l, c = (float(last[k]) for k in ("open", "high", "low", "close"))
    except (KeyError, TypeError, ValueError):
        return True, ""
    rng = h - l
    if rng <= 0:
        return True, ""

    is_sell = direction.upper() == "SELL"
    body = c - o  # signed: negative = down candle, positive = up candle
    with_dir_body  = -body if is_sell else body   # positive when the candle moved WITH our direction
    against_body   = -with_dir_body                # positive when it moved AGAINST our direction

    if with_dir_body > atr * _EXHAUSTION_ATR_MULT:
        return False, (
            f"exhaustion: last candle already moved {abs(body):.2f} "
            f"({with_dir_body / atr:.1f}x ATR) in the {direction} direction -- "
            f"the move already happened, entry now would chase it"
        )

    if against_body > atr * _AGAINST_ATR_MULT:
        return False, (
            f"reversal: last candle moved {abs(body):.2f} "
            f"({against_body / atr:.1f}x ATR) AGAINST the {direction} direction -- "
            f"market has turned since this signal was created"
        )

    # Rejection wick -- price pushed toward the extreme our entry needs and
    # got rejected back, regardless of where the candle closed net.
    wick = (min(o, c) - l) if is_sell else (h - max(o, c))
    if wick / rng > _REJECTION_WICK_FRAC:
        return False, (
            f"rejection: last candle's {'lower' if is_sell else 'upper'} wick is "
            f"{wick / rng:.0%} of its range -- price was already rejected at the "
            f"{'lows' if is_sell else 'highs'}, against a {direction} entry"
        )

    return True, ""
