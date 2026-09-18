"""Is trading stopped, why, and until when — for the header to say so.

Two independent things can stop automated entries and they are stored
separately: the **risk governor** writes `trade_pause_until` with
`risk_halt_reason`, and the **circuit breaker** writes
`circuit_breaker_active_until` onto the risk-settings row after a losing streak.

The NiceGUI header badge read both. The React header read only the governor
between 2026-09-18 and this module, so **a tripped circuit breaker was
invisible on every screen except Settings > Diagnostics** — an operator looking
at the header would believe automated entries were running when they were
being refused.

**This is a READ, and deliberately not the enforcement path.**
`governor.is_trading_paused()` is the last line of defence: `open_trade` checks
it before both send paths and it fails CLOSED on an unreadable database. That
function is untouched. This one exists so a screen can explain a halt, and it
fails the other way — an error here costs the explanation, never the halt.

Nothing here reaches a broker or a real database.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.risk import pause_status


@pytest.fixture
def state(monkeypatch):
    """The two stores, in memory and independently settable."""
    now = time.time()
    s = {
        "paused": False,
        "reason": "",
        "pause_until": 0.0,
        "breaker": {"is_active": False, "active_until": 0.0,
                    "consec_losses": 0, "losses_threshold": 3},
        "now": now,
    }
    monkeypatch.setattr(pause_status._governor, "is_trading_paused",
                        lambda: s["paused"])
    monkeypatch.setattr(pause_status._governor, "halt_reason",
                        lambda: s["reason"] if s["paused"] else "")
    monkeypatch.setattr(pause_status, "_pause_until", lambda: s["pause_until"])
    monkeypatch.setattr(pause_status._breaker, "get_circuit_breaker_state",
                        lambda: dict(s["breaker"]))
    return s


class TestNothingIsStopped:
    def test_it_reports_not_paused(self, state):
        assert pause_status.summary()["paused"] is False

    def test_and_says_nothing_rather_than_something_empty(self, state):
        """The header renders the reason when there is one. A blank badge is a
        badge that reads as a bug."""
        out = pause_status.summary()

        assert out["reason"] == ""
        assert out["until"] is None
        assert out["source"] == ""


class TestTheGovernor:
    def test_a_governor_halt_is_reported_with_its_reason(self, state):
        state.update(paused=True, reason="daily loss limit reached",
                     pause_until=state["now"] + 3600)

        out = pause_status.summary()

        assert out["paused"] is True
        assert out["reason"] == "daily loss limit reached"
        assert out["source"] == "governor"

    def test_it_says_when_trading_resumes(self, state):
        """A halted account that reports no resume time leaves the operator
        watching a screen to find out."""
        until = state["now"] + 3600
        state.update(paused=True, reason="drawdown", pause_until=until)

        assert pause_status.summary()["until"] == pytest.approx(until)

    def test_a_halt_with_no_stored_reason_still_reports_the_halt(self, state):
        """`risk_halt_reason` is written beside the pause but an older row may
        not have one. Reporting "not paused" because the TEXT is missing would
        be the worst possible reading."""
        state.update(paused=True, reason="", pause_until=state["now"] + 60)

        out = pause_status.summary()

        assert out["paused"] is True
        assert out["reason"], "it must say something rather than nothing"


class TestTheCircuitBreaker:
    def test_a_tripped_breaker_is_reported_even_though_the_governor_is_quiet(
        self, state,
    ):
        """The gap this module was written for. The breaker writes a different
        key, so the governor knows nothing about it."""
        state["breaker"] = {"is_active": True, "active_until": state["now"] + 1800,
                            "consec_losses": 3, "losses_threshold": 3}

        out = pause_status.summary()

        assert out["paused"] is True
        assert out["source"] == "circuit-breaker"

    def test_it_says_what_tripped_it(self, state):
        state["breaker"] = {"is_active": True, "active_until": state["now"] + 1800,
                            "consec_losses": 4, "losses_threshold": 3}

        reason = pause_status.summary()["reason"]

        assert "4 consecutive losing trades" in reason, (
            "the count is the actionable part — 'the breaker tripped' without "
            "it leaves the operator guessing how close they were")

    def test_an_enabled_but_untripped_breaker_is_not_a_halt(self, state):
        """Negative control. The breaker is usually enabled and not active, and
        reporting that as a halt would put a warning on the header for ever."""
        state["breaker"] = {"is_active": False, "active_until": 0.0,
                            "consec_losses": 1, "losses_threshold": 3}

        assert pause_status.summary()["paused"] is False


class TestBothAtOnce:
    def test_the_later_resume_time_is_the_one_that_matters(self, state):
        """Trading resumes when the LAST of them expires. Reporting the earlier
        one tells the operator to expect entries that will still be refused."""
        state.update(paused=True, reason="drawdown",
                     pause_until=state["now"] + 600)
        state["breaker"] = {"is_active": True, "active_until": state["now"] + 3600,
                            "consec_losses": 3, "losses_threshold": 3}

        assert pause_status.summary()["until"] == pytest.approx(state["now"] + 3600)

    def test_both_reasons_are_given(self, state):
        """Two different halts are two different things to fix."""
        state.update(paused=True, reason="drawdown",
                     pause_until=state["now"] + 600)
        state["breaker"] = {"is_active": True, "active_until": state["now"] + 3600,
                            "consec_losses": 3, "losses_threshold": 3}

        out = pause_status.summary()

        assert "drawdown" in out["reason"]
        assert "consecutive losing trades" in out["reason"]
        assert out["source"] == "both"


class TestItNeverTakesTheHeaderDown:
    def test_a_failing_governor_read_costs_the_explanation_not_the_page(
        self, state, monkeypatch,
    ):
        """This renders on every header refresh. It is NOT the enforcement
        path — `governor.is_trading_paused()` is, and that one fails closed.
        Here a failure must not blank the header."""
        def _boom():
            raise RuntimeError("database locked")

        monkeypatch.setattr(pause_status._governor, "is_trading_paused", _boom)

        out = pause_status.summary()

        assert out["paused"] is False
        assert out["reason"] == ""

    def test_a_failing_breaker_read_still_reports_the_governor(self, state, monkeypatch):
        """One unreadable source must not hide the other."""
        state.update(paused=True, reason="drawdown", pause_until=state["now"] + 60)

        def _boom():
            raise RuntimeError("no such column")

        monkeypatch.setattr(pause_status._breaker, "get_circuit_breaker_state", _boom)

        out = pause_status.summary()

        assert out["paused"] is True
        assert "drawdown" in out["reason"]


def test_it_does_not_reimplement_the_enforcement_check():
    """The one thing that must stay true. `open_trade` checks
    `governor.is_trading_paused()` before both send paths and it fails CLOSED;
    a second, laxer copy of that logic for display would eventually be the one
    somebody wires into a decision."""
    import inspect

    src = inspect.getsource(pause_status)

    assert "is_trading_paused()" in src, "it must ASK, not re-derive"
    assert "read_app_config_strict" not in src
