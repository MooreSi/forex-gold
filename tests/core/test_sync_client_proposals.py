"""A change made on the Mac reaches the VPS, or waits until it can.

Four propose/flush pairs. The Mac cannot write the VPS's settings directly --
it proposes, and the VPS decides. The property that makes that safe is in
`_flush_pending_settings`'s own docstring: it is idempotent and resends the
FULL pending set every time, so a message lost to a mid-flight disconnect is
simply retried and a VPS that restarted stale never loses track of what the
Mac still wants applied.

The failure if that breaks is quiet and expensive: a setting the user changed
here appears to have been saved, the VPS never hears, and the two nodes trade
on different numbers. `_mirror_settings_locally`'s pending-skip is the other
half of the same mechanism (see test_sync_client_mirrors.py) -- together they
stop a reconnect from reverting a just-made edit.

No socket: the websocket is a list.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync.protocol import (
    MSG_CHANNEL_STRATEGY_PROPOSE, MSG_SETTINGS_PROPOSE,
    MSG_STRATEGY_PARAMS_PROPOSE, MSG_TRADING_SCHEDULE_PROPOSE,
)

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self, fails=False):
        self.sent: list = []
        self.fails = fails

    async def send(self, raw):
        if self.fails:
            raise ConnectionResetError("dropped mid-flight")
        self.sent.append(json.loads(raw))


@pytest.fixture
def node(monkeypatch):
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = sc.CONN_CONNECTED
    cli._ws = _Ws()
    cli._pending_settings = {}
    cli._pending_channel_strategy = {}
    cli._pending_trading_schedule = None
    cli._pending_strategy_params = None
    for name in ("_persist_pending", "_persist_pending_channel_strategy",
                 "_persist_pending_trading_schedule", "_persist_pending_strategy_params"):
        monkeypatch.setattr(cli, name, lambda: None, raising=False)
    return cli


class TestProposingSettings:
    async def test_it_is_sent(self, node):
        await node.propose_settings({"max_daily_loss_pct": 3.0})

        assert node._ws.sent[0]["type"] == MSG_SETTINGS_PROPOSE
        assert node._ws.sent[0]["updates"] == {"max_daily_loss_pct": 3.0}

    async def test_it_is_remembered_until_the_vps_confirms(self, node):
        """The pending set is what stops a reconnect reverting the edit --
        `_mirror_settings_locally` skips any key still in it."""
        await node.propose_settings({"max_daily_loss_pct": 3.0})

        assert node._pending_settings == {"max_daily_loss_pct": 3.0}

    async def test_an_empty_proposal_sends_nothing(self, node):
        await node.propose_settings({})

        assert node._ws.sent == []

    async def test_a_second_proposal_resends_the_whole_pending_set(self, node):
        """Idempotent by design: the VPS may have missed the first, so the
        second carries both rather than only the new one."""
        await node.propose_settings({"max_daily_loss_pct": 3.0})
        await node.propose_settings({"max_open_trades": 5})

        assert node._ws.sent[1]["updates"] == {"max_daily_loss_pct": 3.0,
                                               "max_open_trades": 5}

    async def test_a_failed_send_keeps_the_change_pending(self, node):
        """The whole point. A dropped send must leave the edit queued for the
        next reconnect, not lost with the user believing it saved."""
        node._ws = _Ws(fails=True)

        await node.propose_settings({"max_daily_loss_pct": 3.0})

        assert node._pending_settings == {"max_daily_loss_pct": 3.0}

    async def test_a_failed_send_does_not_raise(self, node):
        """This is reached from a UI save handler."""
        node._ws = _Ws(fails=True)

        await node.propose_settings({"max_daily_loss_pct": 3.0})

    async def test_disconnected_it_queues_without_sending(self, node):
        node.conn_state = sc.CONN_DISCONNECTED

        await node.propose_settings({"max_daily_loss_pct": 3.0})

        assert node._ws.sent == []
        assert node._pending_settings == {"max_daily_loss_pct": 3.0}

    async def test_the_queue_is_flushed_on_reconnect(self, node):
        """What the queue is for: the edit goes out when the link returns."""
        node.conn_state = sc.CONN_DISCONNECTED
        await node.propose_settings({"max_daily_loss_pct": 3.0})
        node.conn_state = sc.CONN_CONNECTED

        await node._flush_pending_settings()

        assert node._ws.sent[0]["updates"] == {"max_daily_loss_pct": 3.0}

    async def test_flushing_an_empty_queue_sends_nothing(self, node):
        await node._flush_pending_settings()

        assert node._ws.sent == []


class TestTheOtherThree:
    async def test_a_channel_strategy_is_proposed(self, node):
        await node.propose_channel_strategy("GD", "scale_out", True)

        sent = node._ws.sent[0]
        assert sent["type"] == MSG_CHANNEL_STRATEGY_PROPOSE
        assert sent["updates"]["GD"] == {"strategy": "scale_out", "auto": True}

    async def test_a_trading_schedule_is_proposed(self, node):
        await node.propose_trading_schedule({"mon": [{"from": "07:00"}]})

        sent = node._ws.sent[0]
        assert sent["type"] == MSG_TRADING_SCHEDULE_PROPOSE
        assert sent["trading_schedule"] == {"mon": [{"from": "07:00"}]}

    async def test_strategy_params_are_proposed(self, node):
        await node.propose_strategy_params({"scale_out": {"tp1_pct": 50}})

        assert node._ws.sent[0]["type"] == MSG_STRATEGY_PARAMS_PROPOSE

    @pytest.mark.parametrize("call,queue", [
        (("propose_channel_strategy", ("GD", "x", True)), "_pending_channel_strategy"),
        (("propose_trading_schedule", ({"mon": []},)), "_pending_trading_schedule"),
        (("propose_strategy_params", ({"a": 1},)), "_pending_strategy_params"),
    ])
    async def test_a_failed_send_keeps_it_queued(self, node, call, queue):
        node._ws = _Ws(fails=True)
        name, args = call

        await getattr(node, name)(*args)

        assert getattr(node, queue)

    @pytest.mark.parametrize("flush", ["_flush_pending_channel_strategy",
                                       "_flush_pending_trading_schedule",
                                       "_flush_pending_strategy_params"])
    async def test_flushing_an_empty_queue_sends_nothing(self, node, flush):
        await getattr(node, flush)()

        assert node._ws.sent == []
