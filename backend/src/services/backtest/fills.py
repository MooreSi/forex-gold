"""Where a zone signal actually fills.

Its own module rather than more of `engine.py`, which sits at the 800-line
ceiling. `engine` imports this; nothing here imports `engine`, so the signal
arrives as its three numbers rather than as a type — which also keeps this
callable from a study script that has rows, not `BtSignal`s.

The whole module exists because of one measurement. `engine._simulate` used to
fill every signal at the MIDPOINT of its entry zone, and the live path does
not: it fills when price ENTERS the zone, which for a signal placed against
the move — every reversal signal, and most Telegram zones — is the worst price
in the zone for that direction.

Measured 2026-09-22 against the 664 reversal-engine signals that reached the
broker and carry a recorded `trigger_price`:

    where the real fill landed in the zone, 0.0 = best edge, 1.0 = worst
        p25 0.84    median 0.95    p75 1.00
        98.1% of real fills were in the WORSE half

    modelled fill minus real fill
        midpoint      $1.34 BETTER than reality — 21% of the stop distance
        adverse edge  $0.16 worse than reality

That was not a small optimism. On 666 real executions the midpoint walk scored
**135 of 283 real losses as wins**, against 18 the other way: the wrong sign on
one trade in five, almost always in its own favour. After the fix the same
walk returned -$0.17/trade against a real ledger of -$0.33, with its errors
roughly balanced (49 false wins, 74 false losses).

The residual $0.16 is deliberately left on the pessimistic side. A backtest
that informs a money decision should err against the trade.

See `docs/system/domains/market/030-what-actually-manages-a-trade-well.md`.
"""
from __future__ import annotations

# `entry_fill_price` alone. `_adverse_zone_edge` is the sentence it is built
# from, not a second surface: a declared export is a statement about what a
# module is FOR, and this module is for one question.
__all__ = ["entry_fill_price"]


def _adverse_zone_edge(entry_low: float, entry_high: float, is_buy: bool) -> float:
    """The edge of the zone this direction is filled at.

    A buy zone sits below price and price falls into it, so the first price
    inside it a buyer can be filled at is its HIGH — which is also the worst
    price in the zone for a buyer. Both readings give the same number, which
    is why it is stated once here rather than argued about per call site.
    """
    return float(entry_high) if is_buy else float(entry_low)


def entry_fill_price(sig, is_buy: bool, spread_pts: float) -> float:
    """Where `sig` fills, with the half-spread paid in the adverse direction.

    `sig` is anything carrying `entry_low` and `entry_high`.
    """
    half = float(spread_pts) / 2.0
    edge = _adverse_zone_edge(sig.entry_low, sig.entry_high, is_buy)
    return edge + half if is_buy else edge - half
