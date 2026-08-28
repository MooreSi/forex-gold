"""The consolidated-ledger push every engine's close path calls.

push_trade_closed() is fire-and-forget from inside a close. Its contract is
mostly about what it must NOT do: never raise, never block, never record the
same closed trade twice.

Three properties carry real consequence:

  * it records LOCALLY first, so the Edge Dashboard and History still show the
    trade when the sync link is down
  * if that local write fails it stops, rather than forwarding a trade this
    node has no record of
  * server and client are mutually exclusive. Exactly one sync role is active
    per machine, and pushing through both would double-count a closed trade in
    the consolidated ledger -- which is what the P&L across machines is read
    from

No network, no database: the record call and both sync roles are faked.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.cluster.sync import ledger


class _Recorder:
    def __init__(self):
        self.pushed = []

    async def push_own_trade_closed(self, trade):      # server shape
        self.pushed.append(trade)

    async def push_trade_closed(self, trade):          # client shape
        self.pushed.append(trade)


TRADE = {"trade_id": "t-1", "engine": "reversal", "direction": "BUY",
         "strategy": "scale_out", "open_time": 1.0, "close_time": 2.0,
         "pnl_dollars": 12.5, "outcome": "win"}


@pytest.fixture
def wiring(monkeypatch):
    """Neutral baseline: local record succeeds, neither sync role present."""
    state = {"recorded": []}

    monkeypatch.setattr(ledger.db_module, "get_or_create_node_id", lambda: "node-1")
    monkeypatch.setattr(ledger.db_module, "record_consolidated_trade",
                        lambda node_id, trade: state["recorded"].append((node_id, trade)))

    from backend.src.services.cluster.sync import client as sync_client
    from backend.src.services.cluster.sync import server as sync_server
    monkeypatch.setattr(sync_server, "get_instance", lambda: None)
    monkeypatch.setattr(sync_client, "get_instance", lambda: None)
    state["server_mod"] = sync_server
    state["client_mod"] = sync_client
    return state


def _push_in_loop(trade=TRADE):
    """ensure_future needs a running loop; the real caller is inside one."""
    async def _go():
        ledger.push_trade_closed(trade)
        await asyncio.sleep(0)      # let the scheduled task run
    asyncio.run(_go())


class TestLocalFirst:
    def test_it_records_locally(self, wiring):
        _push_in_loop()
        assert wiring["recorded"] == [("node-1", TRADE)]

    def test_it_records_locally_even_with_no_sync_configured(self, wiring):
        """The standalone case -- most installs. A trade must still reach the
        local ledger with no sync role at all."""
        _push_in_loop()
        assert len(wiring["recorded"]) == 1

    def test_a_failed_local_write_stops_the_push(self, wiring, monkeypatch):
        """Forwarding a trade this node could not record would put a row in the
        consolidated ledger that the local one disagrees with."""
        def _boom(node_id, trade):
            raise RuntimeError("db locked")
        monkeypatch.setattr(ledger.db_module, "record_consolidated_trade", _boom)
        srv = _Recorder()
        monkeypatch.setattr(wiring["server_mod"], "get_instance", lambda: srv)

        _push_in_loop()
        assert srv.pushed == [], "it forwarded a trade it failed to record"


class TestExactlyOneRole:
    def test_the_server_role_forwards(self, wiring, monkeypatch):
        srv = _Recorder()
        monkeypatch.setattr(wiring["server_mod"], "get_instance", lambda: srv)
        _push_in_loop()
        assert srv.pushed == [TRADE]

    def test_the_client_role_forwards_when_connected(self, wiring, monkeypatch):
        cli = _Recorder(); cli.conn_state = "connected"
        monkeypatch.setattr(wiring["client_mod"], "get_instance", lambda: cli)
        _push_in_loop()
        assert cli.pushed == [TRADE]

    def test_a_disconnected_client_does_not_forward(self, wiring, monkeypatch):
        cli = _Recorder(); cli.conn_state = "disconnected"
        monkeypatch.setattr(wiring["client_mod"], "get_instance", lambda: cli)
        _push_in_loop()
        assert cli.pushed == []
        assert len(wiring["recorded"]) == 1, "but the local record still happens"

    def test_the_server_short_circuits_the_client(self, wiring, monkeypatch):
        """The one that would corrupt the numbers. If both roles somehow exist,
        pushing through both double-counts the trade in the consolidated
        ledger, which is what cross-machine P&L is read from."""
        srv = _Recorder()
        cli = _Recorder(); cli.conn_state = "connected"
        monkeypatch.setattr(wiring["server_mod"], "get_instance", lambda: srv)
        monkeypatch.setattr(wiring["client_mod"], "get_instance", lambda: cli)

        _push_in_loop()

        assert srv.pushed == [TRADE]
        assert cli.pushed == [], "the trade was forwarded twice"


class TestItNeverRaises:
    """It is called from inside a close. An exception here would abort the
    close path after the broker has already closed the position."""

    def test_a_throwing_server_is_swallowed(self, wiring, monkeypatch):
        class _Bad:
            async def push_own_trade_closed(self, trade):
                raise RuntimeError("socket gone")
        monkeypatch.setattr(wiring["server_mod"], "get_instance", lambda: _Bad())
        _push_in_loop()          # must not raise

    def test_a_server_lookup_that_raises_is_swallowed(self, wiring, monkeypatch):
        def _boom():
            raise RuntimeError("import blew up")
        monkeypatch.setattr(wiring["server_mod"], "get_instance", _boom)
        _push_in_loop()

    def test_a_client_lookup_that_raises_is_swallowed(self, wiring, monkeypatch):
        def _boom():
            raise RuntimeError("import blew up")
        monkeypatch.setattr(wiring["client_mod"], "get_instance", _boom)
        _push_in_loop()

    def test_no_running_event_loop_is_survived(self, wiring, monkeypatch):
        """Called from a sync context, ensure_future has no loop to schedule
        onto. The local record must still land and nothing may propagate."""
        srv = _Recorder()
        monkeypatch.setattr(wiring["server_mod"], "get_instance", lambda: srv)

        ledger.push_trade_closed(TRADE)      # no loop running

        assert len(wiring["recorded"]) == 1
