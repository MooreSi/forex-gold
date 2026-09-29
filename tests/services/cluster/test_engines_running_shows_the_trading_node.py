"""The Signal Generator tab says whether the TRADING node's engines run.

In Remote mode Start/Stop already go to the VPS, but /api/engines/state read
"running" off this machine's own instances. On 2026-09-28 the Mac's panel said
Reversal was running (the Mac's copy) while the VPS heartbeat said
`reversal_engine: false`, so the button offered Stop for an engine that was
off where it mattered.

The heartbeat names the engine "reversal_engine"; the panel's id is
"reversal". The mapping is part of the property.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster import remote_control as rc


class _Peer:
    def __init__(self, status):
        self.remote_status = status


@pytest.fixture
def node(monkeypatch):
    state = {"remote": False, "centralized": False,
             "peer": _Peer({"engines": {"breakout": True, "reversal_engine": False}})}
    monkeypatch.setattr(rc._facade, "_is_remote_active", lambda: state["remote"])
    monkeypatch.setattr(rc._facade, "_is_centralized_remote_mode",
                        lambda: state["centralized"])
    monkeypatch.setattr(rc._client, "get_instance", lambda: state["peer"])
    return state


LOCAL = {"breakout": False, "bounce": False, "reversal": True}


def test_locally_it_is_this_nodes_own_state(node):
    assert rc.engines_running(LOCAL) == LOCAL


def test_in_remote_mode_it_is_the_peers_heartbeat(node):
    node["remote"] = True

    got = rc.engines_running(LOCAL)

    assert got["breakout"] is True
    assert got["reversal"] is False


def test_under_centralized_generation_the_local_engines_are_the_live_ones(node):
    node["centralized"] = True
    node["remote"] = True

    assert rc.engines_running(LOCAL) == LOCAL


def test_an_engine_the_heartbeat_does_not_mention_reads_as_off(node):
    """Not as the local value: the local copy is stood down, and "running"
    for it would be the same misreport this fixes."""
    node["remote"] = True
    node["peer"] = _Peer({"engines": {"breakout": True}})

    assert rc.engines_running(LOCAL)["reversal"] is False


def test_no_heartbeat_yet_reads_as_off(node):
    node["remote"] = True
    node["peer"] = _Peer({})

    assert rc.engines_running(LOCAL) == {k: False for k in LOCAL}
