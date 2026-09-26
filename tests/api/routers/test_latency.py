"""Settings > Latency endpoints (docs/todo/006).

GET is the passive read the tab polls: traces and the polling waits, no
probes. POST runs the probes, which is why it is a button and not a poll --
it asks Telegram, the bridge, the EA and the VPS each for a round trip.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import latency as router_mod


@pytest.fixture
def lab(monkeypatch):
    state = {"checks": []}

    async def _check(engine, reader):
        state["checks"].append((engine, reader))
        return {"local": {"probes": {"db": {"ok": True, "ms": 1.0}}}, "vps": None}

    monkeypatch.setattr(router_mod.latency_ctl, "check", _check)
    monkeypatch.setattr(router_mod.latency_ctl, "snapshot",
                        lambda: {"pipelines": {"telegram": {"hops": [], "recent": []}},
                                 "structural": [], "paired": False})
    return state


def test_the_read_runs_no_probe(make_client, lab):
    r = make_client().get("/api/settings/latency")

    assert r.status_code == 200
    assert "telegram" in r.json()["pipelines"]
    assert lab["checks"] == []


def test_the_check_runs_against_the_live_engine_and_reader(make_client, sentinel_engine, lab):
    reader = object()
    r = make_client(reader_provider=lambda: reader).post("/api/settings/latency/check")

    assert r.status_code == 200
    assert r.json()["local"]["probes"]["db"]["ms"] == 1.0
    assert lab["checks"][0] == (sentinel_engine, reader)


def test_a_check_is_not_a_get(make_client, lab):
    """A GET that fans out to four remote services would run on every poll."""
    assert make_client().get("/api/settings/latency/check").status_code == 405
