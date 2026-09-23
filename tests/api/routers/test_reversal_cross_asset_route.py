"""The cross-asset chart data reaches the Reversal report.

docs/todo/reversal-engine/230. Research on a panel that reports real money,
so a read that fails leaves the rest of the report intact.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import engines as engines_router


@pytest.fixture
def lab(monkeypatch):
    state = {"cross_asset": {"peers": ["XAGUSD"], "fits": [], "daily_corr": [],
                             "coverage": {"measured": 3, "missing": 1}}}

    async def _xa():
        if isinstance(state["cross_asset"], Exception):
            raise state["cross_asset"]
        return dict(state["cross_asset"])

    async def _empty():
        return {}

    ctl = engines_router.reversal_ctl
    monkeypatch.setattr(ctl, "reversal_cross_asset", _xa)
    for name in ("reversal_chance_benchmark", "reversal_ml_metrics", "reversal_ml_summary",
                 "reversal_realised_pnl", "reversal_edge_stats"):
        monkeypatch.setattr(ctl, name, _empty)
    monkeypatch.setattr(ctl, "reversal_shadow_report", lambda: [])
    monkeypatch.setattr(ctl, "reversal_shadow_history", lambda limit: [])
    return state


def test_the_cross_asset_data_is_in_the_report(make_client, lab):
    body = make_client().get("/api/engines/reversal/report").json()
    assert body["cross_asset"]["coverage"] == {"measured": 3, "missing": 1}


def test_a_failed_read_does_not_take_the_report_down(make_client, lab):
    lab["cross_asset"] = RuntimeError("no engine database")
    resp = make_client().get("/api/engines/reversal/report")
    assert resp.status_code == 200
    assert resp.json()["cross_asset"] == {}
