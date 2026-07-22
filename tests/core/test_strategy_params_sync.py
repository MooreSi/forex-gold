"""Local/Remote sync support for Strategy Parameters (core_strategy_params.py).

Mirrors test_trading_schedule_sync.py's structure exactly -- same
one-combined-snapshot shape, same forward/re-entrancy-guard/pending-
durability pattern. Covers: the forward-on-local-edit / no-forward-on-
sync-apply re-entrancy guard, the server-side propose/broadcast handlers,
and the client-side propose/mirror/pending-durability handlers -- each
tested by calling the real methods directly rather than mocking the
logic under test.
"""
import json
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_strategy_params as sp
from forex_trader.core.models import STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER
from forex_trader.sync.server import SyncServer
from forex_trader.sync.client import SyncClient


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
    sp._cache.clear()
    yield db
    sp._cache.clear()
    _reset_thread_local_connection()
    os.remove(path)


def _sample_snapshot():
    snap = sp.strategy_params_snapshot()
    snap[STRATEGY_CONSERVATIVE] = {"sl_pt": 7.5, "tp1_pt": 4.0}
    return snap


# ── core_strategy_params.py: forwarding + re-entrancy guard ─────────────────

def test_forward_is_a_noop_when_sync_not_configured(fresh_db):
    """Both get_instance() calls return None until sync is actually set up --
    must not raise, must not hang."""
    with patch("forex_trader.sync.client.get_instance", return_value=None), \
         patch("forex_trader.sync.server.get_instance", return_value=None):
        sp.set_strategy_params(STRATEGY_CONSERVATIVE, {"sl_pt": 6.0})  # must not raise


def test_local_edit_forwards_over_client_when_configured(fresh_db):
    fake_client = AsyncMock()
    fake_client.propose_strategy_params = AsyncMock()
    with patch("forex_trader.sync.client.get_instance", return_value=fake_client), \
         patch("forex_trader.core.core_strategy_params._schedule_coro") as fake_schedule_coro:
        sp.set_strategy_params(STRATEGY_CONSERVATIVE, {"sl_pt": 6.0})
        assert fake_schedule_coro.called
        # the coroutine object passed to _schedule_coro was propose_strategy_params(...)
        coro_arg = fake_schedule_coro.call_args[0][0]
        coro_arg.close()  # avoid an "unawaited coroutine" warning; we're only checking it was built


def test_applying_a_sync_snapshot_does_not_reforward(fresh_db):
    """The core re-entrancy guard: mirroring an incoming snapshot must NOT
    trigger another outbound propose, or the two nodes ping-pong forever."""
    with patch(
        "forex_trader.core.core_strategy_params._forward_strategy_params_over_sync"
    ) as fake_forward:
        sp.apply_strategy_params_snapshot(_sample_snapshot())
        fake_forward.assert_not_called()


def test_reset_and_apply_template_also_forward(fresh_db):
    """reset_strategy_params() and apply_template() both funnel through
    set_strategy_params() -- confirm they trigger the same forward path,
    not just the direct set_strategy_params() call."""
    fake_client = AsyncMock()
    fake_client.propose_strategy_params = AsyncMock()
    with patch("forex_trader.sync.client.get_instance", return_value=fake_client), \
         patch("forex_trader.core.core_strategy_params._schedule_coro") as fake_schedule_coro:
        sp.reset_strategy_params(STRATEGY_CONSERVATIVE)
        assert fake_schedule_coro.called
        fake_schedule_coro.call_args[0][0].close()

    tid = sp.save_template(STRATEGY_SCALP_RUNNER, "Wide", {"sl_pt": 20.0})
    with patch("forex_trader.sync.client.get_instance", return_value=fake_client), \
         patch("forex_trader.core.core_strategy_params._schedule_coro") as fake_schedule_coro2:
        sp.apply_template(tid)
        assert fake_schedule_coro2.called
        fake_schedule_coro2.call_args[0][0].close()


def test_strategy_params_snapshot_round_trips(fresh_db):
    snap = _sample_snapshot()
    sp.apply_strategy_params_snapshot(snap)
    result = sp.strategy_params_snapshot()
    assert result[STRATEGY_CONSERVATIVE] == {"sl_pt": 7.5, "tp1_pt": 4.0}


