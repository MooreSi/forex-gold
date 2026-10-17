"""Bars as an engine should read them: closed, and stamped in UTC.

The bridge's /candles is MT5 `copy_rates_from_pos(symbol, tf, 0, count)`.
Position 0 is the bar still forming and it comes LAST; every stamp is broker
time (New York 17:00 = 00:00) written as if it were a UTC epoch. Measured on
2026-10-17: Friday's last H1 bar read 23:00, and gold shuts at 21:00 UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone

from backend.src.services.market import bar_clock

H = 3600
# Wednesday 2026-10-14 12:02:30 UTC, the broker three hours ahead (US summer).
NOW = datetime(2026, 10, 14, 12, 2, 30, tzinfo=timezone.utc).timestamp()
SUMMER = 3 * H


def _bar(ts, close=100.0):
    return {"ts": ts, "open": close, "high": close + 1, "low": close - 1, "close": close}


class TestTheOffsetIsReadOffTheTick:
    def test_a_fresh_broker_tick_gives_the_whole_hours(self):
        assert bar_clock.broker_offset_s(NOW + SUMMER - 2, NOW) == SUMMER

    def test_the_winter_offset_is_read_the_same_way(self):
        """The broker moves from +3h to +2h in late October. Reading it every
        cycle is what keeps the engines right across that change."""
        assert bar_clock.broker_offset_s(NOW + 2 * H - 2, NOW) == 2 * H

    def test_a_utc_stamp_reads_as_no_offset(self):
        assert bar_clock.broker_offset_s(NOW - 5, NOW) == 0

    def test_a_tick_twenty_minutes_old_still_gives_the_right_hour(self):
        assert bar_clock.broker_offset_s(NOW + SUMMER - 20 * 60, NOW) == SUMMER

    def test_a_tick_too_stale_to_tell_gives_none(self):
        """Forty minutes stale and the nearest whole hour is the wrong one
        (+2h, not +3h). Guessing it would move every clock-based level an
        hour, so the answer is that the clock cannot be set."""
        assert bar_clock.broker_offset_s(NOW + SUMMER - 40 * 60, NOW) is None

    def test_the_limit_is_half_an_hour_either_side_of_it(self):
        """Rounding to the nearest hour flips to the wrong one at exactly
        thirty minutes, so the staleness limit has to be what decides: a
        tick 31 minutes old is refused, not read as +2h."""
        assert bar_clock.broker_offset_s(NOW + SUMMER - 29 * 60, NOW) == SUMMER
        assert bar_clock.broker_offset_s(NOW + SUMMER - 31 * 60, NOW) is None

    def test_a_tick_from_the_future_beyond_the_skew_gives_none(self):
        """Two minutes ahead is not skew, it is two clocks disagreeing."""
        assert bar_clock.broker_offset_s(NOW + SUMMER + 120, NOW) is None

    def test_a_missing_stamp_gives_none(self):
        assert bar_clock.broker_offset_s(None, NOW) is None
        assert bar_clock.broker_offset_s(0, NOW) is None

    def test_a_little_clock_skew_between_machines_is_tolerated(self):
        """The bridge and this process can run on different machines."""
        assert bar_clock.broker_offset_s(NOW + SUMMER + 20, NOW) == SUMMER


class TestTheTickAndTheBarsMustAgree:
    """`measured_offset_s` reads the tick and checks it against the newest
    bar. If the bridge ever sent a tick without its broker stamp, the client
    fills in its own clock (UTC): read alone that says "no offset" while the
    bars stay three hours ahead, and every recent bar would look unclosed."""

    def _bars(self, newest_open_utc, off=SUMMER):
        return [_bar(newest_open_utc - 300 + off), _bar(newest_open_utc + off)]

    def test_they_agree_and_the_offset_is_returned(self):
        tick = type("T", (), {"timestamp": NOW + SUMMER - 2})()
        assert bar_clock.measured_offset_s(tick, self._bars(NOW - 150), NOW) == SUMMER

    def test_a_utc_stamped_tick_against_broker_bars_is_refused(self):
        tick = type("T", (), {"timestamp": NOW - 1})()
        assert bar_clock.measured_offset_s(tick, self._bars(NOW - 150), NOW) is None

    def test_a_bar_too_old_to_judge_does_not_veto_the_tick(self):
        """A quiet bucket or a short gap: the newest bar is stale, the tick
        is fresh, and the tick decides."""
        tick = type("T", (), {"timestamp": NOW + SUMMER - 2})()
        assert bar_clock.measured_offset_s(tick, self._bars(NOW - 45 * 60), NOW) == SUMMER

    def test_no_tick_is_no_offset(self):
        assert bar_clock.measured_offset_s(None, self._bars(NOW - 150), NOW) is None


class TestClosedBars:
    def _m5(self):
        # Broker-stamped M5 bars; the last opened at 12:00 UTC (15:00 broker)
        # and is still forming at 12:02:30.
        opens = [NOW - 150 - 300 * k for k in range(4, -1, -1)]
        return [_bar(t + SUMMER, close=100 + i) for i, t in enumerate(opens)]

    def test_the_forming_bar_is_dropped(self):
        got = bar_clock.utc_closed(self._m5(), 300, SUMMER, NOW)
        assert len(got) == 4
        assert got[-1]["close"] == 103

    def test_stamps_come_back_in_utc(self):
        got = bar_clock.utc_closed(self._m5(), 300, SUMMER, NOW)
        assert datetime.fromtimestamp(got[-1]["ts"], timezone.utc).strftime("%H:%M") == "11:55"

    def test_a_bar_that_closed_with_no_tick_after_it_is_kept(self):
        """With no tick yet in the new bucket MT5's last bar is the one that
        just CLOSED. Dropping the last element blindly would throw it away;
        the close time decides."""
        bars = self._m5()[:-1]
        got = bar_clock.utc_closed(bars, 300, SUMMER, NOW)
        assert len(got) == 4

    def test_the_input_is_not_modified(self):
        bars = self._m5()
        before = [dict(b) for b in bars]
        bar_clock.utc_closed(bars, 300, SUMMER, NOW)
        assert bars == before

    def test_nothing_in_nothing_out(self):
        assert bar_clock.utc_closed(None, 300, SUMMER, NOW) == []
        assert bar_clock.utc_closed([], 300, SUMMER, NOW) == []


class TestUtcStamped:
    def test_every_bar_is_restamped_and_the_forming_one_kept(self):
        bars = [_bar(NOW - 3600 + SUMMER), _bar(NOW - 100 + SUMMER)]
        got = bar_clock.utc_stamped(bars, SUMMER)
        assert [b["ts"] for b in got] == [NOW - 3600, NOW - 100]
        assert bars[0]["ts"] == NOW - 3600 + SUMMER     # the input is untouched
