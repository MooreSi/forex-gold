"""The trading schedule and the daily profit target.

Neither places an order; both decide whether the engines are allowed to. Two
things carry history and both are asserted:

* **The schedule echo.** The service pads a schedule saved before the fourth
  window existed rather than discarding it, so what was sent and what is stored
  can legitimately differ — and the screen must show the second one.
* **"Resume past today's target" is for today only.** The owner asked for it on
  2026-09-16 precisely so it could not become a permanent switch. It clears at
  the day boundary, and the response says so.

Nothing here reaches a broker.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import schedule as schedule_router


@pytest.fixture
def clock(monkeypatch):
    state = {
        "schedule": {"mon": [{"start": "08:00", "end": "12:00", "enabled": True}]},
        "enabled": True,
        "target": 250.0,
        "daily": {"reached": False, "overridden": False, "pnl": 40.0, "target": 250.0},
        "clock_desc": {"label": "Broker time (UTC+3)", "offset_minutes": 180},
        "writes": [],
    }

    async def _daily():
        return state["daily"]

    monkeypatch.setattr(schedule_router.schedule_ctl, "get_trading_schedule",
                        lambda: state["schedule"])
    monkeypatch.setattr(schedule_router.schedule_ctl, "set_trading_schedule",
                        lambda s: state["writes"].append(("schedule", s)))
    monkeypatch.setattr(schedule_router.schedule_ctl, "is_trading_schedule_enabled",
                        lambda: state["enabled"])
    monkeypatch.setattr(schedule_router.schedule_ctl, "set_trading_schedule_enabled",
                        lambda e: state["writes"].append(("enabled", e)))
    monkeypatch.setattr(schedule_router.schedule_ctl, "get_daily_profit_target",
                        lambda: state["target"])
    monkeypatch.setattr(schedule_router.schedule_ctl, "set_daily_profit_target",
                        lambda t: state["writes"].append(("target", t)))
    monkeypatch.setattr(schedule_router.schedule_ctl, "daily_profit_target_state_async", _daily)
    monkeypatch.setattr(schedule_router.schedule_ctl, "resume_past_daily_profit_target",
                        lambda: state["writes"].append(("resume",)))
    monkeypatch.setattr(schedule_router.schedule_ctl, "describe_trading_clock",
                        lambda: state["clock_desc"])
    monkeypatch.setattr(schedule_router.schedule_ctl, "set_trading_clock_offset",
                        lambda m: state["writes"].append(("offset", m)))
    return state


def test_the_screen_reads_its_pieces_in_one_call(make_client, clock):
    body = make_client().get("/api/schedule/state").json()

    assert body["enabled"] is True
    assert body["daily_target"] == 250.0
    assert body["daily_state"]["pnl"] == 40.0
    assert body["schedule"]["mon"][0]["start"] == "08:00"


def test_the_state_names_which_clock_the_windows_are_in(make_client, clock):
    """simon-handover/017 asked exactly this. A schedule screen that does not
    answer it is describing hours in an unknown timezone."""
    body = make_client().get("/api/schedule/state").json()

    assert body["clock"]["label"] == "Broker time (UTC+3)"


def test_saving_the_schedule_echoes_what_was_STORED(make_client, clock):
    """The service pads a pre-2026-08-01 schedule rather than discarding it, so
    sent and stored are not always the same grid."""
    clock["schedule"] = {"mon": [{"start": "09:00", "end": "17:00", "enabled": True}]}

    body = make_client().put("/api/schedule/schedule", json={
        "schedule": {"mon": [{"start": "08:00", "end": "12:00", "enabled": True}]},
    }).json()

    assert body["schedule"]["mon"][0]["start"] == "09:00"
    assert clock["writes"][0][0] == "schedule"


def test_turning_the_schedule_off_forwards_false(make_client, clock):
    """A toggle that only ever sends True can enable a schedule and never
    disable it."""
    make_client().put("/api/schedule/enabled", json={"enabled": False})

    assert ("enabled", False) in clock["writes"]


def test_a_zero_target_is_allowed_because_zero_disables_the_gate(make_client, clock):
    """0 is the documented "off". Refusing it would leave no way to turn the
    whole-day gate off once it is on."""
    r = make_client().put("/api/schedule/daily-target", json={"target": 0})

    assert r.status_code == 200
    assert ("target", 0.0) in clock["writes"]


def test_a_negative_target_is_refused(make_client, clock):
    r = make_client().put("/api/schedule/daily-target", json={"target": -5})

    assert r.status_code == 400
    assert clock["writes"] == []


def test_resuming_says_it_is_for_today_only(make_client, clock):
    """The behaviour the owner asked for. A screen that presented this as a
    setting would be describing something the backend does not do."""
    body = make_client().post("/api/schedule/resume-today").json()

    assert ("resume",) in clock["writes"]
    assert "today only" in body["note"]
    assert "day boundary" in body["note"]


def test_resuming_reports_the_state_afterwards(make_client, clock):
    clock["daily"] = {"reached": True, "overridden": True, "pnl": 300.0, "target": 250.0}

    body = make_client().post("/api/schedule/resume-today").json()

    assert body["daily_state"]["overridden"] is True


def test_reading_the_state_never_resumes(make_client, clock):
    """Negative control. A poll that overrode the target would make the gate
    permanently inert."""
    make_client().get("/api/schedule/state")

    assert clock["writes"] == []


def test_the_clock_offset_is_forwarded_and_the_new_label_returned(make_client, clock):
    clock["clock_desc"] = {"label": "UTC", "offset_minutes": 0}

    body = make_client().put("/api/schedule/clock-offset", json={"minutes": 0}).json()

    assert ("offset", 0) in clock["writes"]
    assert body["clock"]["label"] == "UTC"


def test_resuming_is_not_reachable_by_GET(make_client, clock):
    assert make_client().get("/api/schedule/resume-today").status_code == 405
    assert clock["writes"] == []
