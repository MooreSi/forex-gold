"""A candidate Set & Forget trade: its ratio, its refusals, and its cash.

The last gate before a setup reaches a button that can place it, so most of
this file is refusals. A setup that is merely wrong -- stop on the far side of
entry, target behind price, 1:0.8 written up as a plan -- renders as a tidy
card with plausible numbers and nothing on screen says no. `invalidations` is
what says no, and the page shows what it said.

**No money maths is done here.** `money_at_risk`, `money_at_target` and
`lot_from_risk` forward to `trading/fees_sizing`, which is this app's one P&L
function and its one sizing function. Golden rule 5 -- a second implementation
would be a second answer to "what is this trade worth", and the two would drift
without either looking wrong.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.trading import fees_sizing as _fees

# Alex G's floor. Below it the method's arithmetic stops working: the win rate
# that makes it profitable assumes the winners are twice the losers. 3:1 is
# what he prefers, and is not enforced -- a 2:1 at a good zone is a trade.
MIN_RR = 2.0
PREFERRED_RR = 3.0


def build(direction: str, entry: float, stop_loss: float, take_profit: float,
          *, order_type: str = "market") -> dict:
    """The candidate, with its distances and ratio measured.

    `rr` is None rather than infinite when entry and stop are the same price.
    That is a broken setup, not an infinitely good one, and `inf` would sort to
    the top of any ranking and render as the best trade on the page.
    """
    direction = str(direction or "").strip().upper()
    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    return {
        "direction": direction,
        "entry": float(entry),
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "order_type": order_type,
        "risk": risk,
        "reward": reward,
        "rr": round(reward / risk, 4) if risk > 0 else None,
    }


def order_type_for(direction: str, entry: float, price: float,
                   tolerance: float) -> str:
    """"market", "limit" or "stop" for an entry against the current price.

    "limit" is the set-and-forget entry: the order rests at the zone and waits
    for price to come back to it, which is the whole reason the method does not
    need anyone watching.

    "stop" is named rather than quietly placed as something else. A BUY above
    the market means entering as price runs AWAY from the zone, which is not
    part of this method -- `invalidations` refuses it, and it is better to
    refuse something with the right name on it.
    """
    direction = str(direction or "").strip().upper()
    if abs(price - entry) <= tolerance:
        return "market"
    if direction == "BUY":
        return "limit" if entry < price else "stop"
    if direction == "SELL":
        return "limit" if entry > price else "stop"
    return "market"


def invalidations(s: Optional[dict], min_rr: float = MIN_RR) -> list[str]:
    """Every reason this setup may not be traded, in the operator's words.

    A list rather than a first failure: a setup with an inverted stop AND a
    thin ratio has two things wrong with it, and fixing the one the function
    happened to mention first would just surface the other.

    `None` answers with an empty list. It is called on every read, including
    the ones where the rules refused before a candidate existed -- "there is no
    trade" is a different state from "there is a trade and it is broken", and
    answering it here rather than with an `if` at each call site is what lets
    the controller stay a forwarder.
    """
    if not s:
        return []
    out: list[str] = []
    direction = s.get("direction")
    entry = float(s.get("entry") or 0.0)
    stop = float(s.get("stop_loss") or 0.0)
    target = float(s.get("take_profit") or 0.0)

    if direction not in ("BUY", "SELL"):
        return [f"{direction!r} is not a direction. A setup is BUY or SELL."]

    if s.get("order_type") == "stop":
        out.append(
            "This entry is a stop order — it would buy above the market or "
            "sell below it, chasing price away from the zone. Set & Forget "
            "enters at the zone, not after it."
        )

    if direction == "BUY":
        if stop >= entry:
            out.append(f"A BUY's stop must be below its entry. "
                       f"Stop {stop:.2f} is at or above entry {entry:.2f}.")
        if target <= entry:
            out.append(f"A BUY's target must be above its entry. "
                       f"Target {target:.2f} is at or below entry {entry:.2f}.")
    else:
        if stop <= entry:
            out.append(f"A SELL's stop must be above its entry. "
                       f"Stop {stop:.2f} is at or below entry {entry:.2f}.")
        if target >= entry:
            out.append(f"A SELL's target must be below its entry. "
                       f"Target {target:.2f} is at or above entry {entry:.2f}.")

    # The 30m stage. Only a triggered setup may be placed; the other two are
    # shown so the plan is visible, and refused here so the button is disabled
    # with a reason. Added 2026-09-21 with the trigger stage -- before it, a
    # zone being chosen was the same thing as an order going out.
    stage = s.get("stage")
    if stage == "armed":
        out.append("Price has not reached the zone yet. Set & Forget enters "
                   "when price arrives and the 30m reacts — there is nothing "
                   "to place until it gets there.")
    elif stage == "waiting":
        out.append("Price is at the zone but the 30m has not reacted yet. "
                   "The trigger is a shift of structure or an engulfing close "
                   "on the 30-minute chart.")

    rr = s.get("rr")
    if rr is None:
        out.append("The entry and the stop are the same price, so there is no "
                   "risk to measure and no ratio to judge.")
    elif rr < min_rr:
        out.append(f"Reward-to-risk is 1:{rr:.2f}. Set & Forget does not take "
                   f"anything under 1:{min_rr:g}.")
    return out


def money_at_risk(s: dict, lots: Optional[float]) -> Optional[float]:
    """What a stop-out costs, as a positive amount.

    Positive on purpose: the box prints it under a red band, and a minus sign
    there reads as a loss on a loss. None -- not zero -- when no lot size has
    been chosen, because zero is a real answer meaning "this trade risks
    nothing" and not having chosen yet is a different state.
    """
    if not lots or lots <= 0:
        return None
    return abs(_fees.pnl(s["direction"], s["entry"], s["stop_loss"], lots))


def money_at_target(s: dict, lots: Optional[float]) -> Optional[float]:
    """What the target pays, as a positive amount. None when unsized."""
    if not lots or lots <= 0:
        return None
    return abs(_fees.pnl(s["direction"], s["entry"], s["take_profit"], lots))


def lot_from_risk(entry: float, stop_loss: float, balance: float,
                  risk_pct: float) -> Optional[float]:
    """The lot size for `risk_pct` of `balance` over this stop distance.

    Forwards to the shared sizer untouched. `suggest_lot_size` already applies
    Global Parameters > Max Risk per trade % on top of Risk per trade %; a
    local calculation here would silently drop that second ceiling for this one
    screen.

    A non-positive balance answers None. Better no number than a lot size
    computed against an account that could not be read -- that number would go
    into the box and then to a broker.
    """
    if balance is None or balance <= 0:
        return None
    return _fees.suggest_lot_size(entry, stop_loss, balance, risk_pct)
