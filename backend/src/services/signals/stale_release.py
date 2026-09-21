"""Three guards for the moment a backlog of queued signals is released.

Owner, 2026-09-21, after a resume from the daily-loss halt opened two buys
and a sell on XAUUSD inside 700ms, all within half a point of each other.

WHY THIS IS NOT PART OF THE EXISTING RE-VALIDATION
--------------------------------------------------
`gap_revalidation.py` already notices that the watcher was away and makes
every signal still queued re-prove itself. It ran, correctly, on that resume
(3745s away, 5 signals marked). What it re-asks is the set of instantaneous
gates -- schedule, news blackout, fill delay, R:R and the directional cap with
the template/runner/IME bypasses suspended, and the HTF bias inside
`resolve_open_trade_params`. Each is a question about NOW.

None of them is a question about the SPREAD of the release: how far past its
own zone a given fill has drifted, and whether the queue is about to be
emptied in both directions at once. That is what this module answers, and it
is why these are separate switches rather than more conditions inside the
existing mark.

WHY EVERY ONE OF THEM IS OFF BY DEFAULT
---------------------------------------
rules/60-adding-a-tunable, and the owner's own choice on 2026-09-21: nothing
trades differently until a dial is moved. All three can only ever REFUSE a
trade the app would otherwise take, so switching one on is a live behaviour
change and belongs in a demo session, not in an upgrade.

PURE ON PURPOSE
---------------
Everything each function needs is an argument, including the settings dict.
No database read, no clock, no bridge, no price fetch. Same reason
`contradiction.py` is pure: these decide about order placement, so every
branch has to be a sentence a test can assert, and a future caller must not
be able to change the answer by changing what it reads.

Nothing here places, cancels or modifies an order. It returns a reason string
or None; `pending_activation` does the refusing.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

# The shipped better-fill cap, deliberately equal to the chase-side cap in
# scan_auto_execute. They measure the same quantity -- how far the live price
# has drifted from the zone the signal was written around -- in opposite
# directions, and two caps on one quantity that disagree is how the entry
# paths drift apart. Imported lazily in the accessor rather than at module
# level: this module must stay free of the trading package, which imports
# half the broker layer.
BETTER_FILL_CAP_PTS_DEFAULT = 15.0


def _on(rs: dict, key: str) -> bool:
    """Same reading as risk/capability_gates._on -- one house rule for what
    an absent or falsey toggle means."""
    return bool(rs.get(key, 0))


def _num(rs: dict, key: str, default: float) -> float:
    try:
        v = rs.get(key)
        return default if v is None else float(v)
    except (TypeError, ValueError):
        # A settings row can hold anything. Raising here would abort the
        # whole release pass -- a worse failure than the one the cap exists
        # to prevent.
        return default


# ── 1. How far past its own zone a fill has drifted ──────────────────────────

def better_fill_excess_pts(direction: str, entry_low: float,
                           entry_high: float, tick: Any) -> float:
    """Points by which the live price is BEYOND the entry zone on the
    favourable side, or 0.0 when it is inside the zone or on the other side.

    `governor.price_in_entry_range` treats any distance on this side as an
    "equal or better fill" and admits it without limit. For a signal taken
    the moment it arrives that is fair: the price genuinely is better than
    the one asked for. For a signal that has been queued for 45 minutes it
    is not a better entry into the same setup, it is a different setup --
    the 2026-09-21 SELL filled 7.17 points above a zone that topped out at
    4354.65, which put the live price on the wrong side of the signal's own
    stop.

    The unfavourable side deliberately reports 0.0 rather than a negative
    number: `price_in_entry_range` already refuses it outright, and a
    negative here would read as a cap that can never trip.
    """
    if direction.upper() == "BUY":
        return round(max(0.0, entry_low - tick.ask), 2)
    return round(max(0.0, tick.bid - entry_high), 2)


def better_fill_blocked(rs: dict, direction: str, entry_low: float,
                        entry_high: float, tick: Any) -> Optional[str]:
    """Why this fill is too far past its own zone to still be that signal,
    or None. Off unless `stale_better_fill_cap_enabled`.

    The boundary matches `scan_auto_execute.MAX_GAP_FIRE_PTS`' own
    (`0 < gap <= MAX_GAP_FIRE_PTS`): exactly at the cap is allowed.
    """
    if not _on(rs, "stale_better_fill_cap_enabled"):
        return None
    cap = _num(rs, "stale_better_fill_cap_pts", BETTER_FILL_CAP_PTS_DEFAULT)
    excess = better_fill_excess_pts(direction, entry_low, entry_high, tick)
    if excess <= cap:
        return None
    return (f"price is {excess:.2f} pts past the far side of its own entry "
            f"zone — beyond the {cap:.1f} pt better-fill cap")


# ── 2. Opposing directions inside one release ────────────────────────────────

def burst_hedge_blocked(rs: dict, direction: str,
                        released: Iterable[str]) -> Optional[str]:
    """Why this signal must not be released alongside the ones already
    released in this pass, or None. Off unless `burst_hedge_guard_enabled`.

    SCOPE, deliberately narrow: `released` is what THIS pass has opened, not
    what is already on the book. A pause ending is the case where a queue
    that built up over an hour is emptied against a single tick, and the
    signals in it were written minutes and a very different market apart.
    Positions already open are a different question with a different answer
    -- `positions/core_internal_exposure_guard.py` owns it, and owns it OFF
    on measured evidence (opposing legs were 19% of the Reversal Engine's
    closed trades and ~80% of its profit). Widening this to the book would
    quietly re-fight that decision for every source at once.

    Same side is always allowed: two buys released together are one view
    expressed twice, and `max_open_trades` already caps how many.
    """
    if not _on(rs, "burst_hedge_guard_enabled"):
        return None
    want = direction.upper()
    opposing = [d for d in released if d.upper() != want]
    if not opposing:
        return None
    return (f"{opposing[0].upper()} was already released in this pass — "
            f"the backlog would open both sides against one tick")


# ── 3. The momentum gate's switch ────────────────────────────────────────────

def momentum_gate_enabled(rs: dict) -> bool:
    """Whether the pending watcher's M5 momentum check should be fed.

    The check itself is long-standing (`pending_activation`, "momentum
    mismatch"): it defers a signal whose direction disagrees with the last
    completed M5 candle. It reads `dpm_candles`, and `monitor_cycle` only
    ever filled that cache when `dpm_enabled` -- so on an install with
    Dynamic Profit Management off, the list is permanently empty and the
    check has never run at all. On 2026-09-21 that is what let a BUY and a
    SELL through on the same tick.

    Deliberately NOT `dpm_enabled or ...`. This switch exists to reach the
    check WITHOUT turning DPM on; folding that setting in here would make it
    a no-op for the one configuration it was added for. `monitor_cycle`
    keeps honouring `dpm_enabled` on its own, separately, and feeds the
    watcher from a local list rather than the shared cache so that turning
    this on cannot change what the Auto strategy picker, the trailing ADX or
    the resting-order sweep are handed.
    """
    return _on(rs, "pending_momentum_gate_enabled")
