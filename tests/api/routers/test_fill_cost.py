"""The Dashboard's Fill cost read: one GET, read-only. Nothing reaches a broker."""
from __future__ import annotations

from backend.src.api.routers import fill_cost as fill_router


def test_the_card_reads_in_one_call_with_its_window(make_client, monkeypatch):
    seen = []

    async def _report(days):
        seen.append(days)
        return {"days": days, "n": 3, "median_cost_pts": 0.9, "by_strategy": []}

    monkeypatch.setattr(fill_router.fill_ctl, "report_async", _report)
    body = make_client().get("/api/fills/cost?days=7").json()
    assert body["median_cost_pts"] == 0.9
    assert seen == [7]


def test_the_window_is_bounded(make_client, monkeypatch):
    async def _report(days):
        return {"days": days}

    monkeypatch.setattr(fill_router.fill_ctl, "report_async", _report)
    assert make_client().get("/api/fills/cost?days=0").status_code == 422
    assert make_client().get("/api/fills/cost?days=400").status_code == 422


def test_it_has_no_write_routes():
    assert {m for r in fill_router.router.routes for m in r.methods} == {"GET"}


def test_an_unreachable_trading_node_is_a_503_with_its_reason(make_client, monkeypatch):
    async def _report(days):
        raise fill_router.fill_ctl.RemoteControlFailed("The trading node could not be reached (x).")

    monkeypatch.setattr(fill_router.fill_ctl, "report_async", _report)
    resp = make_client().get("/api/fills/cost")
    assert resp.status_code == 503
    assert "could not be reached" in resp.text
