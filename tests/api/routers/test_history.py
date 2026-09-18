"""The Analysis tab, and the round-trip problem it was built around.

The NiceGUI page had three panels each polling the MT5 bridge independently.
At idle, `/history?days=365` ran 4.3 times a minute and a single page load
produced 388 bridge round-trips in 25 seconds (bugs/030). The React answer is
structural rather than a cache: one endpoint assembles the panels' data once,
and one shared poll reads it — so the first test here is that a full render is
ONE call into the broker, not three.

Nothing here places an order. `compute_mt5_performance` is a reporting read and
`tests/core/test_mt5_performance_is_reporting_only.py` is what keeps it that
way.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import history as history_router


@pytest.fixture
def analysis(monkeypatch, sentinel_engine):
    state = {
        "performance": {"balance": 10_250.44, "closed_trades": 37,
                        "win_rate_pct": 56.8, "total_net_pnl": 412.19,
                        "max_drawdown_pct": 8.4},
        "hourly": {(0, 13): {"pnl": 42.5, "n": 3, "avg": 14.17},
                   (4, 9): {"pnl": -12.0, "n": 2, "avg": -6.0}},
        "channels": [{"source": "GoldSignals", "trades": 20, "wins": 12,
                      "win_rate": 60.0, "net_pnl": 310.0, "paused": False}],
        "ladder": {"scale_out": {"reach": 2.4, "n": 18}},
        "perf_calls": [],
        "recomputed": [],
        "paused": [],
    }

    async def _performance(days):
        state["perf_calls"].append(days)
        return state["performance"]

    sentinel_engine.compute_mt5_performance = _performance
    monkeypatch.setattr(history_router.history_ctl, "get_hourly_pnl_grid",
                        lambda days: state["hourly"])
    monkeypatch.setattr(history_router.history_ctl, "get_channel_scorecard",
                        lambda days: state["channels"])
    monkeypatch.setattr(history_router.history_ctl, "strategy_ladder_reach",
                        lambda days: state["ladder"])
    monkeypatch.setattr(history_router.history_ctl, "session_for_hour",
                        lambda h: "London" if 7 <= h < 12 else "NY")
    monkeypatch.setattr(history_router.history_ctl, "recompute_channel_performance",
                        lambda days: state["recomputed"].append(days))
    monkeypatch.setattr(history_router.history_ctl, "set_channel_paused",
                        lambda source, paused: state["paused"].append((source, paused)))
    return state


# ── One read, not four ───────────────────────────────────────────────────────

def test_a_full_render_costs_one_trip_into_the_broker(make_client, analysis):
    """The bugs/030 lesson, expressed as an assertion. Three panels polling
    independently is what produced 388 round-trips in 25 seconds."""
    make_client().get("/api/history/state?days=30")

    assert analysis["perf_calls"] == [30]


def test_every_panel_is_served_by_that_one_call(make_client, analysis):
    body = make_client().get("/api/history/state?days=30").json()

    assert body["performance"]["closed_trades"] == 37
    assert len(body["hourly"]) == 2
    assert body["channels"][0]["source"] == "GoldSignals"
    assert body["ladder"]["scale_out"]["reach"] == 2.4


def test_the_window_reaches_every_reader(make_client, analysis, monkeypatch):
    """A window applied to some panels and not others produces a screen where
    the heatmap and the scorecard describe different months."""
    seen = {}
    monkeypatch.setattr(history_router.history_ctl, "get_hourly_pnl_grid",
                        lambda days: seen.setdefault("hourly", days) and {})
    monkeypatch.setattr(history_router.history_ctl, "get_channel_scorecard",
                        lambda days: seen.setdefault("channels", days) and [])
    monkeypatch.setattr(history_router.history_ctl, "strategy_ladder_reach",
                        lambda days: seen.setdefault("ladder", days) and {})

    make_client().get("/api/history/state?days=365")

    assert seen == {"hourly": 365, "channels": 365, "ladder": 365}
    assert analysis["perf_calls"] == [365]


# ── The hourly grid ──────────────────────────────────────────────────────────

def test_the_grid_arrives_as_rows_with_weekday_and_hour_as_numbers(make_client, analysis):
    """A tuple is not a JSON key. Encoding it as "0-13" would leave the browser
    parsing it back, in one place, and getting it wrong there."""
    rows = make_client().get("/api/history/state").json()["hourly"]

    assert rows[0] == {"weekday": 0, "hour": 13, "session": "NY",
                       "pnl": 42.5, "n": 3, "avg": 14.17}


def test_the_grid_is_sorted_so_the_heatmap_can_render_it_straight(make_client, analysis):
    rows = make_client().get("/api/history/state").json()["hourly"]

    assert [(r["weekday"], r["hour"]) for r in rows] == [(0, 13), (4, 9)]


def test_each_hour_carries_the_session_the_service_names(make_client, analysis):
    """The session label is the service's decision, not the browser's. Two
    implementations of "is 13:00 the NY session" is one too many."""
    rows = make_client().get("/api/history/state").json()["hourly"]

    assert {r["hour"]: r["session"] for r in rows} == {13: "NY", 9: "London"}


def test_a_malformed_grid_key_is_skipped_rather_than_taking_the_tab_down(
    make_client, analysis,
):
    """Negative control on the unpacking. One bad key should cost one cell."""
    analysis["hourly"] = {(0, 13): {"pnl": 1.0, "n": 1, "avg": 1.0}, "junk": {}}

    rows = make_client().get("/api/history/state").json()["hourly"]

    assert len(rows) == 1


# ── When the broker cannot answer ────────────────────────────────────────────

def test_no_broker_data_is_an_empty_object_not_a_zeroed_one(make_client, analysis):
    """`compute_mt5_performance` returns {} when the bridge fails. Filling it
    with zeros here would render a flat month that never happened."""
    analysis["performance"] = {}

    body = make_client().get("/api/history/state").json()

    assert body["performance"] == {}
    # The rest of the tab still works: those come from the local database.
    assert len(body["hourly"]) == 2


# ── Writes ───────────────────────────────────────────────────────────────────

def test_recomputing_is_a_button_and_returns_the_new_scorecard(make_client, analysis):
    """It walks every trade in the window — not something to do every fifteen
    seconds behind the operator's back."""
    body = make_client().post("/api/history/recompute?days=90").json()

    assert analysis["recomputed"] == [90]
    assert body["channels"][0]["source"] == "GoldSignals"


def test_reading_the_state_never_recomputes(make_client, analysis):
    """Negative control for the test above."""
    make_client().get("/api/history/state")

    assert analysis["recomputed"] == []


def test_pausing_a_channel_forwards_the_flag(make_client, analysis):
    make_client().put("/api/history/channel-paused",
                      json={"source": "GoldSignals", "paused": True})

    assert analysis["paused"] == [("GoldSignals", True)]


def test_unpausing_forwards_false_rather_than_omitting_it(make_client, analysis):
    """A toggle that only ever sends True can pause a channel and never
    restore it."""
    make_client().put("/api/history/channel-paused",
                      json={"source": "GoldSignals", "paused": False})

    assert analysis["paused"] == [("GoldSignals", False)]


def test_a_window_beyond_ten_years_is_rejected(make_client, analysis):
    r = make_client().get("/api/history/state?days=99999")

    assert r.status_code == 422
    assert analysis["perf_calls"] == []
