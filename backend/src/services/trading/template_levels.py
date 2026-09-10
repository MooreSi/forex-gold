"""Where an EA Template's stop and targets land, measured from a price the
caller names.

limit-orders/020. A template states its levels as distances -- `sl_pips`,
`tp{n}_pips`, or an ATR multiple -- so turning them into prices needs a
reference. Every existing caller uses the current tick, because every existing
caller opens at the current tick.

A resting order does not. It fills later, at a price being named now, which may
be an hour and many points away -- so the same conversion applied unchanged
puts the distance right and the level wrong. Live 2026-09-10 the gap was 13.74
points: a template stop of 60 pips would have landed at 4422.74, seven points
ABOVE a BUY's own entry, which is not a stop at all.

So the conversion lives here and takes its reference explicitly. The market
path passes the tick and behaves exactly as before; the limit path passes the
resting price (owner decision, 2026-09-10 --
docs/todo/limit-orders/QUESTIONS.md #1).

**SL and TPs share one reference, always.** `resolution.py`'s own comment says
so: "Computed from the same price reference resolve_template_tps() uses for the
TP ladder, so SL and TP measure from the same entry reference." Splitting them
would give a trade a stop measured from one price and targets from another.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD

log = logging.getLogger(__name__)


class PriceRef:
    """A tick-shaped reference at a single price.

    `resolve_template_tps` reads `tick.ask`/`tick.bid` -- the ask for a BUY's
    anchor and the bid as the EA's own grid base, one spread apart, because a
    market order really does cross the spread.

    A resting limit order does not: it fills at ONE named price or not at all,
    and there is no second side to measure from. So both sides are that price,
    and the ladder comes out measured from where the trade will actually open.
    A tick-shaped object rather than a new parameter on `resolve_template_tps`
    deliberately -- that function is 100 lines of carefully-reasoned ladder
    resolution near open_trade.py's LOC ceiling, and one implementation of it
    is worth more than a tidier signature.
    """

    __slots__ = ("bid", "ask", "mid")

    def __init__(self, price: float):
        self.bid = self.ask = self.mid = float(price)


def template_sl_at(template: Optional[dict], direction: str, ref_px: float,
                   dpm_candles: Any = None) -> Optional[float]:
    """The stop an EA Template puts on a trade entering at `ref_px`, or None.

    None means "the template does not state one" -- `sl_pips = 0` is unset, not
    an instruction to invent a stop -- and every caller then keeps the signal's
    own. That behaviour is unchanged from `resolution.py`, which this was
    extracted from; the only thing that moved is where the reference comes
    from.

    `use_dynamic_atr` beats `sl_pips` when candle data is available to compute
    an ATR, per that field's own comment ("sl_pips is ignored in favour of ATR
    x atr_sl_mult"), and falls back to `sl_pips` when it is not.
    """
    if not template:
        return None
    dist = None
    if bool(template.get("use_dynamic_atr")) and dpm_candles:
        from backend.src.services.dpm.engine import compute_atr
        atr = compute_atr(dpm_candles, period=int(template.get("atr_period") or 14)) or 0.0
        if atr > 0:
            dist = atr * float(template.get("atr_sl_mult") or 1.5)
    if dist is None:
        pips = float(template.get("sl_pips") or 0)
        if pips > 0:
            dist = pips * PIPS_TO_PRICE_XAUUSD
    if dist is None:
        return None
    up = direction.upper() == "BUY"
    return round(ref_px - dist if up else ref_px + dist, 2)
