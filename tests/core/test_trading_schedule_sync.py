"""Local/Remote sync support for the Trading Schedule.

No existing tests exercise SyncServer/SyncClient directly (both are trivial
to construct standalone -- neither requires a real websocket or engine).
Covers: the forward-on-local-edit / no-forward-on-sync-apply re-entrancy
guard, the server-side propose/broadcast handlers, and the client-side
propose/mirror/pending-durability handlers -- each tested by calling the
real methods directly rather than mocking the logic under test.
"""
import json
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from backend.src.db import database as db
from backend.src.services.risk import schedule as sched
from backend.src.controllers.sync.server import SyncServer
from backend.src.controllers.sync.client import SyncClient


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
    yield db
    _reset_thread_local_connection()
    os.remove(path)


def _sample_snapshot():
    schedule = sched._default_schedule()
    schedule["wednesday"][0] = {"enabled": True, "start": "09:00", "end": "12:00", "target": 50.0}
    return {"enabled": True, "schedule": schedule}


# ── core_trading_schedule.py: forwarding + re-entrancy guard ────────────────

def test_forward_is_a_noop_when_sync_not_configured(fresh_db):
    """Both get_instance() calls return None until sync is actually set up --
    must not raise, must not hang."""
    with patch("backend.src.controllers.sync.client.get_instance", return_value=None), \
         patch("backend.src.controllers.sync.server.get_instance", return_value=None):
        sched.set_trading_schedule_enabled(True)  # must not raise


def test_local_edit_forwards_over_client_when_configured(fresh_db):
    fake_client = AsyncMock()
    fake_client.propose_trading_schedule = AsyncMock()
    with patch("backend.src.controllers.sync.client.get_instance", return_value=fake_client), \
         patch("backend.src.services.risk.schedule._schedule_coro") as fake_schedule_coro:
        sched.set_trading_schedule_enabled(True)
        assert fake_schedule_coro.called
        # the coroutine object passed to _schedule_coro was propose_trading_schedule(...)
        coro_arg = fake_schedule_coro.call_args[0][0]
        coro_arg.close()  # avoid an "unawaited coroutine" warning; we're only checking it was built


def test_applying_a_sync_snapshot_does_not_reforward(fresh_db):
    """The core re-entrancy guard: mirroring an incoming snapshot must NOT
    trigger another outbound propose, or the two nodes ping-pong forever."""
    with patch(
        "backend.src.services.risk.schedule._forward_trading_schedule_over_sync"
    ) as fake_forward:
        sched.apply_trading_schedule_snapshot(_sample_snapshot())
        fake_forward.assert_not_called()


def test_trading_schedule_snapshot_round_trips(fresh_db):
    snap = _sample_snapshot()
    sched.apply_trading_schedule_snapshot(snap)
    result = sched.trading_schedule_snapshot()
    assert result["enabled"] is True
    assert result["schedule"]["wednesday"][0]["start"] == "09:00"
    assert result["schedule"]["wednesday"][0]["target"] == 50.0


# ── SyncServer ───────────────────────────────────────────────────────────────

class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def test_server_trading_schedule_snapshot_matches_local_state(fresh_db):
    sched.apply_trading_schedule_snapshot(_sample_snapshot())
    server = SyncServer()
    assert server._trading_schedule_snapshot() == sched.trading_schedule_snapshot()


def test_server_handle_propose_applies_and_broadcasts(fresh_db):
    import asyncio
    server = SyncServer()
    ws = _FakeWs()
    server._clients.add(ws)
    msg = {"type": "trading_schedule_propose", "trading_schedule": _sample_snapshot()}
    asyncio.run(server._handle_trading_schedule_propose(ws, msg))

    # applied locally
    assert sched.is_trading_schedule_enabled() is True
    assert sched.get_trading_schedule()["wednesday"][0]["start"] == "09:00"

    # confirmed state broadcast back to the (only) connected client
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "trading_schedule_state"
    assert ws.sent[0]["trading_schedule"]["schedule"]["wednesday"][0]["target"] == 50.0


def test_server_broadcast_trading_schedule_reaches_all_clients(fresh_db):
    import asyncio
    sched.apply_trading_schedule_snapshot(_sample_snapshot())
    server = SyncServer()
    ws1, ws2 = _FakeWs(), _FakeWs()
    server._clients.add(ws1)
    server._clients.add(ws2)
    asyncio.run(server.broadcast_trading_schedule())
    assert len(ws1.sent) == 1 and len(ws2.sent) == 1
    assert ws1.sent[0]["type"] == "trading_schedule_state"


# ── SyncClient ───────────────────────────────────────────────────────────────

def test_client_pending_trading_schedule_persists_across_instances(fresh_db):
    import asyncio
    client = SyncClient()
    asyncio.run(client.propose_trading_schedule(_sample_snapshot()))
    assert client._pending_trading_schedule == _sample_snapshot()

    # a fresh SyncClient (e.g. after an app restart) must load the same
    # unconfirmed pending change back from app_config
    client2 = SyncClient()
    assert client2._pending_trading_schedule == _sample_snapshot()


def test_client_mirror_applies_when_nothing_pending(fresh_db):
    client = SyncClient()
    client._mirror_trading_schedule_locally(_sample_snapshot())
    assert sched.is_trading_schedule_enabled() is True
    assert sched.get_trading_schedule()["wednesday"][0]["start"] == "09:00"


def test_client_mirror_skips_when_a_local_edit_is_still_unconfirmed(fresh_db):
    """Must not let a stale incoming snapshot clobber a just-made local edit
    that hasn't been confirmed by the VPS yet."""
    client = SyncClient()
    my_edit = _sample_snapshot()
    client._pending_trading_schedule = my_edit

    stale_incoming = sched._default_schedule()
    stale_incoming["wednesday"][0] = {"enabled": True, "start": "01:00", "end": "02:00", "target": 999.0}
    client._mirror_trading_schedule_locally({"enabled": False, "schedule": stale_incoming})

    # local DB untouched by the stale incoming snapshot
    assert sched.is_trading_schedule_enabled() is False  # nothing applied -- default state
    assert sched.get_trading_schedule() == sched._default_schedule()


def test_client_dispatch_clears_pending_once_confirmed_matches(fresh_db):
    """Realistic order: the local edit already applied to this node's own DB
    (via set_trading_schedule_enabled/set_trading_schedule, same as the UI's
    Save button) before propose_trading_schedule ever runs -- _pending_
    trading_schedule tracks "not yet confirmed by the VPS", not "not yet
    applied here"."""
    import asyncio
    my_edit = _sample_snapshot()
    sched.apply_trading_schedule_snapshot(my_edit)  # what the local edit already did

    client = SyncClient()
    client._pending_trading_schedule = my_edit  # awaiting the VPS's confirmation

    asyncio.run(client._dispatch({"type": "trading_schedule_state", "trading_schedule": my_edit}))

    assert client._pending_trading_schedule is None
    assert sched.is_trading_schedule_enabled() is True
