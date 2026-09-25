"""A new pairing token takes effect on the running VPS at once.

Reported 2026-09-25: the paired machine said "bad token". The VPS kept its
6.1-era token (Make this node a VPS keeps an existing one, so a paired machine
is not silently unpaired), which meant no token was ever shown. The way to get
one, generating a new token, had its own fault: the running sync server holds
the token it started with, so the new one was refused until the app restarted.

Nothing here opens a socket: the check is `SyncServer._check_token`, the one
function the HELLO handshake calls.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster import node
from backend.src.services.cluster.sync import server as srv_mod


@pytest.fixture
def running_server(monkeypatch):
    srv = srv_mod.SyncServer()
    srv._token = "old-token"
    monkeypatch.setattr(srv_mod, "_instance", srv)
    tokens = iter(["new-token"])
    monkeypatch.setattr(node._repo, "generate_sync_token", lambda: next(tokens))
    return srv


def test_the_running_server_accepts_the_new_token_straight_away(running_server):
    token = node.generate_sync_token()

    assert token == "new-token"
    assert running_server._check_token("new-token") is True


def test_the_old_token_stops_working(running_server):
    """A new token is how the operator revokes the old one."""
    node.generate_sync_token()

    assert running_server._check_token("old-token") is False


def test_with_no_server_running_it_is_only_stored(monkeypatch):
    monkeypatch.setattr(srv_mod, "_instance", None)
    monkeypatch.setattr(node._repo, "generate_sync_token", lambda: "t")

    assert node.generate_sync_token() == "t"
