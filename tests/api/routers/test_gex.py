"""The Dashboard's GEX read: one GET, read-only. Nothing reaches a broker."""
from __future__ import annotations

from backend.src.api.routers import gex as gex_router


def test_the_card_reads_in_one_call(make_client, monkeypatch):
    calls = []

    async def _report():
        calls.append(1)
        return {"snapshot": None, "n_snapshots": 0}

    monkeypatch.setattr(gex_router.gex_ctl, "report_async", _report)
    body = make_client().get("/api/gex/latest").json()
    assert body == {"snapshot": None, "n_snapshots": 0}
    assert calls == [1]


def test_it_has_no_write_routes():
    assert {m for r in gex_router.router.routes for m in r.methods} == {"GET"}


def test_an_unreachable_trading_node_is_a_503_with_its_reason(make_client, monkeypatch):
    async def _report():
        raise gex_router.gex_ctl.RemoteControlFailed("The trading node could not be reached (x).")

    monkeypatch.setattr(gex_router.gex_ctl, "report_async", _report)
    resp = make_client().get("/api/gex/latest")
    assert resp.status_code == 503
    assert "could not be reached" in resp.text
