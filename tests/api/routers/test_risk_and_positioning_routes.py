"""The two read-only endpoints docs/todo/024 adds.

`/api/history/monte-carlo` forwards the window and horizon to the analytics
service; `/api/news/positioning` returns the COT and GLD snapshot. Both are
one controller call, and neither reaches a broker or the network here: the
controller functions are replaced.
"""
from __future__ import annotations

from backend.src.api.routers import history as history_router
from backend.src.api.routers import news as news_router


def test_monte_carlo_forwards_days_and_horizon(make_client, monkeypatch):
    seen = []

    async def _mc(eng, days, horizon):
        seen.append((days, horizon))
        return {"trades": 60, "horizon": horizon, "note": None}

    monkeypatch.setattr(history_router.monte_carlo_ctl, "monte_carlo", _mc)

    body = make_client().get("/api/history/monte-carlo?days=90&horizon=200").json()

    assert seen == [(90, 200)]
    assert body["horizon"] == 200


def test_monte_carlo_horizon_defaults_to_the_window(make_client, monkeypatch):
    seen = []

    async def _mc(eng, days, horizon):
        seen.append((days, horizon))
        return {}

    monkeypatch.setattr(history_router.monte_carlo_ctl, "monte_carlo", _mc)

    make_client().get("/api/history/monte-carlo?days=30")

    assert seen == [(30, None)]


def test_monte_carlo_refuses_an_absurd_horizon(make_client, monkeypatch):
    async def _mc(eng, days, horizon):
        raise AssertionError("should not be called")

    monkeypatch.setattr(history_router.monte_carlo_ctl, "monte_carlo", _mc)

    assert make_client().get("/api/history/monte-carlo?horizon=50000").status_code == 422


def test_positioning_returns_the_snapshot(make_client, monkeypatch):
    snap = {"cot": {"report_date": "2026-10-13", "mm_net": 107089}, "cot_error": None,
            "cot_fetched_at": 1.0, "gld": None, "gld_error": "down", "gld_fetched_at": None}

    async def _pos():
        return snap

    monkeypatch.setattr(news_router.news_ctl, "positioning", _pos)

    assert make_client().get("/api/news/positioning").json() == snap
