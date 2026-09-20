"""The header's trading-status badge, and what it is allowed to say.

The NiceGUI header carried one always-visible indicator for whether trading
is actually running. The React port had no equivalent, so the dashboard
showed nothing at all while entries were being held.

**Four states, in a fixed order, and the order is the design.** Each of the
lower three is a true statement that would be misleading on its own, which
is why "Circuit Breaker OK" is last and only reachable when nothing else
applies:

1. **Halted** -- the circuit breaker tripped, or a manual pause is in force.
   Needs a human to Resume and can last the day, so it outranks everything.
2. **Profit target reached** -- the day earned its target. Automated entries
   are held for the rest of the day. Ranks below a halt (that is a loss
   guard, this is a win) and above a blackout (that lifts itself in
   minutes, this needs a Resume).
3. **News blackout** -- entries are being held for a news window, which
   lifts itself.
4. **OK.**

Getting this wrong is not cosmetic: a badge reading "Circuit Breaker OK"
while every entry is being held is a false all-clear, which is the exact
complaint that put the news box and the profit-target state into the
NiceGUI header in the first place.

Nothing here places an order or reaches a broker.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import trading_status as ts

_NOW = 1_758_000_000.0


@pytest.fixture
def lab(monkeypatch):
    state = {
        "breaker": {"is_active": False, "active_until": 0.0,
                    "losses_threshold": 3},
        "pause_until": 0.0,
        "target": {"reached": False, "pnl": 0.0, "target": 250.0},
        "news": {"paused": False, "label": "", "detail": "",
                 "resume_ts": None, "mins_remaining": None},
        "halt_reason": "",
        "resets": [],
    }

    monkeypatch.setattr(ts, "_breaker_state", lambda: dict(state["breaker"]))
    monkeypatch.setattr(ts, "_trade_pause_until", lambda: state["pause_until"])
    monkeypatch.setattr(ts, "_daily_target_state", lambda: dict(state["target"]))
    monkeypatch.setattr(ts, "_news_state", lambda: dict(state["news"]))
    monkeypatch.setattr(ts, "_halt_reason", lambda: state["halt_reason"])
    monkeypatch.setattr(ts.time, "time", lambda: _NOW)
    return state


# ── The all-clear ────────────────────────────────────────────────────────────

def test_a_clear_system_reports_ok(lab):
    out = ts.badge()

    assert out["state"] == "ok"
    assert out["label"] == "Circuit Breaker OK"
    assert out["can_resume"] is False


# ── A halt outranks everything ───────────────────────────────────────────────

class TestAHalt:

    def test_a_tripped_breaker_halts(self, lab):
        lab["breaker"] = {"is_active": True, "active_until": _NOW + 3600,
                          "losses_threshold": 3}

        out = ts.badge()

        assert out["state"] == "halted"
        assert out["label"].startswith("Trading Paused until ")
        assert out["can_resume"] is True

    def test_a_manual_pause_halts(self, lab):
        lab["pause_until"] = _NOW + 1800

        assert ts.badge()["state"] == "halted"

    def test_a_pause_that_has_expired_does_not_halt(self, lab):
        """A stored timestamp in the past is not a pause. Reading it as one
        leaves the header saying trading is off while it runs."""
        lab["pause_until"] = _NOW - 60

        assert ts.badge()["state"] == "ok"

    def test_the_halt_shows_the_later_of_the_two_end_times(self, lab):
        """Both can be in force. Showing the earlier one tells the operator
        trading resumes before it does."""
        lab["breaker"] = {"is_active": True, "active_until": _NOW + 600,
                          "losses_threshold": 3}
        lab["pause_until"] = _NOW + 7200

        assert ts.badge()["until"] == _NOW + 7200

    def test_it_outranks_a_reached_profit_target(self, lab):
        lab["pause_until"] = _NOW + 1800
        lab["target"] = {"reached": True, "pnl": 300.0, "target": 250.0}

        assert ts.badge()["state"] == "halted"

    def test_the_reason_names_both_mechanisms(self, lab):
        lab["breaker"] = {"is_active": True, "active_until": _NOW + 600,
                          "losses_threshold": 3}
        lab["pause_until"] = _NOW + 600
        lab["halt_reason"] = "Daily loss limit reached"

        detail = ts.badge()["detail"]

        assert "Daily loss limit reached" in detail
        assert "3 consecutive losses" in detail


# ── The profit target ────────────────────────────────────────────────────────

class TestTheProfitTarget:

    def test_a_reached_target_takes_the_badge(self, lab):
        lab["target"] = {"reached": True, "pnl": 312.5, "target": 250.0}

        out = ts.badge()

        assert out["state"] == "profit_target"
        assert out["label"] == "Profit Target Reached"
        assert out["can_resume"] is True

    def test_the_figures_are_shown(self, lab):
        lab["target"] = {"reached": True, "pnl": 312.5, "target": 250.0}

        assert "312.50" in ts.badge()["detail"]
        assert "250.00" in ts.badge()["detail"]

    def test_it_outranks_a_news_blackout(self, lab):
        """A blackout lifts itself in minutes; the target holds for the rest
        of the day and needs a Resume."""
        lab["target"] = {"reached": True, "pnl": 312.5, "target": 250.0}
        lab["news"] = {"paused": True, "label": "NFP", "detail": "Non-farm",
                       "resume_ts": _NOW + 300, "mins_remaining": 5}

        assert ts.badge()["state"] == "profit_target"


# ── The news blackout ────────────────────────────────────────────────────────

class TestANewsBlackout:

    def test_it_takes_the_badge_over_the_all_clear(self, lab):
        """Entries ARE being held, so "Circuit Breaker OK" would be true and
        misleading at the same time."""
        lab["news"] = {"paused": True, "label": "NFP", "detail": "Non-farm payrolls",
                       "resume_ts": _NOW + 300, "mins_remaining": 5}

        out = ts.badge()

        assert out["state"] == "news_blackout"
        assert out["label"] == "News Blackout"
        assert out["detail"] == "Non-farm payrolls"

    def test_it_carries_the_resume_time_for_a_live_countdown(self, lab):
        """The countdown is computed in the browser against its own clock, so
        it moves between polls instead of freezing at whatever the calendar
        last said."""
        lab["news"] = {"paused": True, "label": "NFP", "detail": "",
                       "resume_ts": _NOW + 300, "mins_remaining": 5}

        assert ts.badge()["resume_ts"] == _NOW + 300

    def test_a_window_with_no_known_end_still_shows(self, lab):
        """The feed-down fallback knows a window is open, not when it ends.
        Showing nothing would hide a real hold."""
        lab["news"] = {"paused": True, "label": "", "detail": "",
                       "resume_ts": None, "mins_remaining": None}

        out = ts.badge()

        assert out["state"] == "news_blackout"
        assert out["resume_ts"] is None

    def test_a_blackout_cannot_be_resumed_by_hand(self, lab):
        """It lifts itself. Offering a Resume would imply otherwise."""
        lab["news"] = {"paused": True, "label": "NFP", "detail": "",
                       "resume_ts": _NOW + 300, "mins_remaining": 5}

        assert ts.badge()["can_resume"] is False


# ── Failures must not blank the header ───────────────────────────────────────

class TestItNeverRaises:

    def test_a_broken_calendar_costs_the_blackout_not_the_badge(self, lab,
                                                               monkeypatch):
        def _boom():
            raise RuntimeError("feed unreachable")

        monkeypatch.setattr(ts, "_news_state", _boom)

        assert ts.badge()["state"] == "ok"

    def test_a_broken_target_read_does_not_hide_a_halt(self, lab, monkeypatch):
        """The halt is the more important fact and is read first."""
        def _boom():
            raise RuntimeError("no db")

        monkeypatch.setattr(ts, "_daily_target_state", _boom)
        lab["pause_until"] = _NOW + 1800

        assert ts.badge()["state"] == "halted"

    def test_a_broken_breaker_read_reports_unknown_rather_than_ok(self, lab,
                                                                 monkeypatch):
        """The one failure that must NOT read as an all-clear: it is the
        state that decides whether orders are being blocked."""
        def _boom():
            raise RuntimeError("no db")

        monkeypatch.setattr(ts, "_breaker_state", _boom)

        out = ts.badge()

        assert out["state"] == "unknown"
        assert out["label"] == "Trading Status Unknown"


# ── Against the real collaborators ───────────────────────────────────────────
# Every test above replaces them, which is what makes them blind to a wrong
# import path -- and `_safe` would swallow exactly that and report "not
# holding anything". These call the unguarded wrappers, where a bad path
# raises where it can be seen. `schedule_options` shipped with one of these
# wrong and 17 green tests.

class TestTheRealWiring:

    def test_the_breaker_read_reaches_a_module_that_exists(self, fresh_db):
        assert isinstance(ts._breaker_state(), dict)

    def test_the_manual_pause_read_reaches_a_module_that_exists(self, fresh_db):
        assert isinstance(ts._trade_pause_until(), float)

    def test_the_daily_target_read_reaches_a_module_that_exists(self, fresh_db):
        assert isinstance(ts._daily_target_state(), dict)

    def test_the_news_read_reaches_a_module_that_exists(self, fresh_db):
        assert isinstance(ts._news_state(), dict)

    def test_the_halt_reason_read_reaches_a_module_that_exists(self, fresh_db):
        assert isinstance(ts._halt_reason(), str)

    def test_the_whole_badge_builds_against_the_real_system(self, fresh_db):
        out = ts.badge()

        assert out["state"] in {"ok", "halted", "profit_target",
                                "news_blackout", "unknown"}
        assert set(out) == {"state", "label", "detail", "until",
                            "resume_ts", "can_resume"}


# ── Resuming ─────────────────────────────────────────────────────────────────
# The badge can be reporting any of three mechanisms, so Resume clears
# whichever is actually in force. One that only cleared the governor would
# leave the operator pressing a button that visibly does nothing.

class TestResume:

    @pytest.fixture(autouse=True)
    def _spies(self, lab, monkeypatch):
        lab["cleared"] = []
        monkeypatch.setattr(
            "backend.src.services.risk.circuit_breaker_repo.reset_circuit_breaker",
            lambda: lab["cleared"].append("breaker"))
        monkeypatch.setattr("backend.src.services.risk.manual_pause.resume",
                            lambda: lab["cleared"].append("pause"))
        monkeypatch.setattr(
            "backend.src.services.risk.schedule.resume_past_daily_profit_target",
            lambda: lab["cleared"].append("target"))
        return lab

    def test_nothing_in_force_clears_nothing(self, lab):
        out = ts.resume_all()

        assert out["cleared"] == []
        assert lab["cleared"] == []

    def test_a_tripped_breaker_is_reset(self, lab):
        lab["breaker"] = {"is_active": True, "active_until": _NOW + 600,
                          "losses_threshold": 3}

        out = ts.resume_all()

        assert lab["cleared"] == ["breaker"]
        assert out["cleared"] == ["circuit_breaker"]

    def test_a_manual_pause_is_lifted(self, lab):
        lab["pause_until"] = _NOW + 600

        ts.resume_all()

        assert lab["cleared"] == ["pause"]

    def test_a_reached_target_is_lifted_for_today(self, lab):
        lab["target"] = {"reached": True, "pnl": 300.0, "target": 250.0}

        ts.resume_all()

        assert lab["cleared"] == ["target"]

    def test_all_three_at_once(self, lab):
        lab["breaker"] = {"is_active": True, "active_until": _NOW + 600,
                          "losses_threshold": 3}
        lab["pause_until"] = _NOW + 600
        lab["target"] = {"reached": True, "pnl": 300.0, "target": 250.0}

        out = ts.resume_all()

        assert lab["cleared"] == ["breaker", "pause", "target"]
        assert out["cleared"] == ["circuit_breaker", "manual_pause",
                                  "daily_profit_target"]

    def test_an_expired_pause_is_not_cleared(self, lab):
        """Re-read rather than trusted: clearing a halt that has since
        lifted itself writes for no reason."""
        lab["pause_until"] = _NOW - 60

        ts.resume_all()

        assert lab["cleared"] == []

    def test_one_failure_does_not_stop_the_others(self, lab, monkeypatch):
        """A breaker reset that throws must not leave a manual pause in
        force, or Resume half-works and the badge still says Paused."""
        monkeypatch.setattr(
            "backend.src.services.risk.circuit_breaker_repo.reset_circuit_breaker",
            lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
        lab["breaker"] = {"is_active": True, "active_until": _NOW + 600,
                          "losses_threshold": 3}
        lab["pause_until"] = _NOW + 600

        out = ts.resume_all()

        assert lab["cleared"] == ["pause"]
        assert out["cleared"] == ["manual_pause"]

    def test_the_response_carries_the_state_afterwards(self, lab):
        out = ts.resume_all()

        assert out["status"]["state"] == "ok"
