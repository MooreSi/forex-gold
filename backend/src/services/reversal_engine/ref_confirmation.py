"""REF confirmation gate for Reversal Engine live execution (2026-07-31).

Only trade a signal when the professional channels have just posted a
matching entry of their own.

Measured on 582 closed signals (after core_ref_signal_backfill recovered the
REF rows that were missing), splitting purely on whether a REF signal with
the same direction and an entry midpoint within _PRICE_DELTA had been posted
in the window *before* this signal filled -- i.e. only information the engine
could actually have had at decision time, no lookahead:

    window     n     total    $/trade   win rate
    15 min    60   + 119.76    +2.00      78%
    30 min    81   +   3.52    +0.04      74%
    60 min   107   +  64.96    +0.61      74%
    120 min  135   - 874.91    -6.48      67%
    every signal 582  -2595.71  -4.46      70%

So the confirmation decays fast: inside an hour it is worth something, by two
hours a same-price match is coincidence and the subset is no better than the
whole. That shape is why the window is a setting rather than a constant.

DEFAULT OFF. The positive buckets are thin (n=60 at 15 min, ~10% of all
signals) and their profit is concentrated in the more recent half of the
sample (first half -48.92, second half +168.68), which is as consistent with
a favourable recent regime as with a real edge. Removing the best and worst
trade *improves* the result, so it is not outlier-driven, but n is too small
to call this proven.

Gates live execution only. The signal is still created, tracked virtually and
fed to the ML either way, so turning this on does not blind the learner and
leaves the counterfactual measurable.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from backend.src.db import database as core_db
from backend.src.services.reversal_engine import reversal_engine_repo

log = logging.getLogger(__name__)

# Entry zones are 3-4 pts wide, so 3.0 pts between midpoints is "the same
# level" rather than "a nearby level". Matches reversal_engine_correlate's
# own tolerance.
_PRICE_DELTA = 3.0
_DEFAULT_WINDOW_MIN = 60

# Which channels count as confirmation is read from channel_parser_config
# rather than hardcoded (reversal_engine_correlate still pins two group IDs
# as literals, which silently ignores any channel added since). Enabled
# channels only: a channel switched off in Parsing Settings shouldn't be
# able to greenlight a live trade.
_ENABLED_CHANNELS_SQL = (
    "SELECT channel_name FROM channel_parser_config WHERE enabled=1"
)


def confirmation_window_s(rs: dict) -> int:
    try:
        mins = int(rs.get("re_ref_confirmation_window_min", _DEFAULT_WINDOW_MIN))
    except (TypeError, ValueError):
        mins = _DEFAULT_WINDOW_MIN
    return max(60, mins * 60)


def is_required(rs: dict) -> bool:
    return bool(rs.get("re_require_ref_confirmation", 0))


def find_ref_confirmation(direction: str, entry_mid: float, rs: dict,
                          at_ts: Optional[float] = None) -> Optional[dict]:
    """The most recent REF signal confirming this one, or None.

    `at_ts` is the moment being decided for (the fill). Only REF signals
    posted at or before it are considered -- a later one is not information
    the engine had, and counting it would make any backtest built on this
    function silently optimistic."""
    if not direction or not entry_mid:
        return None
    now = at_ts or time.time()
    window = confirmation_window_s(rs)
    try:
        row = reversal_engine_repo.find_confirming_signal(
            direction, float(entry_mid), now - window, now, _PRICE_DELTA)
    except Exception as exc:
        log.warning("[RefConfirm] lookup failed: %s", exc)
        return None
    return core_db.row_to_dict(row) if row else None


def check(direction: str, entry_low, entry_high, rs: dict,
          at_ts: Optional[float] = None) -> tuple[bool, str]:
    """(allowed, reason). Always allows when the gate is off, so callers can
    invoke it unconditionally."""
    if not is_required(rs):
        return True, ""
    try:
        entry_mid = (float(entry_low) + float(entry_high)) / 2.0
    except (TypeError, ValueError):
        return False, "REF confirmation required but this signal has no entry zone"

    hit = find_ref_confirmation(direction, entry_mid, rs, at_ts)
    window_min = confirmation_window_s(rs) // 60
    if not hit:
        return False, (f"no matching {direction} signal from the reference channels "
                       f"within {window_min}min at {entry_mid:.2f} +/-{_PRICE_DELTA:g}pts")
    age_min = max(0.0, ((at_ts or time.time()) - float(hit["parsed_at"]))) / 60.0
    return True, (f"confirmed by {hit.get('group_name')} {hit.get('direction')} "
                  f"{age_min:.0f}min ago")
