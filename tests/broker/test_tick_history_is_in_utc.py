"""Tick history is asked for, and handed back, in true UTC.

docs/todo/reversal-engine/250, item 2. `copy_ticks_range` reads the datetimes
it is given in the terminal's SERVER-time convention and stamps the ticks it
returns in that same convention. On the VPS that is UTC+3, so a request for
"the last hour" came back three hours early, stamped as if it were now. The
2026-09-03 probe compared the returned stamps with the request -- which agree
with each other in either convention -- so it could not see it. The audit of
2026-09-30 did: the unshifted query priced gold near 4181 while the live quote
was near 4193.

The fake terminal below behaves the way a UTC+3 broker does: it holds ticks
at their true UTC moment, reads a request's datetimes as server time and
stamps what it returns in server time.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

import mt5_bridge
import mt5_ticks

SERVER_OFFSET = 3 * 3600          # the VPS's broker
NOW = 1_790_000_000.0             # a moment the market is open


class _UTCPlus3Terminal:
    COPY_TICKS_ALL = -1

    def __init__(self, true_ticks, last_tick_age_s=2.0, offset=SERVER_OFFSET):
        self._ticks = true_ticks          # [(true_utc_ts, bid)]
        self._offset = offset
        self._age = last_tick_age_s
        self.requests = []

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time=int(NOW - self._age + self._offset))

    def copy_ticks_range(self, symbol, start: datetime, end: datetime, flags):
        self.requests.append((start, end))
        lo = start.timestamp() - self._offset     # server time -> true UTC
        hi = end.timestamp() - self._offset
        return [{"time": int(t + self._offset), "bid": b, "ask": b + 0.2,
                 "last": 0.0, "volume": 0.0, "flags": 6}
                for t, b in self._ticks if lo <= t <= hi]


@pytest.fixture(autouse=True)
def _forget_the_offset():
    mt5_ticks.reset_offset_cache()
    yield
    mt5_ticks.reset_offset_cache()


@pytest.fixture
def terminal(monkeypatch):
    def install(term):
        monkeypatch.setattr(mt5_bridge, "mt5", term)
        monkeypatch.setattr(mt5_bridge, "_ensure_connected", lambda: True)
        monkeypatch.setattr(mt5_ticks.time, "time", lambda: NOW)
        return term
    return install


# Gold three hours ago was 4181; in the last hour it is 4193.
TICKS = [(NOW - 3 * 3600 - 1800, 4181.0), (NOW - 1800, 4193.0)]


def test_the_last_hour_is_the_last_hour(terminal):
    terminal(_UTCPlus3Terminal(TICKS))

    got = mt5_bridge._get_ticks_range(NOW - 3600, NOW)

    assert [t["bid"] for t in got] == [4193.0]


def test_the_ticks_come_back_stamped_in_true_utc(terminal):
    """Price and stamp together: the uncorrected read returns the 4181 tick
    stamped NOW-1800 (three hours early, stamped three hours late), so a
    check of the stamp alone passes against the bug."""
    terminal(_UTCPlus3Terminal(TICKS))

    got = mt5_bridge._get_ticks_range(NOW - 3600, NOW)

    assert [(t["time"], t["bid"]) for t in got] == [(NOW - 1800, 4193.0)]


def test_a_broker_on_utc_is_left_alone(terminal):
    """The negative control: with no offset the correction must be a no-op."""
    term = terminal(_UTCPlus3Terminal(TICKS, offset=0))

    got = mt5_bridge._get_ticks_range(NOW - 3600, NOW)

    assert [t["bid"] for t in got] == [4193.0]
    assert term.requests[0][0].timestamp() == pytest.approx(NOW - 3600)


def test_a_stale_quote_with_nothing_learnt_refuses(terminal):
    """Market closed: the last tick is 47 minutes old, so tick time minus now
    is not an offset. With no earlier reading to fall back on, the answer is
    None -- the same "cannot say" every other failure here returns -- rather
    than a window shifted by 47 minutes."""
    terminal(_UTCPlus3Terminal(TICKS, last_tick_age_s=47 * 60))

    assert mt5_bridge._get_ticks_range(NOW - 3600, NOW) is None


def test_a_stale_quote_uses_the_offset_learnt_while_the_market_was_open(terminal):
    terminal(_UTCPlus3Terminal(TICKS))
    mt5_bridge._get_ticks_range(NOW - 3600, NOW)            # learns +3h

    terminal(_UTCPlus3Terminal(TICKS, last_tick_age_s=47 * 60))
    got = mt5_bridge._get_ticks_range(NOW - 3600, NOW)

    assert [t["bid"] for t in got] == [4193.0]


def test_a_weekend_old_quote_is_never_read_as_an_offset(terminal):
    """Two days stale rounds to a whole number of half hours (-45h), which
    the freshness test alone would not catch."""
    terminal(_UTCPlus3Terminal(TICKS, last_tick_age_s=48 * 3600))

    assert mt5_bridge._get_ticks_range(NOW - 3600, NOW) is None


def test_the_one_day_bound_still_holds(terminal):
    terminal(_UTCPlus3Terminal(TICKS))

    assert mt5_bridge._get_ticks_range(NOW - 2 * 86400, NOW) is None
