"""Say it once, keep saying it occasionally, never say it 7,200 times.

Three separate loops in this app log the same warning on every cycle while a
condition persists, and all three were found in one day (2026-09-01):

  * the reconciliation pass, every ~12s while a placeholder sat out its
    24-hour expiry -- roughly 7,200 identical warnings (fixed in 3de72e5)
  * `monitor_loop`'s "EA unhealthy" line, every second while the EA was off
    the chart -- ~400 lines in the seven minutes it took to run one demo
  * the refused-close ERROR, every second for as long as AutoTrading was off

The cost is never disk. A warning that appears thousands of times stops being
read, and the next genuinely new one scrolls past inside it. But going silent
is worse: a condition that persists all day matters more than one that appears
once, not less.

So: loud on a change, quiet on a repeat, and a periodic reminder so a standing
problem cannot disappear. This is the third site, which is what makes it a
shared helper rather than a third bespoke implementation -- the repo carries a
duplicate-implementation detector for exactly this reason.
"""
from __future__ import annotations

import pytest

from backend.src.utils import log_throttle


@pytest.fixture(autouse=True)
def _clean():
    log_throttle.reset()
    yield
    log_throttle.reset()


class TestTheFirstTime:
    def test_a_new_condition_should_be_announced(self):
        assert log_throttle.should_announce("ea", "unhealthy") is True


class TestARepeat:
    def test_the_same_condition_should_not(self):
        log_throttle.should_announce("ea", "unhealthy")

        assert log_throttle.should_announce("ea", "unhealthy") is False

    def test_and_not_on_the_thousandth_try_either(self):
        log_throttle.should_announce("ea", "unhealthy")

        assert not any(log_throttle.should_announce("ea", "unhealthy")
                       for _ in range(1000))


class TestAChange:
    def test_a_different_condition_is_announced(self):
        log_throttle.should_announce("ea", "unhealthy")

        assert log_throttle.should_announce("ea", "disconnected") is True

    def test_and_then_that_one_repeats_quietly(self):
        log_throttle.should_announce("ea", "unhealthy")
        log_throttle.should_announce("ea", "disconnected")

        assert log_throttle.should_announce("ea", "disconnected") is False

    def test_going_back_to_the_first_condition_is_announced_again(self):
        """Flapping between two states is itself news, and a cache that
        remembered every condition forever would hide it."""
        log_throttle.should_announce("ea", "unhealthy")
        log_throttle.should_announce("ea", "disconnected")

        assert log_throttle.should_announce("ea", "unhealthy") is True


class TestSeparateSubjects:
    def test_two_keys_do_not_silence_each_other(self):
        """Two trades, both unhealthy, must each get their own line -- the
        operator needs to know it is both and not one."""
        log_throttle.should_announce("trade-a", "unhealthy")

        assert log_throttle.should_announce("trade-b", "unhealthy") is True

    def test_and_each_repeats_quietly_on_its_own(self):
        log_throttle.should_announce("trade-a", "unhealthy")
        log_throttle.should_announce("trade-b", "unhealthy")

        assert log_throttle.should_announce("trade-a", "unhealthy") is False
        assert log_throttle.should_announce("trade-b", "unhealthy") is False


class TestItNeverGoesFullySilent:
    def test_a_standing_condition_is_repeated_after_the_interval(self, monkeypatch):
        log_throttle.should_announce("ea", "unhealthy", interval_s=60)
        now = [log_throttle.time.time()]
        monkeypatch.setattr(log_throttle.time, "time", lambda: now[0])

        now[0] += 61

        assert log_throttle.should_announce("ea", "unhealthy", interval_s=60) is True

    def test_and_goes_quiet_again_straight_after(self, monkeypatch):
        now = [log_throttle.time.time()]
        monkeypatch.setattr(log_throttle.time, "time", lambda: now[0])
        log_throttle.should_announce("ea", "unhealthy", interval_s=60)
        now[0] += 61
        log_throttle.should_announce("ea", "unhealthy", interval_s=60)

        assert log_throttle.should_announce("ea", "unhealthy", interval_s=60) is False

    def test_the_default_interval_is_long_enough_to_be_worth_having(self):
        """At one call a second, anything under a few minutes barely helps."""
        assert log_throttle.DEFAULT_INTERVAL_S >= 900


class TestClearing:
    def test_a_resolved_condition_is_announced_again_when_it_returns(self):
        """When the problem goes away the caller clears it, so its return is
        news rather than a match against a stale entry."""
        log_throttle.should_announce("ea", "unhealthy")
        log_throttle.clear("ea")

        assert log_throttle.should_announce("ea", "unhealthy") is True

    def test_clearing_something_unknown_is_harmless(self):
        log_throttle.clear("never-seen")

    def test_reset_forgets_everything(self):
        log_throttle.should_announce("a", "x")
        log_throttle.should_announce("b", "x")
        log_throttle.reset()

        assert log_throttle.should_announce("a", "x") is True
        assert log_throttle.should_announce("b", "x") is True


class TestItDoesNotGrowForever:
    def test_the_store_is_bounded(self):
        """This is called from loops that run forever, with keys that include
        trade ids. An unbounded dict is a slow leak in a process meant to run
        for weeks."""
        for i in range(log_throttle.MAX_TRACKED * 3):
            log_throttle.should_announce(f"trade-{i}", "unhealthy")

        assert len(log_throttle._seen) <= log_throttle.MAX_TRACKED
