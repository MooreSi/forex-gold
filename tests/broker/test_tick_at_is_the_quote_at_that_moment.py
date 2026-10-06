"""`_get_tick_at` returns the quote in force at the moment asked about, in
true UTC (bugs/052 addendum).

It called `copy_ticks_from(<UTC datetime>, 1)`, which reads the datetime in
the terminal's server-time convention (mt5_ticks.py says why) and returns the
first tick AT OR AFTER it with no bound. Every logged TCA reference quote on
the Mac (5 of 5) came back on the next whole hour, 5 to 54 minutes after the
fill, so the broker/drift split was refused each time and the spread on those
rows came from the wrong moment.

Now it reads a two-minute window through the offset-corrected
`_get_ticks_range` and returns the last tick at or before the moment (the
quote then in force), else the first one after it inside the window, else
None.
"""
from tests.broker.test_tick_history_is_in_utc import (  # noqa: F401
    NOW, _UTCPlus3Terminal, _forget_the_offset, terminal,
)

import mt5_bridge

T = NOW - 1800


def test_the_quote_in_force_at_the_moment(terminal):
    terminal(_UTCPlus3Terminal([(T - 5, 4190.0), (T + 3, 4193.0), (T + 3600, 4300.0)]))
    got = mt5_bridge._get_tick_at(T)
    assert got["bid"] == 4190.0
    assert got["time"] == T - 5
    assert got["spread"] == 0.2


def test_with_nothing_before_it_the_next_tick_in_the_window(terminal):
    terminal(_UTCPlus3Terminal([(T + 3, 4193.0)]))
    assert mt5_bridge._get_tick_at(T)["bid"] == 4193.0


def test_a_tick_an_hour_away_is_not_the_quote(terminal):
    terminal(_UTCPlus3Terminal([(T + 3600, 4300.0)]))
    assert mt5_bridge._get_tick_at(T) is None
