"""The trading sessions, defined once (bugs/057).

Owner, 2026-10-07. Every part of the app that asks "which session is this
hour" asks here: the Trading Markets gate, P&L by session, the Reversal and
Breakout engines, the strategy AI.

    asian    00:00-06:59 UTC
    london   07:00-11:59     London's 08:00 local open is 07:00 UTC in summer
    overlap  12:00-15:59     London and New York both open
    ny       16:00-20:59
    off      21:00-23:59     no session: the gate refuses automated trading

Before this there were six copies and they disagreed on eight hours of the
day (the gate called 21:00-06:59 Asian; the Reversal Engine 00:00-07:59; the
Breakout engine 23:00-07:59). The weekend is a separate question, answered by
`dpm.engine.is_weekly_market_closed` and `market.sessions.get_session`.

Pure. Imports nothing from the app.
"""
from __future__ import annotations

LONDON_START = 7
OVERLAP_START = 12
NY_START = 16
NY_END = 21

SESSIONS = ("asian", "london", "overlap", "ny", "off")


def session_for_hour(utc_hour: int) -> str:
    h = int(utc_hour) % 24
    if h < LONDON_START:
        return "asian"
    if h < OVERLAP_START:
        return "london"
    if h < NY_START:
        return "overlap"
    if h < NY_END:
        return "ny"
    return "off"
