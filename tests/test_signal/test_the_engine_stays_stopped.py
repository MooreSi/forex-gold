"""The Bounce engine does not start, by any route.

Its panel was removed on 2026-09-02. The service was left in place and excluded
from `engines_controller.start_stopped_engines`, and the code has claimed ever
since that it therefore "cannot run with no panel to show that it is running".

**It ran anyway, for twelve days** (`docs/todo/bugs/046`), because the bulk
start is not the only thing that starts it:

  * `app.py` auto-starts it on every launch unless `sg_engine_enabled` is "0",
    and that key is only ever written by a Stop Engine button that no longer
    exists on any screen;
  * `app.py`'s own watchdog loop **re-starts it every five minutes** if it
    finds it stopped — so even stopping it by hand did not keep it stopped.

Owner, 2026-09-14: *"the bounce engine has now been removed so there shouldn't
be any decisions relating to this"*. It had not been; it is now.

The guard is in `start()` rather than at the three call sites, for the reason
this bug shares with bugs/014, 019 and 051: defend the place, not each path.
A fourth caller added next year is covered without anyone remembering this.
"""
from __future__ import annotations

import pytest

from backend.src.services.test_signal import test_signal_service as svc


@pytest.fixture
def engine():
    return svc.TestSignalEngine(bridge=None)


class TestItRefusesToStart:
    def test_start_leaves_it_stopped(self, engine):
        engine.start()

        assert engine.is_running is False

    def test_it_starts_no_background_tasks(self, engine):
        """The point of the exercise. A running cycle loop is bridge calls,
        database writes and an analysis row a minute, for a panel nobody can
        open."""
        engine.start()

        assert engine._cycle_task is None
        assert engine._outcome_task is None
        assert engine._velocity_task is None
        assert engine._watchdog_task is None

    def test_its_status_says_why(self, engine):
        """Silently declining to start is how this became invisible in the
        first place. Whatever reads the status must be able to say so."""
        engine.start()

        detail = engine.status_detail.lower()
        assert engine.status == "stopped"
        # The DATE, not the words. "panel" and "removed" both also appear in
        # the constant name the message cites, so asserting on either passed
        # against a message that had lost its explanation entirely. The date is
        # carried only by the explanation.
        assert "2026-09-02" in detail
        assert "bugs/046" in detail

    def test_repeated_starts_stay_refused(self, engine):
        """app.py's watchdog calls start() every five minutes."""
        for _ in range(3):
            engine.start()

        assert engine.is_running is False

    def test_stop_on_an_engine_that_never_started_is_harmless(self, engine):
        engine.start()
        engine.stop()

        assert engine.is_running is False


class TestRevivingItIsOneEdit:
    @pytest.mark.asyncio
    async def test_clearing_the_flag_lets_it_start_again(self, engine, monkeypatch):
        """Not a deletion — the service, its database and its history are all
        intact, and the sync server still binds engines by the fixed
        (breakout, bounce, reversal) order. Flip this constant and it runs.
        Asserted so that the way back is a fact rather than a claim in a
        comment."""
        monkeypatch.setattr(svc, "PANEL_REMOVED", False)

        engine.start()   # needs a running loop: start() creates its tasks

        assert engine.is_running is True
        engine.stop()

    def test_the_flag_is_on_by_default(self):
        assert svc.PANEL_REMOVED is True
