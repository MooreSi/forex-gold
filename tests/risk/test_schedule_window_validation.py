"""A trading window that is not a time silently stops the engines.

The schedule is stored as text and parsed when the engines ask whether they may
trade. `_find_active_block` catches a parse failure and moves on, so a broken
window does not raise anywhere: it simply never matches. Depending on which
window it is, the engines then stop trading or keep trading past a stop, and
nothing on any screen says why.

**Range matters as much as shape**, and this is the part that was missing.
`_parse_hm` is `int(h) * 60 + int(m)`, so "25:00" parses happily to 1500
minutes and "08:99" to 579 — no exception, and no clock time they can ever
equal. A validator that only caught `ValueError` would pass both.

It is checked in the service rather than in the router because every caller
needs it. A paired node forwards its grid straight into `set_trading_schedule`,
and a peer that disables trading on this machine with no error on either side
is the worst version of this bug.

Nothing here touches a database: the store is a recorder.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import schedule as sched


@pytest.fixture
def store(monkeypatch):
    """The app_config write, recorded. Nothing forwards to a peer either."""
    written: list = []
    monkeypatch.setattr(sched.db_module, "set_app_config",
                        lambda k, v: written.append((k, v)))
    monkeypatch.setattr(sched, "_maybe_forward_trading_schedule", lambda *a: None)
    return written


def _grid(start, end, enabled=True):
    return {"mon": [{"start": start, "end": end, "enabled": enabled}]}


class TestShape:
    @pytest.mark.parametrize("bad", ["noon", "08-00", "8", ":", "08:"])
    def test_a_window_that_is_not_hhmm_is_refused(self, bad, store):
        with pytest.raises(ValueError):
            sched.set_trading_schedule(_grid(bad, "12:00"))

        assert store == []

    def test_the_message_names_the_day_the_window_and_the_end(self, store):
        """An operator with four windows a day across seven days needs to be
        told which one, not that "the schedule" is wrong."""
        with pytest.raises(ValueError) as exc:
            sched.set_trading_schedule(
                {"tue": [{"start": "08:00", "end": "12:00"},
                         {"start": "13:00", "end": "banana"}]})

        assert "tue" in str(exc.value)
        assert "window 2" in str(exc.value)
        assert "end" in str(exc.value)
        assert "banana" in str(exc.value)


class TestRange:
    """The half that a try/except alone would miss."""

    @pytest.mark.parametrize("bad", ["25:00", "8:60", "99:99", "24:00", "-1:00"])
    def test_a_time_that_is_not_a_time_of_day_is_refused(self, bad, store):
        with pytest.raises(ValueError):
            sched.set_trading_schedule(_grid(bad, "12:00"))

        assert store == []

    def test_the_parser_alone_would_have_accepted_it(self):
        """The reason the range check exists, asserted rather than asserted
        about. `_parse_hm("25:00")` raises nothing and returns a number no
        clock can equal."""
        assert sched._parse_hm("25:00") == 1500

    def test_midnight_and_the_last_minute_are_both_times(self, store):
        sched.set_trading_schedule(_grid("00:00", "23:59"))

        assert len(store) == 1


class TestTheGapsThatAreMeantToBeThere:
    def test_a_window_with_no_times_at_all_is_how_the_grid_says_empty(self, store):
        """Four rows a day means most of them are blank. Rejecting falsy values
        would make the screen unusable."""
        sched.set_trading_schedule(_grid("", ""))

        assert len(store) == 1

    def test_a_window_with_one_end_missing_is_a_typo_not_a_gap(self, store):
        with pytest.raises(ValueError):
            sched.set_trading_schedule(_grid("08:00", ""))

        assert store == []

    def test_a_disabled_window_is_checked_too(self, store):
        """Enabling it later is one click. A bad time that only bites once
        somebody ticks the box is worse, not better."""
        with pytest.raises(ValueError):
            sched.set_trading_schedule(_grid("bad", "12:00", enabled=False))

        assert store == []

    def test_an_empty_schedule_is_allowed(self, store):
        sched.set_trading_schedule({})

        assert len(store) == 1


class TestAPeerCannotBreakThisNode:
    def test_a_forwarded_schedule_is_validated_the_same_way(self, store):
        """`_from_sync=True` is the paired node's path. A peer that disables
        trading here with no error on either side is the worst version of this
        bug."""
        with pytest.raises(ValueError):
            sched.set_trading_schedule(_grid("25:00", "12:00"), _from_sync=True)

        assert store == []


class TestAGoodScheduleIsStoredUnchanged:
    def test_it_is_written_as_json_under_the_expected_key(self, store):
        """Negative control for every test above: a validator that rejected
        everything would pass all of them."""
        import json

        sched.set_trading_schedule(_grid("08:00", "12:00"))

        key, value = store[0]
        assert key == "trading_schedule"
        assert json.loads(value) == _grid("08:00", "12:00")
