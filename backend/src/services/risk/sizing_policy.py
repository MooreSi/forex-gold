"""Sizing as a policy, not a constant.

Section 5.4 of `docs/todo/reversal-engine/200`.

The app sizes by risk-per-trade percent in `trading/fees_sizing.suggest_lot_
size`. That is the right base and it is the whole of it. Three things a desk
does on top are missing:

  * **volatility targeting** -- the same percent risk on a violent day is
    more risk, not the same risk
  * **drawdown scaling** -- size down while losing, the only free reduction
    in the probability of ruin there is
  * **a correlated exposure cap** -- six open XAUUSD signals in the same
    direction are one position of six times the size, and
    `_MAX_OPEN_SIGNALS = 6` counts signals, not exposure

**This modifies a base lot size, it never computes one.** Duplicating
`suggest_lot_size` would put two sizing rules in the codebase and the wrong
one would eventually win an argument nobody was having.

**Every scalar is exactly 1.0 when its input is absent**, so `apply` with a
default `SizingInputs` returns the lot size it was given, unchanged and to
the last decimal place. Nothing sizes differently until a dial moves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# A quiet hour is usually the hour before it stops being quiet, so the
# upside of volatility targeting is capped much harder than the downside.
MAX_VOL_SCALAR = 1.5
MIN_VOL_SCALAR = 0.25

# A sizing curve must never reach zero. Stopping is a circuit-breaker
# decision with a reason attached, not a rounding outcome.
MIN_DD_SCALAR = 0.25

# Full Kelly on an ESTIMATED probability is a fast route to ruin: the
# estimate is wrong and Kelly is convex in that error. Quarter to half is
# the conventional range and this is the ceiling, whatever fraction is asked
# for.
MAX_KELLY = 0.5

MIN_LOT = 0.01


@dataclass(frozen=True)
class SizingInputs:
    atr: float = 0.0
    reference_atr: float = 0.0
    drawdown_pct: float = 0.0
    dd_start_pct: float = 0.05
    dd_full_pct: float = 0.20
    # Lots already open in the same instrument and direction, and the cap.
    # A cap of 0.0 means OFF, matching every other setting in this codebase.
    open_correlated_lots: float = 0.0
    correlated_cap_lots: float = 0.0


@dataclass(frozen=True)
class SizingResult:
    lots: float
    notes: list = field(default_factory=list)


def volatility_scalar(atr: float, reference_atr: float) -> float:
    """Shrink size when the instrument is moving more than its reference."""
    if atr <= 0 or reference_atr <= 0:
        return 1.0
    return max(MIN_VOL_SCALAR, min(MAX_VOL_SCALAR, reference_atr / atr))


def drawdown_scalar(drawdown_pct: float, start_pct: float = 0.05,
                    full_pct: float = 0.20) -> float:
    """1.0 until `start_pct` of drawdown, tapering to `MIN_DD_SCALAR`.

    A grace band before the taper starts, because normal variance is not a
    drawdown and sizing down inside it just lowers the recovery rate.
    """
    dd = max(0.0, float(drawdown_pct))
    if dd <= start_pct or full_pct <= start_pct:
        return 1.0
    progress = min(1.0, (dd - start_pct) / (full_pct - start_pct))
    return max(MIN_DD_SCALAR, 1.0 - progress * (1.0 - MIN_DD_SCALAR))


def kelly_fraction(prob: float, payoff_r: float, fraction: float = 0.25) -> float:
    """Fractional Kelly on a calibrated probability.

    `payoff_r` is the win/loss ratio b. Only meaningful on a probability
    that has been validated out of sample -- see `meta_label.MetaLabeller`,
    which refuses to arm when it cannot beat a coin. Feeding this an
    uncalibrated score is worse than not using it.
    """
    p = max(0.0, min(1.0, float(prob)))
    b = float(payoff_r)
    if b <= 0:
        return 0.0
    edge = (p * (b + 1.0) - 1.0) / b
    if edge <= 0:
        return 0.0
    return min(MAX_KELLY, edge * max(0.0, float(fraction)))


def correlated_room(open_lots: float, cap_lots: float) -> Optional[float]:
    """Lots still available under the correlated-exposure cap.

    None when the cap is off (0.0). Not 0.0: a cap that silently meant
    "never trade again" is the kind of thing found the hard way.
    """
    if cap_lots <= 0:
        return None
    return max(0.0, round(float(cap_lots) - float(open_lots), 6))


def apply(base_lots: float, inputs: SizingInputs) -> SizingResult:
    """Compose every configured adjustment over a base lot size."""
    lots = float(base_lots)
    notes: list[str] = []

    vol = volatility_scalar(inputs.atr, inputs.reference_atr)
    if vol != 1.0:
        lots *= vol
        notes.append(f"volatility scalar {vol:.2f} (ATR {inputs.atr:.2f} vs "
                     f"reference {inputs.reference_atr:.2f})")

    dd = drawdown_scalar(inputs.drawdown_pct, inputs.dd_start_pct,
                         inputs.dd_full_pct)
    if dd != 1.0:
        lots *= dd
        notes.append(f"drawdown scalar {dd:.2f} at "
                     f"{inputs.drawdown_pct * 100:.1f}% drawdown")

    room = correlated_room(inputs.open_correlated_lots,
                           inputs.correlated_cap_lots)
    if room is not None and lots > room:
        lots = room
        notes.append(f"correlated exposure cap: {room:.2f} lots left of "
                     f"{inputs.correlated_cap_lots:.2f}")

    if lots <= 0:
        return SizingResult(0.0, notes)
    return SizingResult(max(MIN_LOT, round(lots, 2)), notes)
