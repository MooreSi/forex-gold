"""Out of Hours must run on a timezone the owner chooses, not on UTC alone.

handover/020. The Trading Schedule moved to Europe/London on 2026-09-01 because
those windows are the owner's day. Out of Hours stayed on UTC, so for the four
months a year the UK is on BST the two disagree by an hour: an OOH window set
to 22:00 actually begins at 23:00 local, and that hour of the night is managed
by the base strategy instead of the OOH one.

**Why not the machine's local clock, which is what was asked for.** This app
runs on two machines -- the owner's box and a VPS at 217.155.25.160, which is
conventionally UTC. Reading each machine's own clock would make the two nodes
enter Out of Hours an hour apart for those same four months and disagree about
which strategy manages a trade, while each looked correct on its own. That is
the inconsistency the request was meant to remove, arriving by another route.
A named zone, stored once, gives the same answer on both machines and still
works for a user in another country. Owner's choice, 2026-09-07.

**The default is UTC, deliberately, and it is NOT what was asked for.** The
tunable rule (docs/system/rules/60-adding-a-tunable.md) is that a default must
be byte-identical to the constant it replaces: nothing may trade differently
until a human moves a dial. Defaulting to the machine's zone would silently
retune every existing install on upgrade -- and on this install it would
default the two nodes to DIFFERENT zones, which is precisely the split being
designed out. The owner sets the value once; the code changes nothing on its
own.

get_effective_strategy decides which strategy manages a trade
(monitor_cycle.py:206), so a throw here stops trade management. Every bad
input is tested for that, not merely for correctness.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from backend.src.services.risk import risk_settings_repo as rsr


def _rs(**over):
    base = {
        "trade_strategy": "scale_out", "ooh_enabled": 1,
        "ooh_start_time": "22:00", "ooh_end_time": "07:00",
        "ooh_strategy": "conservative",
    }
    base.update(over)
    return base


# 21:30 UTC on a July evening. Europe/London is BST (+1), so it is 22:30 local
# -- inside a 22:00-07:00 window there, and outside it in UTC. The whole
# disagreement in one instant.
BST_EVENING = datetime(2026, 7, 15, 21, 30, tzinfo=timezone.utc)
# January: London is UTC, so both readings must agree.
GMT_EVENING = datetime(2026, 1, 15, 21, 30, tzinfo=timezone.utc)


class TestTheDefaultChangesNothing:
    def test_no_timezone_set_behaves_exactly_as_utc_did(self):
        """The upgrade must not retune anyone. 21:30 UTC is outside a
        22:00-07:00 UTC window, and that is what it stayed."""
        strat, active = rsr.get_effective_strategy(_rs(), now=BST_EVENING)

        assert active is False
        assert strat == "scale_out"

    def test_an_explicit_utc_is_the_same_as_none(self):
        assert rsr.get_effective_strategy(
            _rs(ooh_timezone="UTC"), now=BST_EVENING) == ("scale_out", False)

    def test_utc_still_activates_inside_its_own_window(self):
        """Negative control: the default must still be capable of firing, or
        the test above passes for a system that never activates at all."""
        strat, active = rsr.get_effective_strategy(
            _rs(), now=datetime(2026, 7, 15, 23, 30, tzinfo=timezone.utc))

        assert active is True
        assert strat == "conservative"


class TestAChosenZoneIsUsed:
    def test_london_activates_an_hour_before_utc_does_in_summer(self):
        """The reported problem, as a test: 22:30 BST is inside the window
        the owner set, and UTC says it is not."""
        strat, active = rsr.get_effective_strategy(
            _rs(ooh_timezone="Europe/London"), now=BST_EVENING)

        assert active is True
        assert strat == "conservative"

    def test_in_winter_london_and_utc_agree(self):
        """London is UTC in January. If this differed, the code would be
        applying a fixed offset rather than a real zone."""
        assert rsr.get_effective_strategy(
            _rs(ooh_timezone="Europe/London"), now=GMT_EVENING)[1] is False
        assert rsr.get_effective_strategy(
            _rs(), now=GMT_EVENING)[1] is False

    def test_a_zone_the_other_side_of_the_world(self):
        """Not a UK special case. 21:30 UTC is 06:30 next day in Tokyo, which
        is inside a 22:00-07:00 window."""
        assert rsr.get_effective_strategy(
            _rs(ooh_timezone="Asia/Tokyo"), now=BST_EVENING)[1] is True

    def test_the_date_range_uses_the_same_zone(self):
        """A holiday range is the owner's dates, so it must be read in the
        owner's zone. 2026-07-15 21:30Z is already the 16th in Tokyo."""
        rs = _rs(ooh_timezone="Asia/Tokyo", ooh_date_active=1,
                 ooh_date_from="2026-07-16", ooh_date_to="2026-07-16")

        assert rsr.get_effective_strategy(rs, now=BST_EVENING)[1] is True


class TestBadInputCannotStopTradeManagement:
    """get_effective_strategy runs in monitor_cycle. A throw here is not a
    wrong answer, it is trade management stopping."""

    @pytest.mark.parametrize("bad", [
        "Not/AZone", "", "   ", "Europe/Londo", "12:00", None, 7, "UTC+1",
    ])
    def test_an_unusable_zone_falls_back_to_utc_inside_the_helper(self, bad):
        """Asserted on `_ooh_now` directly, NOT through
        get_effective_strategy.

        get_effective_strategy wraps its whole body in try/except and returns
        (base, False) on any exception -- which is identical to what a correct
        UTC fallback produces for this instant. Going through it, a version
        that RAISED on a bad zone passed this test: proved by mutation, the
        fallback narrowed to `except ValueError` while ZoneInfo raises
        ZoneInfoNotFoundError (a KeyError), and nothing went red. The outer
        handler was answering for the code under test.
        """
        got = rsr._ooh_now(_rs(ooh_timezone=bad), now=BST_EVENING)

        assert got == BST_EVENING
        assert got.utcoffset() == timedelta(0), f"{bad!r} did not fall back to UTC"

    def test_a_naive_now_is_read_as_utc_not_as_the_machines_clock(self):
        """Callers pass no `now` in production, but a naive datetime must not
        be silently read against whatever zone the machine happens to be in.

        `astimezone()` on a naive datetime assumes LOCAL time rather than
        raising, so this is a silent wrong answer, not a crash -- and a test
        that merely called the function without asserting caught nothing
        (proved by mutation). 21:30 naive, read as UTC, is 22:30 in London and
        inside the window; read as the local clock of a machine already on BST
        it is 21:30 London and outside it.
        """
        strat, active = rsr.get_effective_strategy(
            _rs(ooh_timezone="Europe/London"),
            now=datetime(2026, 7, 15, 21, 30))

        assert active is True
        assert strat == "conservative"

    def test_a_non_utc_now_is_converted_not_assumed(self):
        """An aware datetime in some other zone must be converted, not read
        as though its wall clock were already the target zone."""
        tokyo_now = BST_EVENING.astimezone(timezone(timedelta(hours=9)))

        assert rsr.get_effective_strategy(
            _rs(ooh_timezone="Europe/London"), now=tokyo_now)[1] is True
