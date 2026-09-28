"""Why the VPS's commit is unknown, said plainly (owner, 2026-09-28).

The Remote tab showed "VPS unknown ... (not connected, or an older version)"
while the VPS was connected and trading: it ran bcd8c92, which predates the
commit in the heartbeat. Connected-but-older and not-connected need different
actions (update it once by other means, versus fix the link), so the report
says which.

Nothing here reaches a socket: the sync client is a stand-in.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import _update_sync as us
from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import protocol as P


class _Client:
    def __init__(self, state, status):
        self.conn_state = state
        self.remote_status = status
        self.last_update_result = None


@pytest.fixture
def peer(monkeypatch):
    def _set(state, status):
        monkeypatch.setattr(sc, "get_instance", lambda: _Client(state, status))
    return _set


def test_a_connected_vps_without_a_commit_is_an_older_build(peer):
    peer(P.CONN_CONNECTED, {"type": P.MSG_STATUS_HEARTBEAT, "balance": 903.62})

    got = us.current_version_report()

    assert got["remote"] is None
    assert got["remote_reason"] == "older_build"


def test_no_link_is_not_blamed_on_the_vps_version(peer):
    peer(P.CONN_DISCONNECTED, {"commit": "a" * 40})

    assert us.current_version_report()["remote_reason"] == "not_connected"


def test_a_reported_commit_says_so(peer):
    peer(P.CONN_CONNECTED, {"commit": "a" * 40, "git_version": "2.45.1"})

    got = us.current_version_report()

    assert got["remote"]["commit"] == "a" * 40
    assert got["remote_reason"] == "reported"
