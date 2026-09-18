"""Pausing trading by hand, and lifting the pause again.

Three surfaces have written `trade_pause_until` directly: the Telegram
`/pause` command, the bot panel's end-of-session pause, and the NiceGUI header
dialog. They agreed about pausing and **disagreed about resuming**: `/resume`
calls `rearm_risk_guards()` and the dashboard's Resume button did not.

That difference is not cosmetic. Both post-close guards halt for the rest of
the broker day, so resuming after a give-back halt without re-arming is a
no-op — the day's peak is already spent and the guard re-trips on the very
next close. The operator presses Resume, sees trading resume, and it stops
again on the first trade that closes. The button appears broken and the reason
is invisible.

So this is one function, used by every surface, rather than the same three
lines written three times and drifting.

**Pausing is the safe direction and is not gated.** Resuming lets automated
entries happen again, so it re-arms rather than simply clearing the flag —
this is the one place where "do less" would be the dangerous choice.

Nothing here reaches a broker or a real database.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.risk import manual_pause


@pytest.fixture
def store(monkeypatch):
    s = {"config": {}, "rearmed": 0}
    monkeypatch.setattr(manual_pause.db_module, "set_app_config",
                        lambda k, v: s["config"].__setitem__(k, v))
    monkeypatch.setattr(manual_pause.db_module, "get_app_config",
                        lambda k: s["config"].get(k))
    monkeypatch.setattr(manual_pause._governor, "rearm_risk_guards",
                        lambda: s.__setitem__("rearmed", s["rearmed"] + 1))
    return s


class TestPausing:
    """`_pause_for` is private: `pause()` is the one surface, so that the rule
    about which box wins cannot be bypassed by a caller reaching past it."""

    def test_a_duration_in_hours_becomes_a_timestamp_in_the_future(self, store):
        until = manual_pause._pause_for(hours=2)

        assert until == pytest.approx(time.time() + 7200, abs=5)
        assert float(store["config"]["trade_pause_until"]) == pytest.approx(until)

    def test_a_quarter_hour_is_allowed(self, store):
        """15 minutes is the smallest step the dialog offered and a real use:
        stepping away during a news release."""
        until = manual_pause._pause_for(hours=0.25)

        assert until == pytest.approx(time.time() + 900, abs=5)

    def test_an_explicit_moment_is_used_as_given(self, store):
        target = time.time() + 3600

        assert manual_pause.pause_until(target) == pytest.approx(target)

    def test_a_moment_in_the_past_is_refused(self, store):
        """It would write a pause that is already expired — trading would not
        stop, and the screen would say it had."""
        with pytest.raises(ValueError) as exc:
            manual_pause.pause_until(time.time() - 60)

        assert "future" in str(exc.value)
        assert store["config"] == {}

    def test_now_is_refused_too(self, store):
        with pytest.raises(ValueError):
            manual_pause.pause_until(time.time())

    def test_a_duration_of_zero_is_refused(self, store):
        with pytest.raises(ValueError):
            manual_pause._pause_for(hours=0)

    def test_pausing_does_not_rearm_anything(self, store):
        """Re-arming on the way IN would reset the guards' windows at the
        moment a guard may have just halted trading."""
        manual_pause._pause_for(hours=1)

        assert store["rearmed"] == 0


class TestChoosingBetweenTheTwoBoxes:
    """A dialog offers "pause for N hours" and "pause until HH:MM". The rule
    lives here so the Telegram command gets it too."""

    def test_a_moment_beats_a_duration(self, store):
        target = time.time() + 7200

        assert manual_pause.pause(hours=1, until=target) == pytest.approx(target)

    def test_hours_are_used_when_no_moment_is_given(self, store):
        assert manual_pause.pause(hours=1) == pytest.approx(time.time() + 3600, abs=5)

    def test_neither_falls_back_to_four_hours_not_to_zero(self, store):
        """Zero would be a pause already in the past: trading would not stop
        and the screen would say it had."""
        assert manual_pause.pause() == pytest.approx(time.time() + 4 * 3600, abs=5)

    def test_an_explicit_zero_is_still_refused(self, store):
        """`None` means "not given" and gets the default; `0` means the
        operator typed a zero, which is not a pause."""
        with pytest.raises(ValueError):
            manual_pause.pause(hours=0)


class TestResuming:
    def test_it_clears_the_pause(self, store):
        store["config"]["trade_pause_until"] = str(time.time() + 3600)

        manual_pause.resume()

        assert store["config"]["trade_pause_until"] == "0"

    def test_it_rearms_the_post_close_guards(self, store):
        """The difference between the two old surfaces, and the whole reason
        this function exists. Without it, resuming after a give-back halt is
        undone by the next close and the button appears broken."""
        manual_pause.resume()

        assert store["rearmed"] == 1

    def test_the_flag_is_cleared_even_if_rearming_fails(self, store, monkeypatch):
        """Fail-safe in the direction of the operator's intent. They asked for
        trading to resume; a guard that could not be re-armed must not leave
        the pause in place with no explanation."""
        def _boom():
            raise RuntimeError("database locked")

        monkeypatch.setattr(manual_pause._governor, "rearm_risk_guards", _boom)

        manual_pause.resume()

        assert store["config"]["trade_pause_until"] == "0"


class TestTheStateItReports:
    def test_a_future_pause_reads_as_paused(self, store):
        until = time.time() + 600
        store["config"]["trade_pause_until"] = str(until)

        state = manual_pause.state()

        assert state["paused"] is True
        assert state["until"] == pytest.approx(until)

    def test_an_expired_pause_reads_as_not_paused(self, store):
        store["config"]["trade_pause_until"] = str(time.time() - 600)

        assert manual_pause.state()["paused"] is False

    def test_no_stored_value_reads_as_not_paused(self, store):
        assert manual_pause.state()["paused"] is False

    def test_an_unreadable_value_reads_as_not_paused_HERE(self, store):
        """Deliberately the opposite of `governor.is_trading_paused()`, which
        fails CLOSED and reports PAUSED when it cannot read.

        That one gates orders and must never let one through on a bad read.
        This one only decides whether a dialog offers Pause or Resume, and
        showing Resume on a machine that is not paused is harmless; refusing to
        offer Pause because a read blipped is not.
        """
        store["config"]["trade_pause_until"] = "not a number"

        assert manual_pause.state()["paused"] is False
