"""The header badge must not read "Trading Active" while the Trading Markets
toggles refuse every automated entry.

Reported 2026-10-08: London and New York switched off on the Schedule page
during the London/NY overlap, and the header still said "Trading Active".
`is_session_allowed` -- the gate every automated route calls -- was refusing
entries; the badge never asked it.

Nothing here places an order or reaches a broker.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import trading_status as ts

_NOW = 1_758_000_000.0


@pytest.fixture
def lab(monkeypatch):
    state = {
        "breaker": {"is_active": False, "active_until": 0.0, "losses_threshold": 3},
        "pause_until": 0.0,
        "target": {"reached": False},
        "news": {"paused": False},
        "session": (True, "overlap"),
    }
    monkeypatch.setattr(ts, "_paired_vps_view", lambda: None)
    monkeypatch.setattr(ts, "_breaker_state", lambda: dict(state["breaker"]))
    monkeypatch.setattr(ts, "_trade_pause_until", lambda: state["pause_until"])
    monkeypatch.setattr(ts, "_halt_reason", lambda: "")
    monkeypatch.setattr(ts, "_daily_target_state", lambda: dict(state["target"]))
    monkeypatch.setattr(ts, "_news_state", lambda: dict(state["news"]))
    monkeypatch.setattr(ts, "_session_state", lambda: state["session"])
    monkeypatch.setattr(ts.time, "time", lambda: _NOW)
    return state


def test_an_allowed_session_is_still_the_all_clear(lab):
    assert ts.badge()["state"] == "ok"


def test_a_switched_off_session_is_not_trading_active(lab):
    lab["session"] = (False, "overlap")

    out = ts.badge()

    assert out["state"] == "session_closed"
    assert out["label"] != "Trading Active"
    assert "Trading Markets" in out["detail"]


def test_it_names_the_session_that_is_off(lab):
    lab["session"] = (False, "overlap")

    assert "London/New York overlap" in ts.badge()["detail"]


def test_a_resume_cannot_open_a_switched_off_market(lab):
    """resume_all clears none of the toggles, so offering it would be a
    button that visibly does nothing."""
    lab["session"] = (False, "london")

    assert ts.badge()["can_resume"] is False


def test_the_weekend_reads_market_closed(lab):
    lab["session"] = (False, "closed")

    assert ts.badge()["label"] == "Market Closed"


def test_the_uncovered_evening_reads_outside_trading_hours(lab):
    lab["session"] = (False, "off")

    assert ts.badge()["label"] == "Outside Trading Hours"


def test_a_halt_still_outranks_it(lab):
    lab["session"] = (False, "london")
    lab["pause_until"] = _NOW + 1800

    assert ts.badge()["state"] == "halted"


def test_a_reached_target_still_outranks_it(lab):
    """The target needs a Resume; the session does not. The one a human must
    act on is the one the badge shows."""
    lab["session"] = (False, "london")
    lab["target"] = {"reached": True, "pnl": 300.0, "target": 250.0}

    assert ts.badge()["state"] == "profit_target"


def test_it_outranks_a_news_blackout(lab):
    """A blackout lifts in minutes; with the market switched off, trading
    would still not resume when it does."""
    lab["session"] = (False, "london")
    lab["news"] = {"paused": True, "detail": "NFP", "resume_ts": _NOW + 300}

    assert ts.badge()["state"] == "session_closed"


def test_a_broken_session_read_does_not_blank_the_badge(lab, monkeypatch):
    def _boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(ts, "_session_state", _boom)

    assert ts.badge()["state"] == "ok"


def test_the_session_read_reaches_the_real_gate(fresh_db):
    """Every test above replaces it, and `_safe` would swallow a wrong import
    path as "not holding anything"."""
    allowed, name = ts._session_state()

    assert isinstance(allowed, bool) and isinstance(name, str)


def test_switching_both_off_is_seen_by_the_real_gate(fresh_db, monkeypatch):
    """End to end through the real settings row and the real gate."""
    from backend.src.services.dpm import engine
    from backend.src.services.risk.schedule_options import set_markets
    monkeypatch.setattr(engine, "detect_session", lambda: "overlap")
    monkeypatch.setattr(engine, "is_weekly_market_closed", lambda now=None: False)

    set_markets({"london": False, "new_york": False})

    assert ts._session_state() == (False, "overlap")
