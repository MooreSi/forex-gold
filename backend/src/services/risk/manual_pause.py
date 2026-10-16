"""Pausing trading by hand, and lifting the pause again. One definition.

`trade_pause_until` was written directly by three surfaces: the Telegram
`/pause` command, the bot panel's end-of-session pause, and the NiceGUI header
dialog. They agreed about pausing and **disagreed about resuming** — `/resume`
called `rearm_risk_guards()` and the dashboard's Resume button did not.

That difference is not cosmetic. Both post-close guards halt for the rest of
the broker day, so resuming after a give-back halt without re-arming is a
no-op: the day's peak is already spent and the guard re-trips on the very next
close. The operator presses Resume, watches trading resume, and it stops again
on the first trade that closes -- with the button looking broken and the reason
invisible.

So the pair lives here and every surface calls it.

**Pausing is the safe direction.** It stops new orders and changes nothing
else: signal generators and the Telegram reader keep running, and active trade
management (SL/TP monitoring) continues exactly as before. Resuming is the
direction that lets money move again, which is why it does MORE than clear a
flag rather than less.

Nothing here places or closes anything.
"""
from __future__ import annotations

import logging
import time

from backend.src.db import database as db_module
from backend.src.services.risk import governor as _governor

log = logging.getLogger(__name__)

__all__ = ["pause", "pause_until", "resume", "resume_past_goal", "state",
           "PAUSE_KEY", "GOAL_KEPT"]

PAUSE_KEY = "trade_pause_until"


def pause_until(timestamp: float) -> float:
    """Halt new orders until `timestamp` (unix seconds). Returns it.

    A moment in the past is refused rather than written: it would store a pause
    that has already expired, so trading would not stop and the screen would
    say it had.
    """
    ts = float(timestamp)
    if ts <= time.time():
        raise ValueError("The pause has to end in the future.")
    db_module.set_app_config(PAUSE_KEY, str(ts))
    log.info("[pause] trading paused until %s",
             time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)))
    return ts


def _pause_for(hours: float) -> float:
    """Halt new orders for `hours` from now. Returns when it will lift.

    Fractions are the point: 0.25 is fifteen minutes, which is what stepping
    away over a news release actually needs.
    """
    return pause_until(time.time() + float(hours) * 3600.0)


def pause(hours: float | None = None, until: float | None = None) -> float:
    """Halt new orders. A given moment wins over a duration.

    The choice lives here rather than in a router or a controller because it
    has a rule: a dialog offers both boxes, and an empty hours field must never
    become 0 -- that is a pause already in the past, which would leave trading
    running while the screen said it had stopped. Four hours is the default the
    dialog has always shown.
    """
    if until is not None:
        return pause_until(until)
    return _pause_for(4.0 if hours is None else hours)


def resume() -> str | None:
    """Lift the pause, and re-arm the guards that would otherwise undo it.

    Returns None when lifted. A secured (or held) daily goal is the one halt
    this does NOT lift while the goal is switched on: it returns `GOAL_KEPT`,
    the sentence every Resume surface shows, and changes nothing (owner, 2026-10-10: on
    9 October a Resume undid a goal secured at 05:46 and the day traded on to
    -$39.28). Switching the goal off, or raising it, is how to trade on.

    The re-arm is not optional. See the module docstring: without it, resuming
    after a give-back halt lasts until the next close.

    The flag is cleared FIRST and a failing re-arm is swallowed. That is
    fail-safe in the direction of what the operator asked for: they pressed
    Resume, and a guard that could not be re-armed must not leave trading
    halted with no explanation. The guard is still armed either way — it has
    simply not had its window reset.
    """
    from backend.src.services.risk import daily_goal as _goal
    kept = _goal.keeps_its_halt()
    if kept:
        log.warning("[pause] Resume left the daily goal's halt in place: %s", kept)
        return GOAL_KEPT
    db_module.set_app_config(PAUSE_KEY, "0")
    try:
        _governor.rearm_risk_guards()
    except Exception as exc:
        log.warning("[pause] resumed, but the post-close guards could not be "
                    "re-armed (%s) — a give-back halt may re-trip on the next "
                    "close", exc)
    return None


def resume_past_goal() -> None:
    """"Resume anyway (ignore today's goal)": `resume`, lifting the goal's own
    halt too (owner, 2026-10-16).

    Only when that halt is the goal's does it also stop the goal halting again
    until the day ends (`daily_goal.override_for_today`), which is also what
    lets `resume` past `keeps_its_halt` below. Over another guard's
    halt it is a plain Resume and sets nothing, so a goal reached later today
    still halts. Plain `resume` is unchanged: the 2026-10-10 rule stands for
    every surface that has not asked for this by name.
    """
    from backend.src.services.risk import daily_goal as _goal
    if _goal.keeps_its_halt():
        _goal.override_for_today()
        log.warning("[pause] Resume anyway: the daily goal's halt lifted for today")
    resume()


# What every Resume surface says when the goal's halt stays.
GOAL_KEPT = ("Today's daily goal is reached, so trading stays paused until "
             "midnight. To trade on today, raise the goal or switch it off in "
             "Risk settings.")


def state() -> dict:
    """`{paused, until}` for a screen deciding whether to offer Pause or Resume.

    **Fails OPEN, deliberately** — the opposite of `governor.is_trading_paused()`,
    which gates orders and reports PAUSED when it cannot read. This one only
    chooses which button to show: offering Resume on a machine that is not
    paused is harmless, and refusing to offer Pause because a read blipped is
    not.
    """
    try:
        until = float(db_module.get_app_config(PAUSE_KEY) or 0)
    except (TypeError, ValueError):
        return {"paused": False, "until": None}
    if until <= time.time():
        return {"paused": False, "until": None}
    return {"paused": True, "until": until}
