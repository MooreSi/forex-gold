"""The Reversal engine's learning curve reaches the screen.

`panel_data.ml_metrics()` has answered since the restructure and no endpoint
ever called it, so the React Signal Generator tab could not draw the "is it
learning?" chart the NiceGUI panel had -- the one that plots the rolling win
rate and realised R over the closed signals. The Breakout half of that chart
was already served by `/breakout/report`; this is the other half. Asked for on
2026-09-20.

The chart is a nicety on a panel that reports real money, so a metrics read
that fails must leave the rest of the report intact -- the same rule the
Breakout reads follow.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import engines as engines_router


@pytest.fixture
def lab(monkeypatch):
    state = {
        "metrics": {
            "n_data": 3,
            "win_flag_series": [1, 0, 1],
            "actual_r_series": [1.2, -1.0, 0.8],
            "signal_ids": ["a", "b", "c"],
            "accuracy": 0.66,
        },
        "summary": {"trained": True, "labeled_count": 3, "min_needed": 40},
    }

    async def _metrics():
        if isinstance(state["metrics"], Exception):
            raise state["metrics"]
        return dict(state["metrics"])

    async def _summary():
        return dict(state["summary"])

    async def _empty_dict():
        return {}

    monkeypatch.setattr(engines_router.reversal_ctl, "reversal_ml_metrics", _metrics)
    monkeypatch.setattr(engines_router.reversal_ctl, "reversal_ml_summary", _summary)
    monkeypatch.setattr(engines_router.reversal_ctl, "reversal_realised_pnl", _empty_dict)
    monkeypatch.setattr(engines_router.reversal_ctl, "reversal_edge_stats", _empty_dict)
    monkeypatch.setattr(engines_router.reversal_ctl, "reversal_shadow_report", lambda: [])
    monkeypatch.setattr(engines_router.reversal_ctl, "reversal_shadow_history",
                        lambda limit: [])
    return state


def test_the_per_signal_outcomes_reach_the_panel(make_client, lab):
    """Raw flags, not a cumulative mean: a cumulative mean cannot be
    un-averaged back into the rolling one the chart draws."""
    body = make_client().get("/api/engines/reversal/report").json()

    assert body["ml"]["metrics"]["win_flag_series"] == [1, 0, 1]


def test_the_realised_r_series_reaches_the_panel(make_client, lab):
    body = make_client().get("/api/engines/reversal/report").json()

    assert body["ml"]["metrics"]["actual_r_series"] == [1.2, -1.0, 0.8]


def test_how_far_from_training_it_is_comes_too(make_client, lab):
    """"Needs 37 more closed signals" is what an untrained engine's panel
    should say instead of an empty chart."""
    body = make_client().get("/api/engines/reversal/report").json()

    assert body["ml"]["summary"]["min_needed"] == 40


def test_metrics_that_cannot_be_read_do_not_take_the_report_down(make_client, lab):
    """This panel reports real money. A chart's data source is never worth
    the rest of it."""
    lab["metrics"] = RuntimeError("no ml table on a fresh install")

    r = make_client().get("/api/engines/reversal/report")

    assert r.status_code == 200
    assert r.json()["ml"]["metrics"] == {}
