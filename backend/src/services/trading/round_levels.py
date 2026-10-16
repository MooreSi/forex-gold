"""Round-number exits for EA Template levels (docs/todo/019 A).

Risk > "Where a template's stop and targets land" > round_number_exits_enabled,
OFF by default. Turning it on changes where a real order's stop and targets
land, and so (through the stop) its lot size: owner sign-off and a demo
session first.

The idea is Osler's (NY Fed staff report 125, FX dealer order books, 2000):
take-profit orders cluster AT round numbers, so price tends to stall there;
stop-loss orders cluster just BEYOND them, so a break runs through them. So a
target a little past a round number is pulled in to just before it, and a
stop sitting just past one is pushed out of that zone. It has never been
measured on gold; the constants below were fixed before any measurement and
are not tuned.

Pure price arithmetic plus one settings read. Nothing here reaches a broker.
"""
from __future__ import annotations

import math
from typing import Optional

SETTING_KEY = "round_number_exits_enabled"
STEP = 5.00      # round levels every $5 (a $10 level is also a $5 level)
ZONE = 1.00      # how close to a level counts as "at" it
OFFSET = 0.30    # how far before a level a target is put
_EPS = 1e-9


def enabled(rs: Optional[dict] = None) -> bool:
    """The switch. Off when it cannot be read: an unreadable setting must
    never move a stop."""
    try:
        if rs is None:
            from backend.src.db import database as _db
            rs = _db.get_risk_settings()
        return bool(int(rs.get(SETTING_KEY, 0) or 0))
    except Exception:
        return False


def adjust_tp(tp: float, entry: float, is_buy: bool) -> float:
    """A target within ZONE past a round level comes back to OFFSET before
    it. Unchanged otherwise, and unchanged if the new target would not be
    beyond the entry reference."""
    if is_buy:
        level = math.floor(tp / STEP + _EPS) * STEP
        if tp - level <= ZONE + _EPS:
            new = round(level - OFFSET, 2)
            return new if new > entry else tp
        return tp
    level = math.ceil(tp / STEP - _EPS) * STEP
    if level - tp <= ZONE + _EPS:
        new = round(level + OFFSET, 2)
        return new if new < entry else tp
    return tp


def adjust_sl(sl: float, entry: float, is_buy: bool) -> float:
    """A stop within ZONE beyond a round level (where the stop orders sit)
    goes to ZONE + OFFSET beyond it. It only ever moves further from entry:
    the old stop was at most ZONE past the level, the new one is ZONE +
    OFFSET past it (pinned by test_a_stop_never_moves_closer_to_entry)."""
    if is_buy:
        level = math.ceil(sl / STEP - _EPS) * STEP
        if level - sl <= ZONE + _EPS:
            return round(level - ZONE - OFFSET, 2)
        return sl
    level = math.floor(sl / STEP + _EPS) * STEP
    if sl - level <= ZONE + _EPS:
        return round(level + ZONE + OFFSET, 2)
    return sl


def adjust_ladder(tps: dict, ref: float, sign: int) -> dict:
    """Every level of an anchor ladder, when the switch is on."""
    if not tps or not enabled():
        return tps
    return {n: adjust_tp(p, ref, sign > 0) for n, p in tps.items()}
