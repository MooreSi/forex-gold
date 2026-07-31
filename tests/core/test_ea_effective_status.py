"""ea_bridge.get_effective_ea_status(): the top-bar EA badge's logic.
Reflects whichever node is actually executing trades -- this node's own
EABridge connection when standalone or when this node is the active
trader, or the paired node's EA state (from the sync heartbeat) when a
Local/Remote pairing has handed control to the other node.
"""
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from forex_trader.core import database as db
from forex_trader.core import ea_bridge


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    ea_bridge.set_instance(None)
    yield db
    ea_bridge.set_instance(None)
    _reset_thread_local_connection()
    os.remove(path)


def test_standalone_no_sync_client_reflects_local_ea(fresh_db):
    with patch("backend.src.controllers.sync.client.get_instance", return_value=None):
        fake_ea = MagicMock()
        fake_ea.is_ea_healthy.return_value = True
        ea_bridge.set_instance(fake_ea)
        ok, scope = ea_bridge.get_effective_ea_status()
        assert ok is True
        assert scope == "this node"


def test_standalone_no_local_ea_instance_is_unhealthy(fresh_db):
    with patch("backend.src.controllers.sync.client.get_instance", return_value=None):
        ea_bridge.set_instance(None)
        ok, scope = ea_bridge.get_effective_ea_status()
        assert ok is False
        assert scope == "this node"


def test_sync_connected_but_this_node_is_active_trader_reflects_local_ea(fresh_db):
    db.set_active_trader("local")
    fake_client = MagicMock()
    fake_client.conn_state = "connected"
    with patch("backend.src.controllers.sync.client.get_instance", return_value=fake_client):
        fake_ea = MagicMock()
        fake_ea.is_ea_healthy.return_value = False
        ea_bridge.set_instance(fake_ea)
        ok, scope = ea_bridge.get_effective_ea_status()
        assert ok is False
        assert scope == "this node"


def test_sync_connected_and_remote_is_active_trader_reflects_remote_heartbeat(fresh_db):
    db.set_active_trader("remote_vps")
    fake_client = MagicMock()
    fake_client.conn_state = "connected"
    fake_client.remote_status = {"ea_connected": True}
    with patch("backend.src.controllers.sync.client.get_instance", return_value=fake_client):
        # Local EA healthy is irrelevant here -- must not be consulted.
        fake_ea = MagicMock()
        fake_ea.is_ea_healthy.return_value = False
        ea_bridge.set_instance(fake_ea)
        ok, scope = ea_bridge.get_effective_ea_status()
        assert ok is True
        assert scope == "VPS"


def test_remote_active_but_heartbeat_says_ea_disconnected(fresh_db):
    db.set_active_trader("remote_vps")
    fake_client = MagicMock()
    fake_client.conn_state = "connected"
    fake_client.remote_status = {"ea_connected": False}
    with patch("backend.src.controllers.sync.client.get_instance", return_value=fake_client):
        ok, scope = ea_bridge.get_effective_ea_status()
        assert ok is False
        assert scope == "VPS"


def test_remote_active_trader_but_sync_client_disconnected_falls_back_to_local(fresh_db):
    """A stale active_trader="remote_vps" flag from before a dropped
    connection must not report a phantom "VPS" scope with no live data."""
    db.set_active_trader("remote_vps")
    fake_client = MagicMock()
    fake_client.conn_state = "disconnected"
    with patch("backend.src.controllers.sync.client.get_instance", return_value=fake_client):
        fake_ea = MagicMock()
        fake_ea.is_ea_healthy.return_value = True
        ea_bridge.set_instance(fake_ea)
        ok, scope = ea_bridge.get_effective_ea_status()
        assert ok is True
        assert scope == "this node"
