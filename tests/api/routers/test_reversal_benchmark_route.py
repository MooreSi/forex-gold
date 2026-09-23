"""The chance benchmark reaches the Reversal report. docs/todo/reversal-engine/220.

It is research on a panel that reports real money, so a benchmark that cannot
be computed must leave the rest of the report intact -- the rule every other
guarded read on this route follows.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import engines as engines_router


@pytest.fixture
def lab(monkeypatch):
    state = {"benchmark": {"all": {"overall": {"n": 3, "verdict": "too few"}}}}

    async def _bench():
        if isinstance(state["benchmark"], Exception):
            raise state["benchmark"]
        return dict(state["benchmark"])

    async def _empty_dict():
        return {}

    ctl = engines_router.reversal_ctl
    monkeypatch.setattr(ctl, "reversal_chance_benchmark", _bench)
    monkeypatch.setattr(ctl, "reversal_ml_metrics", _empty_dict)
    monkeypatch.setattr(ctl, "reversal_ml_summary", _empty_dict)
    monkeypatch.setattr(ctl, "reversal_realised_pnl", _empty_dict)
    monkeypatch.setattr(ctl, "reversal_edge_stats", _empty_dict)
    monkeypatch.setattr(ctl, "reversal_shadow_report", lambda: [])
    monkeypatch.setattr(ctl, "reversal_shadow_history", lambda limit: [])
    return state


def test_the_benchmark_is_in_the_report(make_client, lab):
    body = make_client().get("/api/engines/reversal/report").json()
    assert body["benchmark"]["all"]["overall"]["verdict"] == "too few"


def test_a_benchmark_that_fails_does_not_take_the_report_down(make_client, lab):
    lab["benchmark"] = RuntimeError("no engine database")
    resp = make_client().get("/api/engines/reversal/report")
    assert resp.status_code == 200
    assert resp.json()["benchmark"] == {}