def test_apply_snapshot_skips_unknown_strategy_key(fresh_db):
    """Forward-compat: a snapshot from a newer version with an extra
    strategy this node doesn't recognize must not raise."""
    snap = {"not_a_real_strategy": {"sl_pt": 1.0}}
    sp.apply_strategy_params_snapshot(snap)  # must not raise


# ── SyncServer ───────────────────────────────────────────────────────────────

class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def test_server_strategy_params_snapshot_matches_local_state(fresh_db):
    sp.apply_strategy_params_snapshot(_sample_snapshot())
    server = SyncServer()
    assert server._strategy_params_snapshot() == sp.strategy_params_snapshot()


def test_server_handle_propose_applies_and_broadcasts(fresh_db):
    import asyncio
    server = SyncServer()
    ws = _FakeWs()
    server._clients.add(ws)
    msg = {"type": "strategy_params_propose", "strategy_params": _sample_snapshot()}
    asyncio.run(server._handle_strategy_params_propose(ws, msg))

    # applied locally
    assert sp.get_strategy_params(STRATEGY_CONSERVATIVE) == {"sl_pt": 7.5, "tp1_pt": 4.0}

    # confirmed state broadcast back to the (only) connected client
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "strategy_params_state"
    assert ws.sent[0]["strategy_params"][STRATEGY_CONSERVATIVE] == {"sl_pt": 7.5, "tp1_pt": 4.0}


def test_server_broadcast_strategy_params_reaches_all_clients(fresh_db):
    import asyncio
    sp.apply_strategy_params_snapshot(_sample_snapshot())
    server = SyncServer()
    ws1, ws2 = _FakeWs(), _FakeWs()
    server._clients.add(ws1)
    server._clients.add(ws2)
    asyncio.run(server.broadcast_strategy_params())
    assert len(ws1.sent) == 1 and len(ws2.sent) == 1
    assert ws1.sent[0]["type"] == "strategy_params_state"


# ── SyncClient ───────────────────────────────────────────────────────────────

def test_client_pending_strategy_params_persists_across_instances(fresh_db):
    import asyncio
    client = SyncClient()
    asyncio.run(client.propose_strategy_params(_sample_snapshot()))
    assert client._pending_strategy_params == _sample_snapshot()

    # a fresh SyncClient (e.g. after an app restart) must load the same
    # unconfirmed pending change back from app_config
    client2 = SyncClient()
    assert client2._pending_strategy_params == _sample_snapshot()


def test_client_mirror_applies_when_nothing_pending(fresh_db):
    client = SyncClient()
    client._mirror_strategy_params_locally(_sample_snapshot())
    assert sp.get_strategy_params(STRATEGY_CONSERVATIVE) == {"sl_pt": 7.5, "tp1_pt": 4.0}


def test_client_mirror_skips_when_a_local_edit_is_still_unconfirmed(fresh_db):
    """Must not let a stale incoming snapshot clobber a just-made local edit
    that hasn't been confirmed by the VPS yet."""
    client = SyncClient()
    my_edit = _sample_snapshot()
    client._pending_strategy_params = my_edit

    stale_incoming = sp.strategy_params_snapshot()
    stale_incoming[STRATEGY_CONSERVATIVE] = {"sl_pt": 999.0, "tp1_pt": 999.0}
    client._mirror_strategy_params_locally(stale_incoming)

    # local DB untouched by the stale incoming snapshot
    assert sp.get_strategy_params(STRATEGY_CONSERVATIVE) == sp.default_params(STRATEGY_CONSERVATIVE)


def test_client_dispatch_clears_pending_once_confirmed_matches(fresh_db):
    """Realistic order: the local edit already applied to this node's own DB
    (via set_strategy_params, same as the UI's Save & Apply button) before
    propose_strategy_params ever runs -- _pending_strategy_params tracks
    "not yet confirmed by the VPS", not "not yet applied here"."""
    import asyncio
    my_edit = _sample_snapshot()
    sp.apply_strategy_params_snapshot(my_edit)  # what the local edit already did

    client = SyncClient()
    client._pending_strategy_params = my_edit  # awaiting the VPS's confirmation

    asyncio.run(client._dispatch({"type": "strategy_params_state", "strategy_params": my_edit}))

    assert client._pending_strategy_params is None
    assert sp.get_strategy_params(STRATEGY_CONSERVATIVE) == {"sl_pt": 7.5, "tp1_pt": 4.0}
