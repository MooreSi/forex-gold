"""The pending-signal watcher notices its own absence, and distrusts what it
finds when it comes back.

Owner, 2026-09-11.

**The hole.** While trading is paused -- a circuit breaker, the daily-loss
halt, a manual pause from the dashboard or the bot -- monitor_cycle skips
`try_activate_pending_signals` entirely (`if _pending_watch and not
is_trading_paused`). For the whole halt, nothing in the queue ages, expires,
or is re-checked against anything. Signals keep arriving and keep being
queued: a Telegram signal whose price is outside its zone is inserted as
'pending' by the scan path, and one whose price is INSIDE its zone tries to
open, is refused by `open_trade`'s pause gate, and is put back to 'pending'
by scan_auto_execute's failure handler. Both land in the same queue.

The moment the pause lifts, the next cycle runs the entire backlog in one
pass. The expiry ladder culls the short-window ones (180s by default), but a
signal on an EA template gets an hour and one on a runner strategy gets four,
so a halt of any realistic length ends with hour-old setups executing on the
first tick that touches their zone. Every gate they then face is
instantaneous -- is it news RIGHT NOW, is the bias aligned RIGHT NOW -- and
none of them knows the app was blind for the last fifty minutes.

**The same hole opens without a pause.** The watcher also does not run when
auto-execute is toggled off (`_pending_watch` is that setting), or when the
app is not running at all. The 2026-07-03 incident behind the scan path's
4-minute staleness guard was exactly this shape: a toggle-off gap, then a
backfilled 22-minute-old signal filling at market and going straight to its
stop. So the trigger here is not "was there a pause" but "was I away",
which covers pause, toggle and restart with one mechanism and cannot be
slipped past by a fourth reason to skip a cycle that nobody has invented
yet.

**Why the last-run time is in the database.** Module state does not survive
a restart, and a restart is one of the gaps this exists to catch. The value
is stamped no more than once every `_STAMP_EVERY_S`, so the watcher's 1s
cadence does not mean a database write every second; the measured gap is
therefore up to that much LONGER than the true one. That direction is the
safe one -- it can only make the watcher more suspicious -- and it is why
`GAP_THRESHOLD_S` is comfortably above both numbers rather than close to the
5s idle poll.

A fresh install has no stamp at all. That reports no gap: an absent record
says nothing about how long the watcher was away, and treating it as a
gap would make every first run distrust its own queue.

**What the mark does is decided elsewhere.** This module answers one
question -- "was this signal in the queue across a blind gap" -- and holds no
opinion about what should then happen to it. See
pending_activation.try_activate_pending_signals for the re-validation
itself. Nothing here reads a price, places an order, or touches a broker.
"""
from __future__ import annotations

import logging
from typing import Iterable

from backend.src.db import database as db_module

log = logging.getLogger(__name__)

# How long the watcher must have been away before its queue is treated as
# unexamined. The watcher's own cadence is 1s (a trade open or a signal
# waiting) to 5s (idle), and the stamp below lags reality by up to
# _STAMP_EVERY_S, so this has to clear 5 + _STAMP_EVERY_S with room to spare
# or an ordinary idle cycle would read as an outage. A real gap of roughly
# 45s and up trips it; every pause, toggle-off and restart worth the name is
# far longer than that.
GAP_THRESHOLD_S = 60.0

# Stamping every cycle would be a database write per second for the life of
# the app, for a value only ever read at second granularity.
_STAMP_EVERY_S = 15.0

_LAST_RUN_KEY = "pending_watcher_last_run"

# signal_ids that were in the queue when a gap was detected, and have not
# been resolved since. Module state rather than a column: a restart re-runs
# the detection anyway and re-marks whatever is still queued, so persisting
# it would buy nothing. Same shape as pending_activation._ACTIVATION_FAILURES.
_STALE: set[str] = set()


def _read_last_run() -> float:
    try:
        return float(db_module.get_app_config(_LAST_RUN_KEY) or 0)
    except (TypeError, ValueError):
        # A corrupt value is not evidence of a gap, and it is not evidence
        # against one either. Treat it as no record and re-stamp below.
        return 0.0
    except Exception:
        log.debug("[GapRevalidation] could not read %s", _LAST_RUN_KEY,
                  exc_info=True)
        return 0.0


def _stamp(now: float) -> None:
    try:
        db_module.set_app_config(_LAST_RUN_KEY, str(now))
    except Exception:
        # Failing to record the time is not a reason to refuse to trade; the
        # worst case is that the next cycle reads an older stamp and is more
        # suspicious than it needed to be.
        log.debug("[GapRevalidation] could not stamp %s", _LAST_RUN_KEY,
                  exc_info=True)


def observe_watcher_run(now: float, queued_signal_ids: Iterable[str]) -> float:
    """Record that the watcher is running, and return the length of the blind
    gap that preceded this run (0.0 when there wasn't one).

    Every signal in `queued_signal_ids` at the moment a gap is detected is
    marked as needing re-validation -- the ones that predate the gap and the
    ones that arrived during it alike, since neither was ever examined
    against a market the app could see.
    """
    last = _read_last_run()
    gap = 0.0

    if last > 0 and now - last > GAP_THRESHOLD_S:
        gap = now - last
        ids = [s for s in queued_signal_ids if s]
        if ids:
            _STALE.update(ids)
            log.warning(
                "[GapRevalidation] Pending watcher was away %.0fs — %d queued "
                "signal(s) must re-validate against the current market before "
                "they can execute: %s",
                gap, len(ids), ", ".join(s[:8] for s in ids),
            )
        else:
            log.info("[GapRevalidation] Pending watcher was away %.0fs — "
                     "queue empty, nothing to re-validate", gap)

    if last <= 0 or now - last >= _STAMP_EVERY_S:
        _stamp(now)

    return gap


def needs_revalidation(signal_id: str) -> bool:
    """True when this signal sat in the queue across a blind gap and has not
    since proved itself against the live market."""
    return signal_id in _STALE


def clear(signal_id: str) -> None:
    """Drop the mark -- the signal has activated, expired or been abandoned."""
    _STALE.discard(signal_id)
