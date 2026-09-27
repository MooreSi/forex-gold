"""The per-channel daily loss cap's endpoints.

The read is the Schedule screen's card: the default cap, the per-channel
overrides and, per channel, today's P&L and whether entries are being held.
The write stores both settings together and echoes what was stored. A bad
value is refused with the service's own reason, because "The cap for GOLD X
must be 0 (off) or a positive dollar amount" is something the operator can
fix and "invalid" is not.

Nothing here reaches a broker: the controller is replaced with fakes.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import channel_loss_cap as cap_router


@pytest.fixture
def svc(monkeypatch):
    state = {
        "stored": {"default_cap": 100.0, "overrides": {"GOLD X": 0.0},
                   "channels": [{"channel": "GOLD X", "cap": 0.0,
                                 "day_pnl": -40.0, "held": False}],
                   "error": ""},
        "writes": [],
        "refuse": None,
    }

    async def _state():
        return state["stored"]

    def _set(default_cap, overrides):
        if state["refuse"]:
            raise ValueError(state["refuse"])
        state["writes"].append((default_cap, overrides))
        state["stored"] = {**state["stored"], "default_cap": default_cap,
                           "overrides": overrides}

    monkeypatch.setattr(cap_router.cap_ctl, "state_async", _state)
    monkeypatch.setattr(cap_router.cap_ctl, "set_caps", _set)
    return state


def test_the_card_reads_in_one_call(make_client, svc):
    body = make_client().get("/api/channel-loss-cap").json()
    assert body["default_cap"] == 100.0
    assert body["overrides"] == {"GOLD X": 0.0}
    assert body["channels"][0]["day_pnl"] == -40.0


def test_a_write_stores_both_settings_and_echoes_the_state(make_client, svc):
    resp = make_client().put(
        "/api/channel-loss-cap",
        json={"default_cap": 150.0, "overrides": {"GOLD Y": 60.0}},
    )
    assert resp.status_code == 200
    assert svc["writes"] == [(150.0, {"GOLD Y": 60.0})]
    assert resp.json()["default_cap"] == 150.0


def test_a_refused_value_reaches_the_operator_by_name(make_client, svc):
    svc["refuse"] = "The cap for GOLD Y must be 0 (off) or a positive dollar amount."
    resp = make_client().put(
        "/api/channel-loss-cap", json={"default_cap": 0, "overrides": {"GOLD Y": -5}},
    )
    assert resp.status_code == 400
    assert "GOLD Y" in resp.text
    assert svc["writes"] == []


def test_the_schedule_screen_read_carries_the_card(make_client, monkeypatch):
    from backend.src.api.routers import schedule as schedule_router
    ctl = schedule_router.schedule_ctl

    async def _none():
        return {"reached": False}

    async def _card():
        return {"default_cap": 80.0, "overrides": {}, "channels": [], "error": ""}

    monkeypatch.setattr(ctl, "get_trading_schedule", lambda: {})
    monkeypatch.setattr(ctl, "is_trading_schedule_enabled", lambda: False)
    monkeypatch.setattr(ctl, "get_daily_profit_target", lambda: 0.0)
    monkeypatch.setattr(ctl, "daily_profit_target_state_async", _none)
    monkeypatch.setattr(ctl, "describe_trading_clock", lambda: {})
    monkeypatch.setattr(ctl, "screen_extras", lambda: {})
    monkeypatch.setattr(ctl, "channel_loss_cap_state_async", _card)

    body = make_client().get("/api/schedule/state").json()
    assert body["channel_loss_cap"]["default_cap"] == 80.0
