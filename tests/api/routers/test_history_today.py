"""`today=true` on the history endpoints: today on the trading clock.

The dashboard's "Today" window (owner, 2026-10-10). Without the flag both
endpoints behave exactly as before; `test_history.py` pins that.

Nothing here reaches a broker: the engine is the sentinel, its performance
read and the trade table are recorded stubs.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import history as history_router
from backend.src.services.risk import clock

DAY_START = 1_791_586_800.0


@pytest.fixture
def recorded(monkeypatch, sentinel_engine):
    seen = {"perf": [], "table": []}

    async def _performance(days, since_ts=None):
        seen["perf"].append((days, since_ts))
        return {"total_net_pnl": 0.0}

    async def _table(engine, days, today=False):
        seen["table"].append((days, today))
        return {"rows": [], "error": None,
                "curve": {"points": [], "net": 0, "peak": 0,
                          "max_drawdown": 0, "trades": 0}}

    sentinel_engine.compute_mt5_performance = _performance
    monkeypatch.setattr(clock, "day_start_ts", lambda: DAY_START, raising=False)
    for name in ("get_hourly_pnl_grid", "strategy_ladder_reach"):
        monkeypatch.setattr(history_router.history_ctl, name, lambda days: {})
    monkeypatch.setattr(history_router.history_ctl, "get_channel_scorecard",
                        lambda days: [])
    monkeypatch.setattr(history_router.history_ctl._trade_table,
                        "closed_trades", _table)
    return seen


def test_today_measures_performance_from_local_midnight(make_client, recorded):
    r = make_client().get("/api/history/state?days=1&today=true")
    assert r.status_code == 200
    (days, since), = recorded["perf"]
    assert since == DAY_START
    assert days >= 2, "the last 24 hours do not always reach back to midnight"


def test_without_today_performance_is_asked_as_before(make_client, recorded):
    make_client().get("/api/history/state?days=1")
    assert recorded["perf"] == [(1, None)]


def test_today_reaches_the_trade_table(make_client, recorded):
    make_client().get("/api/history/trades?days=1&today=true")
    assert recorded["table"] == [(1, True)]


def test_the_response_says_which_window_it_answered(make_client, recorded):
    body = make_client().get("/api/history/state?days=1&today=true").json()
    assert body["today"] is True
