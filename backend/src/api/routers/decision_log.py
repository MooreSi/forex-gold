"""The Signal Decision Log: what the app decided, and what four OFF gates would.

`docs/todo/signal-validation/010`. Recording only — **nothing here changes a
trade, and nothing here places one.** The switch that turns the recording on is
an ordinary parsing toggle (`tg_decision_log_enabled`, default OFF); this is
what it is for.

Its own router rather than three more handlers on `parsing.py`, for the same
reason it was its own module under NiceGUI: that surface is the parsing toggles
and the keyword editor, and this is neither.

**Three readouts, because they become useful at different times.** `summary` is
available from the first decision — how many signals each path executed and
declined, and what did the declining. `report` needs trades that have closed,
so it says nothing for days and is the reason the whole thing exists.

**All three are GET and none polls.** This sits behind a live trading page; a
card that polled a database every few seconds to show a number that moves twice
a day is a cost with no benefit. The browser asks when the operator asks.

`mean_r` comes back as `null`, never `0.0`, for a variant that has scored
nothing. Zero expectancy and no evidence are different statements, and the
dashboard renders the difference.

`contradictions` is the same kind of thing about a different question --
what each buy/sell contradiction policy would have done when two sources
disagreed (2026-09-21). It is here rather than on a router of its own
because it is the same contract: a research log behind an off-by-default
parsing toggle, recording and acting on nothing.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from backend.src.controllers import telegram_controller as tg_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/decision-log", tags=["decision-log"])


@router.get("/summary")
async def summary() -> dict:
    """How many decisions, split by path, and what declined them."""
    return tg_ctl.decision_log_summary()


@router.get("/report")
async def report() -> dict:
    """Champion vs challenger, over the decisions whose trade has closed.

    Wrapped in an object rather than returned as a bare list: a top-level JSON
    array leaves nowhere to add a field later without breaking every reader.
    """
    return {"variants": tg_ctl.decision_log_report()}


@router.get("/contradictions")
async def contradictions() -> dict:
    """Per contradiction policy: how often each verdict came up.

    Wrapped in an object for the same reason `report` is -- a bare top-level
    JSON array leaves nowhere to add a field later.

    An `action` recorded as null is an ABSTENTION and is counted separately
    as `abstained`. Folding it into `allow` would report an approval rate
    for a policy that had nothing to decide on.
    """
    return {"policies": tg_ctl.contradiction_report()}


@router.post("/backfill")
async def backfill() -> dict:
    """Rebuild decisions from past Telegram trades.

    POST because it writes rows, and a button rather than anything automatic
    because it walks every past trade. Safe to press twice: the rebuild is
    idempotent and the count says how many were genuinely new, which is what
    tells the operator that pressing it again will do nothing.

    Reconstructed rows are marked as such. The clock gates and the entry
    trigger rebuild exactly; the news calendar and the trend read are gone and
    record no opinion rather than a guessed one.
    """
    added = tg_ctl.decision_log_backfill()
    return {
        "added": added,
        "note": (f"Rebuilt {added} past decision(s)." if added
                 else "Nothing new to rebuild — every past trade is already in."),
    }
