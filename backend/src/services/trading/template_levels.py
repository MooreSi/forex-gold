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


def atr_scaled_ladder(template: Optional[dict], atr: float, ref: float,
                      sign: int, tps: dict) -> dict:
    """The anchor TP ladder, sized to volatility instead of to fixed pips.

    `use_dynamic_atr` already sizes the STOP and TP LEVEL 1 from ATR --
    "Dynamic ATR sizing of SL/TP1", the field's own documented scope. Every
    level above TP1 keeps a fixed pip distance, so on a volatile day the
    stop and the first target move out and the rest of the ladder does not.
    R is then constant at TP1 and drifts everywhere above it, which is the
    same payoff inversion `docs/todo/reversal-engine/200` section 1.1 set
    out to remove.

    `atr_ladder_scale` (default False, so no existing template changes)
    rescales the WHOLE ladder by the factor that puts TP1 on its ATR
    multiple. The relative spacing somebody tuned by hand survives
    untouched; only the size of the thing changes. Level 1 comes out
    identical either way, which is what makes this safe to layer over the
    existing override rather than replacing it.

    The ANCHOR ladder only. Pending-leg levels travel to the EA as pips
    measured from its own staging base and are not resolved to prices here,
    so scaling them would need the EA to agree; the recommended preset is
    single-entry and does not use them.

    Refuses rather than guesses in three cases: no ATR, no ladder, or no
    `tp1_pips` to define the shape relative to. In the last case there is no
    reference to preserve, and inventing one would silently move every level
    on a template whose author never set a first target.
    """
    if not template or not tps or atr <= 0:
        return tps
    if not bool(template.get("use_dynamic_atr")):
        return tps

    target_dist = atr_tp1_distance(template, atr) or 0.0
    factor = atr_scale_factor(template, atr)

    if factor is None:
        # The behaviour that already existed: level 1 only.
        out = dict(tps)
        out[1] = ref + sign * target_dist
        return out

    return {n: ref + sign * abs(price - ref) * factor for n, price in tps.items()}


def atr_sl_distance(template: Optional[dict], atr: float) -> Optional[float]:
    """The stop distance in PRICE that `use_dynamic_atr` implies, or None.

    One definition, shared by the live path (`template_sl_at`) and the
    backtest walk. Two copies of this rule is how a backtest ends up
    measuring a stop the live trade never had.
    """
    if not template or atr <= 0 or not bool(template.get("use_dynamic_atr")):
        return None
    return atr * float(template.get("atr_sl_mult") or 1.5)


def atr_tp1_distance(template: Optional[dict], atr: float) -> Optional[float]:
    """The first target's distance in PRICE, or None."""
    if not template or atr <= 0 or not bool(template.get("use_dynamic_atr")):
        return None
    return atr * float(template.get("atr_tp1_mult") or 1.5)


def atr_scale_factor(template: Optional[dict], atr: float) -> Optional[float]:
    """What to multiply every pips-derived TP distance by, or None.

    None means "do not rescale the ladder": either `atr_ladder_scale` is
    off, or there is no `tp1_pips` to define the shape relative to and so
    no shape to preserve.
    """
    target = atr_tp1_distance(template, atr)
    if target is None or not bool(template.get("atr_ladder_scale")):
        return None
    tp1_pips = float(template.get("tp1_pips") or 0.0)
    if tp1_pips <= 0:
        return None
    return target / (tp1_pips * PIPS_TO_PRICE_XAUUSD)
