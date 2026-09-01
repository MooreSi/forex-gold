"""The Trading Schedule tab's clock control.

The offset setting, its readers and the Mac-to-VPS sync all went in before
anything could set it. This covers the control that finally does, and the
choice-building behind it -- a dropdown of offsets is only safer than a typed
number if the list is actually right.

Source-level plus pure data. Nothing here renders NiceGUI or reaches a broker.
"""
from __future__ import annotations

import pytest

from frontend.pages.trading import _schedule as sched_page
from tests.frontend._source import module_source


class TestTheChoices:
    def test_the_machine_clock_is_first_and_is_the_default(self):
        """Every single-machine install wants this one, and it is what the
        setting means when it is empty."""
        options = sched_page._offset_options()

        assert list(options)[0] is sched_page.MACHINE_CLOCK

    def test_it_spans_the_inhabited_world(self):
        """-12:00 to +14:00. Stopping at +12 would silently exclude Kiribati
        and New Zealand in daylight saving."""
        offsets = [k for k in sched_page._offset_options()
                   if k is not sched_page.MACHINE_CLOCK]

        assert min(offsets) == -12 * 60
        assert max(offsets) == 14 * 60

    def test_the_half_hour_zones_are_there(self):
        """India is +05:30 and half a billion people live in it. A whole-hour
        list would have no entry they could pick."""
        options = sched_page._offset_options()

        assert 330 in options and options[330] == "UTC+05:30"

    def test_the_three_quarter_hour_zones_are_there(self):
        """Nepal is +05:45, the Chatham Islands +12:45. A 30-minute step
        would leave both with no correct choice."""
        options = sched_page._offset_options()

        assert 345 in options and 765 in options

    def test_utc_is_offered(self):
        assert sched_page._offset_options()[0] == "UTC+00:00"

    def test_a_negative_half_hour_reads_correctly(self):
        """-03:30 is Newfoundland. Formatted from independently-signed parts
        it comes out as -02:30, which is an hour wrong and looks plausible."""
        assert sched_page._offset_options()[-210] == "UTC-03:30"

    def test_every_choice_is_one_the_setter_accepts(self):
        """A dropdown that can produce a value the service refuses is a
        dropdown that throws in the user's face."""
        from backend.src.services.risk import clock as risk_clock
        from backend.src.utils.trading_clock import MAX_OFFSET_MIN

        for offset in sched_page._offset_options():
            if offset is sched_page.MACHINE_CLOCK:
                continue
            assert isinstance(offset, int) and abs(offset) <= MAX_OFFSET_MIN


class TestTheSelection:
    def test_an_unset_clock_selects_the_machine_entry(self):
        assert sched_page._selected_offset({"configured": None}) is sched_page.MACHINE_CLOCK

    def test_a_configured_clock_selects_that_offset(self):
        assert sched_page._selected_offset({"configured": 330}) == 330

    def test_utc_selects_utc_and_not_the_machine_entry(self):
        """0 is falsy. Anything testing it with `or` puts a UTC+0 user back on
        the machine's clock, which on a VPS is the bug this all exists to fix."""
        assert sched_page._selected_offset({"configured": 0}) == 0


class TestTheChosenValue:
    def test_the_machine_entry_saves_as_none(self):
        assert sched_page._offset_to_save(sched_page.MACHINE_CLOCK) is None

    def test_an_offset_saves_as_itself(self):
        assert sched_page._offset_to_save(330) == 330

    def test_utc_saves_as_zero_not_none(self):
        assert sched_page._offset_to_save(0) == 0


class TestItIsWiredIn:
    def test_the_card_goes_through_the_controller(self):
        """The frontend never reaches past a controller for this."""
        src = module_source("frontend/pages/trading/_schedule.py")

        assert "set_trading_clock_offset" in src
        assert "describe_trading_clock" in src

    def test_it_does_not_reach_into_the_service_or_the_db(self):
        src = module_source("frontend/pages/trading/_schedule.py")

        assert "services.risk.clock" not in src
        assert "backend.src.db" not in src

    def test_the_card_is_rendered_on_the_schedule_tab(self):
        src = module_source("frontend/pages/trading/_schedule.py")

        assert "_render_trading_clock_card" in src
        assert src.count("_render_trading_clock_card") >= 2, (
            "defined but never called"
        )


class TestItActuallyRenders:
    """The source checks above prove the card is written and called. Only a
    render proves the widgets build -- a bad `ui.select` options dict throws
    at render time and every assertion above would still pass."""

    @pytest.mark.asyncio
    async def test_the_card_is_on_the_page(self, user):
        await user.open("/")
        await user.should_see("Trading Clock")

    @pytest.mark.asyncio
    async def test_the_landmark_can_fail(self, user):
        """Negative control. "Trading Clock" appears in exactly one module,
        so a failure here names this card and nothing else."""
        await user.open("/")
        with pytest.raises(AssertionError):
            await user.should_see("__not_the_trading_clock__")
