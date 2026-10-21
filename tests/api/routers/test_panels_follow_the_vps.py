"""The Signal Generator, Trend PA and Brain routes answer from the VPS when
that is where the work happens (owner, 2026-10-21). The routing rules are in
tests/core/test_engine_panels_follow_the_engine_node.py; this is that the HTTP
routes use them, and that an unreachable VPS is a 503 rather than this node's
idle engines.

Nothing here reaches a broker or a peer: the sync client is a recorder.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster import peer_reports as pr


class _FakeClient:
    def __init__(self):
        self.asked: list[tuple] = []
        self.raises = None
        self.remote_signal_gen_stats: dict = {}

    async def request_peer_report(self, name, args, **kw):
        self.asked.append((name, args))
        if self.raises:
            raise self.raises
        return {"result": {"answered_by": "vps", "name": name}}


@pytest.fixture
def vps(monkeypatch):
    cli = _FakeClient()
    monkeypatch.setattr(pr, "trader_is_peer", lambda: True)
    monkeypatch.setattr(pr, "_centralized", lambda: False)
    monkeypatch.setattr(pr._client, "get_instance", lambda: cli)
    return cli


@pytest.mark.parametrize("path,name", [
    ("/api/engines/reversal/report", "reversal_panel"),
    ("/api/engines/breakout/report", "breakout_panel"),
    ("/api/engines/trend-pa/report", "trend_pa_panel"),
    ("/api/brain", "brain"),
])
def test_each_panel_is_the_vps_s(make_client, vps, path, name):
    body = make_client().get(path).json()

    assert body["answered_by"] == "vps"
    assert [n for n, _ in vps.asked] == [name]


@pytest.mark.parametrize("path", [
    "/api/engines/reversal/report", "/api/engines/breakout/report",
    "/api/engines/trend-pa/report", "/api/brain",
])
def test_an_unreachable_vps_is_a_503_not_this_nodes_idle_engine(make_client, vps, path):
    vps.raises = ConnectionError("not connected")

    res = make_client().get(path)

    assert res.status_code == 503
    assert "could not be reached" in res.json()["error"]["message"]


def test_the_reversal_report_carries_the_pro_model_of_the_node_that_built_it(
        make_client, monkeypatch):
    """The pro signal model's status was only on /state, which is always this
    node's. In the report, the VPS's build carries the VPS's model."""
    from backend.src.api.routers import engines as engines_router
    ctl = engines_router.reversal_ctl

    async def _empty():
        return {}

    for name in ("reversal_realised_pnl", "reversal_edge_stats", "reversal_ml_metrics",
                 "reversal_ml_summary", "reversal_chance_benchmark", "reversal_cross_asset"):
        monkeypatch.setattr(ctl, name, _empty)
    monkeypatch.setattr(ctl, "reversal_shadow_report", lambda: [])
    monkeypatch.setattr(ctl, "reversal_shadow_history", lambda limit: [])
    monkeypatch.setattr(ctl, "reversal_shadow_ledger", lambda limit: [])
    monkeypatch.setattr(ctl, "pro_model_status", lambda: {"fitted": True, "n": 412})

    body = make_client().get("/api/engines/reversal/report").json()

    assert body["pro_model"] == {"fitted": True, "n": 412}
