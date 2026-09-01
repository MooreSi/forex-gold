"""Did the EA stop working, or did we just stop hearing from it?

bugs/013, step 3, option B.

The app infers EA health from silence -- `_HEARTBEAT_TIMEOUT_S = 8.0`, four
missed 2-second pings. That says the app has not HEARD from the EA. It does not
say the EA stopped managing the trade, and from the app's side the two are
indistinguishable. The bug's own "Not to do" section forbids acting on the
signal for exactly that reason, so every option that reduces exposure depends
on first knowing which of the two it is.

Measured over 30 days of rotated logs (2026-09-01): four bursts on 2026-08-31,
0-9 seconds each, five live tickets, and zero correlation with the app's own
event-loop stalls. Real, short, and not the app.

So the EA now reports what it actually did, and this turns that report into a
verdict. Three outcomes, not two:

    ran        the EA managed through the silence. Missed pings; no exposure.
    no_ticks   MT5 delivered no ticks, so OnTick never ran and there was
               nothing to manage against. Not a fault -- and a quiet market is
               when a stall looks most alarming and matters least.
    stalled    ticks arrived and the EA did not act on them. The real thing.

Plus two honest non-answers: `restarted` (a counter went backwards, so this is
a different EA, not a stalled one) and `unknown` (nothing to compare, or an EA
build that predates the extra fields).

Diagnostic only. Nothing here changes what is managed, sent or closed -- the
point is to establish whether there is any exposure before anything acts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

RAN = "ran"
STALLED = "stalled"
NO_TICKS = "no_ticks"
RESTARTED = "restarted"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Verdict:
    outcome: str
    ticks: int = 0
    passes: int = 0
    summary: str = ""


_last: Optional[tuple[int, int]] = None


def _counters(ping) -> Optional[tuple[int, int]]:
    """(ticks, passes) from a ping, or None if this EA does not report them.

    Never raises: this runs on the receive path for every message the EA
    sends, and a malformed one must cost the diagnostic, not the link.
    """
    try:
        return int(ping["ticks"]), int(ping["passes"])
    except (KeyError, TypeError, ValueError):
        return None


def record(ping) -> None:
    """Remember the counters from a ping, for the next comparison."""
    global _last
    counters = _counters(ping)
    if counters is not None:
        _last = counters


def verdict_after_silence(ping) -> Verdict:
    """What the EA was doing while the app could not hear it.

    Compares against the last recorded ping, so it is meaningful only when
    called on the first message AFTER a silence. It does not record; the
    caller decides whether this ping becomes the new baseline.
    """
    before = _last
    after = _counters(ping)

    if before is None or after is None:
        return Verdict(UNKNOWN, summary=(
            "no verdict: this EA build does not report what it managed, or "
            "there is nothing to compare against yet"))

    d_ticks = after[0] - before[0]
    d_passes = after[1] - before[1]

    if d_ticks < 0 or d_passes < 0:
        # Both counters reset with the terminal or the EA. Backwards means a
        # NEW EA, and calling that a stall would log a fault every time the
        # chart is reloaded.
        return Verdict(RESTARTED, summary=(
            "the EA restarted during the gap -- its counters went backwards, "
            "so this is a new EA rather than a stalled one"))

    if d_passes > 0:
        return Verdict(RAN, d_ticks, d_passes, summary=(
            f"the EA kept managing through the silence "
            f"({d_passes} management passes over {d_ticks} ticks) -- missed "
            f"pings, not a management outage"))

    if d_ticks == 0:
        return Verdict(NO_TICKS, d_ticks, d_passes, summary=(
            "no ticks arrived during the gap, so the EA was never asked to "
            "manage anything -- a quiet market, not a fault"))

    return Verdict(STALLED, d_ticks, d_passes, summary=(
        f"the EA did not manage anything across {d_ticks} ticks -- a real "
        f"management outage for the length of the gap"))


def reset() -> None:
    """Forget the baseline. For tests, and after a deliberate reconnect."""
    global _last
    _last = None
