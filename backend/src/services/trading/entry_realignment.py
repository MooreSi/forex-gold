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

**The pip limit (owner, 2026-10-02).** `lk_entry_realignment_max_pips` caps how
far any realignment may chase, in both directions: price that MISSED the zone
(a BUY above it, a SELL below) and price that went THROUGH it toward the stop.
Missed by more waits for the zone; breached by more is discarded. Blank (0) is
no limit and changes nothing: every route behaves exactly as it did before the
field existed. When a limit is set it is the only cap, so Immediate Market
Entry's fixed gap-fire cap applies only when no limit is set.

Pure arithmetic: no broker, no database, no settings lookup. The caller decides
whether realignment is switched on; this decides what the numbers would be, and
returns None whenever they would not be safe to trade.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RealignedEntry:
    entry_px: float
    stop_loss: float
    tps: dict
    delta: float


def realign_cap_pts(rs: dict) -> float:
    """The pip limit in price points, or 0.0 for "no limit".

    0.0 whenever realignment itself is off, so a stored limit cannot act while
    the switch it belongs to is off. Blank, None and non-numeric all read as no
    limit: the field refusing to parse must not silently become a tiny cap.
    """
    if not bool(rs.get("lk_entry_realignment", 0)):
        return 0.0
    try:
        pips = float(rs.get("lk_entry_realignment_max_pips") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return round(pips * PIPS_TO_PRICE_XAUUSD, 6) if pips > 0 else 0.0


def gap_fire_cap_pts(rs: dict, *, ime_on: bool, ime_cap: float) -> float:
    """How far past its zone a signal may be chased into a market entry.

    A set pip limit is the only cap and applies whether or not IME is on. With
    no limit, IME keeps its own fixed cap and nothing fires without IME, which
    is the behaviour before the limit existed.
    """
    limit = realign_cap_pts(rs)
    if limit > 0:
        return limit
    return float(ime_cap) if ime_on else 0.0


def realign_missed(parsed: dict, live_px: float,
                   cap_pts: float) -> Optional[tuple[dict, float]]:
    """`(shifted signal, gap)` when price MISSED the zone within `cap_pts`.

    Missed is price having run away the favourable-looking way: a BUY above its
    zone top, a SELL below its zone floor. The entry zone, stop and every
    target move by the gap, so the trade keeps the shape it was sent with from
    where it can actually be entered. None when price is in the zone, missed
    by more than the cap, or there is no cap. The input is not mutated, and an
    unset target stays unset rather than becoming a price near zero.
    """
    if cap_pts <= 0:
        return None
    d = str(parsed.get("direction") or "").upper()
    el = float(parsed["entry_low"])
    eh = float(parsed["entry_high"])
    if d == "BUY":
        gap = round(live_px - eh, 2)
        sign = 1.0
    elif d == "SELL":
        gap = round(el - live_px, 2)
        sign = -1.0
    else:
        return None
    if gap <= 0 or gap > cap_pts:
        return None
    out = dict(parsed)
    out["entry_low"] = round(el + sign * gap, 2)
    out["entry_high"] = round(eh + sign * gap, 2)
    out["stop_loss"] = round(float(parsed["stop_loss"]) + sign * gap, 2)
    for n in range(1, 9):
        v = parsed.get(f"tp{n}")
        if v is not None:
            out[f"tp{n}"] = round(float(v) + sign * gap, 2)
    return out, gap


def realign_grid_zone(parsed: dict, live_px: float, rs: dict,
                      source_label: str = "") -> Optional[tuple[dict, float, float, str]]:
    """`(shifted signal, entry_low, entry_high, note)` for a grid template.

    A grid stages resting legs across the signal's zone, so a zone price has
    run away from is shifted whole, putting the first leg at market. Only when
    a pip limit is set: IME has never shifted a grid, and a blank limit must
    not start now. None means place the grid at the signalled zone, as before.
    """
    out = realign_missed(parsed, live_px, realign_cap_pts(rs))
    if out is None:
        return None
    shifted, gap = out
    el, eh = float(shifted["entry_low"]), float(shifted["entry_high"])
    log.info("[%s] Entry Realignment: grid zone shifted %.2f pts to %.2f–%.2f (market %.2f)",
             source_label, gap, el, eh, live_px)
    return shifted, el, eh, (f"Grid zone realigned +{gap:.1f}pt — market at "
                             f"{live_px:.2f}. Levels shifted to match.")


def realign_for_breach(*, direction: str, entry_low: float, entry_high: float,
                       live_px: float, stop_loss: float,
                       tps: dict, max_pts: float = 0.0) -> Optional[RealignedEntry]:
    """The realigned levels, or None if this is not a breach worth entering.

    None means "do not realign" and the caller keeps its existing behaviour,
    which is to discard. Every None case is deliberate:

      * not a breach at all (in the zone, or moved the favourable way)
      * exactly on the zone edge -- `price_in_entry_range` counts that as IN
        the zone, and the two must not disagree about the same price
      * the breach is deeper than `max_pts` (0 = no limit, the old behaviour)
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
    if max_pts > 0 and abs(round(delta, 2)) > max_pts:
        return None
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
