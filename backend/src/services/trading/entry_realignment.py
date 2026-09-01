"""Shift a breached signal's stop and targets to the price it is entered at.

Owner decisions, 2026-09-01 (docs/simon-handover/009):

  * a breached zone still **discards** by default — nothing changes for anyone
    who has not switched Entry Realignment on;
  * and realignment should exist on the **market** path too, gated on the same
    setting, because it previously lived only in the limit-order path and the
    same situation was therefore handled two different ways depending on which
    route a signal took.

**What a breach is here.** Price has moved through the zone *toward the stop*
before any entry existed — a BUY that has fallen below its zone, or a SELL that
has risen above it. Entering flat at that price would leave a smaller stop than
the channel specified, which is a materially different trade. It is not "price
ran away to somewhere better"; that case is not a breach and is not realigned.

**What realignment does.** Moves the stop and every target by the same
distance, so the trade keeps the shape it was sent with, at a worse price.

The case this was built from, 2026-08-28:

    SELL  entry 4537.00-4539.00  SL 4544.00  TP1 4535.00
    price 4540.45  ->  SL 4545.45, TP1 4536.45

5.00 of stop and 5.00 to TP1, exactly as sent, measured from 4540.45.

Pure arithmetic: no broker, no database, no settings lookup. The caller decides
whether realignment is switched on; this decides what the numbers would be, and
returns None whenever they would not be safe to trade.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RealignedEntry:
    entry_px: float
    stop_loss: float
    tps: dict
    delta: float


def realign_for_breach(*, direction: str, entry_low: float, entry_high: float,
                       live_px: float, stop_loss: float,
                       tps: dict) -> Optional[RealignedEntry]:
    """The realigned levels, or None if this is not a breach worth entering.

    None means "do not realign" and the caller keeps its existing behaviour,
    which is to discard. Every None case is deliberate:

      * not a breach at all (in the zone, or moved the favourable way)
      * exactly on the zone edge -- `price_in_entry_range` counts that as IN
        the zone, and the two must not disagree about the same price
      * the realigned stop would sit on the wrong side of the entry, or on top
        of it. That is not a wide stop, it is an immediate close, and no trade
        is better than that trade.
    """
    d = (direction or "").upper()
    if d == "BUY":
        edge = entry_low
        breached = live_px < edge
    elif d == "SELL":
        edge = entry_high
        breached = live_px > edge
    else:
        return None

    if not breached:
        return None

    delta = live_px - edge
    new_sl = round(stop_loss + delta, 2)
    # A 0 or None target is "not set". Shifting it would turn it into a real
    # price near zero, which the EA would take as a genuine target.
    new_tps = {n: round(v + delta, 2) for n, v in (tps or {}).items() if v}

    risk = (live_px - new_sl) if d == "BUY" else (new_sl - live_px)
    if risk <= 0:
        log.warning(
            "[realign] refusing %s: realigned stop %.2f is not on the losing "
            "side of entry %.2f -- the original stop (%.2f) was already wrong "
            "for this direction",
            d, new_sl, live_px, stop_loss,
        )
        return None

    return RealignedEntry(entry_px=live_px, stop_loss=new_sl, tps=new_tps,
                          delta=delta)
