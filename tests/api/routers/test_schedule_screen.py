"""The Schedule screen's own endpoints.

The React schedule screen showed a grid of start/end boxes. The NiceGUI page
it replaced also carried the Trading Markets toggles, a per-window profit
target and a per-source Override dropdown, and none of that had anywhere to
come from over HTTP. These are the endpoints that supply it.

Nothing here places an order. The markets toggle and the schedule grid both
decide whether the engines are ALLOWED to, which is why an unknown market
name is refused by name rather than ignored.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import schedule as schedule_router


@pytest.fixture
def lab(monkeypatch):
    state = {
        "markets": {"asia": True, "london": True, "new_york": False,
                    "session": "london", "allowed_now": True},
        "options": [{"value": "", "label": "— No Override —"},
                    {"value": "auto", "label": "Auto (AI-managed)"}],
        "channels": ["GoldSignals"],
        "market_writes": [],
        "offset_writes": [],
        "clock": {"configured": None, "effective": 60,
                  "following_machine": True, "label": "UTC+01:00"},
    }

    def _set_markets(fields):
        if "tokyo" in fields:
            raise ValueError("unknown trading market(s): tokyo")
        state["market_writes"].append(dict(fields))

    def _set_offset(minutes):
        if minutes is not None and abs(minutes) > 1440:
            raise ValueError("that is a typo, not a timezone")
        state["offset_writes"].append(minutes)

    ctl = schedule_router.schedule_ctl
    monkeypatch.setattr(ctl, "get_trading_schedule", lambda: {"monday": []})
    monkeypatch.setattr(ctl, "is_trading_schedule_enabled", lambda: True)
    monkeypatch.setattr(ctl, "get_daily_profit_target", lambda: 250.0)
    monkeypatch.setattr(ctl, "describe_trading_clock", lambda: dict(state["clock"]))
    monkeypatch.setattr(ctl, "trading_markets", lambda: dict(state["markets"]))
    # The state read takes all three through one guarded call, so that a
    # piece being unavailable costs itself rather than the whole screen.
    monkeypatch.setattr(ctl, "screen_extras", lambda: {
        "markets": dict(state["markets"]),
        "override_options": list(state["options"]),
        "channels": list(state["channels"]),
    })
    monkeypatch.setattr(ctl, "set_trading_markets", _set_markets)
    monkeypatch.setattr(ctl, "set_trading_clock_offset", _set_offset)

    async def _daily_state():
        return {"reached": False, "pnl": 0.0, "target": 250.0}

    monkeypatch.setattr(ctl, "daily_profit_target_state_async", _daily_state)
    return state


# ── One read for the whole screen ────────────────────────────────────────────

def test_the_state_read_carries_the_markets(make_client, lab):
    body = make_client().get("/api/schedule/state").json()

    assert body["markets"]["london"] is True
    assert body["markets"]["new_york"] is False


def test_the_state_read_carries_the_override_choices(make_client, lab):
    """Sent once with the state, not fetched per dropdown: there are up to
    28 windows on this screen and the list is identical for all of them."""
    body = make_client().get("/api/schedule/state").json()

    assert body["override_options"][0]["value"] == ""
    assert body["override_options"][1]["value"] == "auto"


def test_the_state_read_carries_the_channels_a_window_can_gate(make_client, lab):
    body = make_client().get("/api/schedule/state").json()

    assert body["channels"] == ["GoldSignals"]


def test_the_state_read_still_carries_the_grid_and_the_clock(make_client, lab):
    """The additions must not have displaced what was there."""
    body = make_client().get("/api/schedule/state").json()

    assert body["schedule"] == {"monday": []}
    assert body["enabled"] is True
    assert body["daily_target"] == 250.0
    assert body["clock"]["following_machine"] is True


# ── Switching a market ───────────────────────────────────────────────────────

def test_one_market_can_be_switched(make_client, lab):
    r = make_client().put("/api/schedule/markets",
                          json={"markets": {"london": False}})

    assert r.status_code == 200
    assert lab["market_writes"] == [{"london": False}]


def test_the_response_echoes_what_is_now_stored(make_client, lab):
    body = make_client().put("/api/schedule/markets",
                             json={"markets": {"london": False}}).json()

    assert "markets" in body
    assert set(body["markets"]) >= {"asia", "london", "new_york"}


def test_an_unknown_market_is_refused_by_name(make_client, lab):
    r = make_client().put("/api/schedule/markets",
                          json={"markets": {"tokyo": True}})

    assert r.status_code == 400
    assert "tokyo" in r.json()["error"]["message"]
    assert lab["market_writes"] == []


# ── The clock ────────────────────────────────────────────────────────────────

class TestTheClockOffset:

    def test_an_offset_can_be_set(self, make_client, lab):
        make_client().put("/api/schedule/clock-offset", json={"minutes": 330})

        assert lab["offset_writes"] == [330]

    def test_null_restores_the_machines_own_clock(self, make_client, lab):
        """The bug this endpoint had: `minutes` was typed `int`, so the
        default state -- the one every single-machine install wants -- was
        the one state the screen could not set. A user who picked a VPS
        offset once could never go back."""
        r = make_client().put("/api/schedule/clock-offset", json={"minutes": None})

        assert r.status_code == 200
        assert lab["offset_writes"] == [None]

    def test_an_omitted_offset_also_means_the_machine_clock(self, make_client, lab):
        make_client().put("/api/schedule/clock-offset", json={})

        assert lab["offset_writes"] == [None]

    def test_utc_itself_is_not_mistaken_for_the_machine_clock(self, make_client, lab):
        """Zero is falsy. Anything using `or` here puts a UTC+0 user back on
        the machine's clock, which on a VPS is the exact bug the control
        exists to fix."""
        make_client().put("/api/schedule/clock-offset", json={"minutes": 0})

        assert lab["offset_writes"] == [0]

    def test_a_nonsense_offset_is_refused_with_the_reason(self, make_client, lab):
        r = make_client().put("/api/schedule/clock-offset", json={"minutes": 99999})

        assert r.status_code == 400
        assert "typo" in r.json()["error"]["message"]
