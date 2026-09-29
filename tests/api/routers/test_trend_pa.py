"""The Trend PA panel endpoints. The controller is faked; nothing here reaches
a broker or a peer."""
from __future__ import annotations

import pytest

from backend.src.api.routers import trend_pa as tpa_router


@pytest.fixture
def ctl(monkeypatch):
    state = {"refuse": None, "backtests": 0}

    async def _report():
        return {"where": "local", "live": {"n": 0}, "backtest": {"n": 533}}

    async def _backtest():
        if state["refuse"]:
            raise tpa_router.tpa_ctl.RemoteControlFailed(state["refuse"])
        state["backtests"] += 1
        return {"started": True, "where": "local"}

    monkeypatch.setattr(tpa_router.tpa_ctl, "report", _report)
    monkeypatch.setattr(tpa_router.tpa_ctl, "request_backtest", _backtest)
    return state


def test_the_report_reads_in_one_call(make_client, ctl):
    body = make_client().get("/api/engines/trend-pa/report").json()
    assert body["backtest"]["n"] == 533 and body["where"] == "local"


def test_the_backtest_is_a_post(make_client, ctl):
    assert make_client().get("/api/engines/trend-pa/backtest").status_code == 405
    resp = make_client().post("/api/engines/trend-pa/backtest")
    assert resp.status_code == 200 and ctl["backtests"] == 1


def test_an_unreachable_peer_is_a_readable_refusal(make_client, ctl):
    ctl["refuse"] = "The remote node could not be reached (timeout). Nothing changed."
    resp = make_client().post("/api/engines/trend-pa/backtest")
    assert resp.status_code >= 400 and "could not be reached" in resp.text


def test_the_engine_card_is_labelled(make_client, monkeypatch):
    from backend.src.api.routers import engines
    assert engines.ENGINE_LABELS["trend_pa"] == "Trend PA"
