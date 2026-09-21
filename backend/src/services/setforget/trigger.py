"""The 30-minute trigger: WHEN to enter, once the higher timeframes have said
where and which way.

This stage did not exist until 2026-09-21, and its absence is what put a
resting order days of travel from price. The Weekly and Daily pick the zone,
the 4H agrees or does not -- and then nothing happened, because there was no
third stage to wait for. So the app rested an order at the zone and waited for
the market to come to it, which on a level a fortnight away is not a trade.

Alex G's guide puts the entry on the lowest of three timeframes ("a 4:1 or 8:1
ratio ... e.g. Daily, 4H, 30-min"), and the community's Perfect Checklist gives
a 25% group to "2H, 1H, 30m" holding Shift of Structure (10%), Engulfing
Pattern (10%) and Round Psychological Level (5%). This module is that group.

**Two gates, and both are required.** Price must have ARRIVED at the zone, and
the 30m must then have REACTED. Either alone is the failure being replaced:
arrival alone is an order resting in front of a market that has not got there,
and a reaction anywhere on the chart is a trade with no level behind it.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.setforget import aoi, patterns, structure

# The round numbers gold actually respects. Fifties and hundreds hold without
# any structure behind them, because that is where people put orders.
ROUND_STEP = 50.0


def has_arrived(zone: Optional[dict], price: float, tolerance: float) -> bool:
    """Whether price is at the zone, within the caller's tolerance.

    The gate the app was missing. `tolerance` is the caller's because what
    counts as "at" a hand-drawn level depends on how far the instrument moves
    in a bar, not on anything this module knows.
    """
    if not zone:
        return False
    return aoi.distance(zone, price) <= tolerance


def evaluate(candles: list[dict], direction: str) -> Optional[dict]:
    """What the 30m has done, or None if it has not done anything yet.

    A shift of structure outranks an engulfing when both are present. They are
    10% each on the checklist, but a shift is a statement about structure and
    an engulfing is one bar -- reporting the weaker would understate the chart.

    None is the common case and it has to stay quiet. A trigger stage that
    fires on arrival is the stage it replaces wearing a new name.
    """
    direction = str(direction or "").strip().upper()
    if not candles:
        return None

    shift = structure.shift_of_structure(candles, direction)
    if shift:
        return {"kind": "shift_of_structure", "ts": shift["ts"],
                "level": shift["level"], "direction": direction}

    found = patterns.confirmation(candles)
    if found and found.get("kind") == "engulfing":
        want = "bullish" if direction == "BUY" else "bearish"
        if found.get("direction") == want:
            return {"kind": "engulfing", "ts": found["ts"],
                    "level": found["low"] if direction == "BUY" else found["high"],
                    "direction": direction}
    return None


def round_level(price: float, tolerance: float) -> Optional[float]:
    """The round psychological level price is sitting at, or None.

    The checklist's 5% item. Not a reason to trade on its own -- which is why
    it is the lightest thing in its group -- but a level that holds without
    structure behind it is worth knowing about when deciding whether the one
    with structure will.
    """
    if price <= 0 or tolerance < 0:
        return None
    nearest = round(price / ROUND_STEP) * ROUND_STEP
    return nearest if abs(price - nearest) <= tolerance else None
