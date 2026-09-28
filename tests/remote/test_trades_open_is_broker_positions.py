"""The admin console's "N trades open" must be what the broker holds.

It used to be COUNT(*) of status='open' rows in the client's own database.
That disagreed with MT5 whenever a row was never closed off -- found live
2026-09-28: the VPS reported "2 trades open" while MT5 had no positions at
all, because two EA Template placeholder rows (ticket 0, entry 0) were
waiting out the 24h no-fill expiry. The console is for looking at a machine
from outside; it should say what the broker says, not what the app believes.

When the broker cannot be read, the count is None ("unknown"), never 0: a
console that says "0 trades open" about a machine with live positions is
worse than one that says it cannot tell.
"""
import asyncio

import pytest

from backend.src.services.cluster.remote import client as rc
from backend.src.services.cluster.remote import _broker_positions as bp
from backend.src.services.channels import repo as channels_repo


class _Bridge:
    """Only the two reads the count may use. Anything else -- an order, a
    close -- is an AttributeError, so a test fails if the count ever tries."""

    def __init__(self, positions, connected=True, hang=False):
        self._positions = positions
        self._connected = connected
        self._hang = hang

    async def get_positions(self):
        if self._hang:
            await asyncio.sleep(60)
        if isinstance(self._positions, Exception):
            raise self._positions
        return self._positions

    async def get_health(self):
        return {"connected": self._connected}


class _Engine:
    def __init__(self, bridge):
        self._bridge = bridge


def _count(monkeypatch, bridge, **kw):
    monkeypatch.setattr(bp, "_engine", lambda: _Engine(bridge) if bridge else None)
    return asyncio.run(bp.broker_position_count(**kw))


def test_counts_the_positions_the_broker_reports(monkeypatch):
    positions = [{"ticket": 1}, {"ticket": 2}, {"ticket": 3}]
    assert _count(monkeypatch, _Bridge(positions)) == 3


def test_no_positions_on_a_connected_broker_is_zero(monkeypatch):
    assert _count(monkeypatch, _Bridge([], connected=True)) == 0


def test_an_empty_list_from_a_disconnected_bridge_is_unknown(monkeypatch):
    """Same ambiguity position_sync guards against: a bridge that answers but
    is not connected to the terminal returns [] for "I don't know"."""
    assert _count(monkeypatch, _Bridge([], connected=False)) is None


def test_a_failed_read_is_unknown_not_zero(monkeypatch):
    assert _count(monkeypatch, _Bridge(None)) is None


def test_a_read_that_raises_is_unknown(monkeypatch):
    assert _count(monkeypatch, _Bridge(RuntimeError("pipe closed"))) is None


def test_a_read_that_hangs_is_unknown_and_does_not_stall_the_heartbeat(monkeypatch):
    assert _count(monkeypatch, _Bridge([{"ticket": 1}], hang=True), timeout_s=0.05) is None


def test_no_engine_yet_is_unknown(monkeypatch):
    assert _count(monkeypatch, None) is None


def test_the_heartbeat_reports_the_broker_count_not_the_database_rows(monkeypatch):
    """The exact live case: the database holds two open rows the broker has
    never heard of. The heartbeat must say 0."""
    monkeypatch.setattr(channels_repo, "get_open_trade_count", lambda: 2)

    assert rc._build_status(broker_positions=0)["trades_open"] == 0


def test_the_heartbeat_says_unknown_when_the_broker_could_not_be_read(monkeypatch):
    monkeypatch.setattr(channels_repo, "get_open_trade_count", lambda: 2)

    assert rc._build_status(broker_positions=None)["trades_open"] is None
