"""The Set & Forget "Auto" switch (2026-09-24).

This router reads Auto's status and flips it on or off. **It places nothing
itself**: the orders come from `services/setforget/auto.run_forever`, started
once at app startup, through the same `open_manual_market_order` the page's
Execute button reaches. A request here must never reach a money method -- if
it did, pressing a button would be an order with none of Auto's refusals in
front of it.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import setforget_auto as auto_router

_MONEY_CALLS = ("open_manual_market_order", "open_manual_limit_order",
                "close_trade", "partial_close_trade", "open_trade_from_signal")


@pytest.fixture
def switch(monkeypatch):
    state = {"enabled": False, "writes": []}

    def set_enabled(on):
        state["writes"].append(on)
        state["enabled"] = on

    monkeypatch.setattr(auto_router.auto_ctl, "is_enabled",
                        lambda: state["enabled"])
    monkeypatch.setattr(auto_router.auto_ctl, "set_enabled", set_enabled)
    monkeypatch.setattr(auto_router.auto_ctl, "status",
                        lambda: {"enabled": state["enabled"], "decision": None,
                                 "reason": "", "last_run": None})
    return state


def test_it_reports_whether_auto_is_on(make_client, switch):
    body = make_client().get("/api/trading/setforget/auto").json()

    assert body["enabled"] is False


def test_it_switches_auto_on_and_off(make_client, switch):
    client = make_client()

    on = client.put("/api/trading/setforget/auto", json={"enabled": True}).json()
    off = client.put("/api/trading/setforget/auto", json={"enabled": False}).json()

    assert switch["writes"] == [True, False]
    assert on["enabled"] is True and off["enabled"] is False


def test_a_body_without_the_flag_is_refused(make_client, switch):
    res = make_client().put("/api/trading/setforget/auto", json={})

    assert res.status_code >= 400
    assert switch["writes"] == []


def test_no_request_here_reaches_a_money_method(make_client, switch,
                                                sentinel_engine):
    client = make_client()
    client.get("/api/trading/setforget/auto")
    client.put("/api/trading/setforget/auto", json={"enabled": True})

    assert [n for n, _, _ in sentinel_engine.calls if n in _MONEY_CALLS] == []
