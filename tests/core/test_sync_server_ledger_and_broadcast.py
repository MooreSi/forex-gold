"""The VPS's side: the ledger, the broadcast, and the follow-up match.

Four things, each with a failure that is quiet rather than loud:

  * **`_broadcast`** is how every state change reaches the Mac. A client that
    died without closing must be reaped, or the VPS spends every broadcast
    writing to a dead socket -- and one raising exception would stop the
    clients behind it in the set receiving anything at all.
  * **`_handle_trade_closed`** guards on both identity columns, unlike the
    bulk ledger path that did not and dropped the whole link (bugs/019).
  * **`push_own_trade_closed`** records locally AND forwards, so a closure is
    in the ledger whether or not the Mac is listening.
  * **`_handle_signal_followup`** decides whether a follow-up signal modifies
    an existing instant-entry trade or falls through to opening a new one. It
    must answer `matched` honestly: a false `matched=True` means the Mac never
    opens the trade, and a false `matched=False` means it opens a second.

No socket and no database: every writer is captured.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync.protocol import (
    MSG_LEDGER_PUSH, MSG_SIGNAL_FOLLOWUP_ACK, MSG_TRADE_CLOSED,
)

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self, fails=False):
        self.sent: list = []
        self.fails = fails

    async def send(self, raw):
        if self.fails:
            raise ConnectionResetError("gone")
        self.sent.append(json.loads(raw) if isinstance(raw, str) else raw)

    def types(self):
        return [m.get("type") for m in self.sent if isinstance(m, dict)]


@pytest.fixture
def node():
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._clients = set()
    srv._main_engine = None
    return srv


@pytest.fixture
def ledger(monkeypatch):
    rows: list = []
    monkeypatch.setattr(ss.db_module, "record_consolidated_trade",
                        lambda node_id, trade: rows.append((node_id, trade)))
    monkeypatch.setattr(ss.db_module, "get_or_create_node_id", lambda: "vps")
    return rows


class TestBroadcasting:
    async def test_every_client_receives_it(self, node):
        a, b = _Ws(), _Ws()
        node._clients = {a, b}

        await node._broadcast({"type": "x"})

        assert a.sent and b.sent

    async def test_a_dead_client_is_reaped(self, node):
        """Left in the set, every future broadcast writes to a dead socket."""
        dead = _Ws(fails=True)
        node._clients = {dead}

        await node._broadcast({"type": "x"})

        assert dead not in node._clients

    async def test_one_dead_client_does_not_stop_the_others(self, node):
        """The one that matters. An exception escaping here stops every
        client behind it in the set from receiving the message."""
        dead, alive = _Ws(fails=True), _Ws()
        node._clients = {dead, alive}

        await node._broadcast({"type": "x"})

        assert alive.sent

    async def test_a_live_client_is_kept(self, node):
        """Negative control: a reaper that empties the set would satisfy the
        dead-client test and disconnect everyone."""
        alive = _Ws()
        node._clients = {alive}

        await node._broadcast({"type": "x"})

        assert alive in node._clients

    async def test_no_clients_is_not_an_error(self, node):
        await node._broadcast({"type": "x"})


class TestTheConsolidatedLedger:
    async def test_a_closed_trade_is_recorded(self, node, ledger):
        node._handle_trade_closed({"node_id": "mac", "trade": {"trade_id": "t1"}})

        assert ledger == [("mac", {"trade_id": "t1"})]

    @pytest.mark.parametrize("msg", [
        {"trade": {"trade_id": "t1"}},
        {"node_id": "mac", "trade": {}},
        {"node_id": "", "trade": {"trade_id": "t1"}},
        {"node_id": "mac"},
    ])
    async def test_a_half_row_is_not_written(self, node, ledger, msg):
        """Both columns are NOT NULL and together they are the row's identity
        -- the same guard whose absence on the bulk path dropped the whole
        link in bugs/019."""
        node._handle_trade_closed(msg)

        assert ledger == []

    async def test_a_pull_returns_the_whole_ledger(self, node, monkeypatch):
        monkeypatch.setattr(ss.db_module, "get_consolidated_trades",
                            lambda days: [{"trade_id": "t1"}])
        ws = _Ws()

        await node._handle_ledger_pull(ws)

        assert ws.sent[0]["type"] == MSG_LEDGER_PUSH
        assert ws.sent[0]["trades"] == [{"trade_id": "t1"}]

    async def test_an_own_close_is_recorded_before_it_is_forwarded(
        self, node, ledger,
    ):
        """Recorded locally whether or not the Mac is listening. Forwarding
        only would lose the closure entirely while disconnected."""
        await node.push_own_trade_closed({"trade_id": "t1"})

        assert ledger == [("vps", {"trade_id": "t1"})]

    async def test_an_own_close_is_also_forwarded(self, node, ledger):
        client = _Ws()
        node._clients = {client}

        await node.push_own_trade_closed({"trade_id": "t1"})

        assert MSG_TRADE_CLOSED in client.types()

    async def test_an_own_close_is_recorded_with_no_client_connected(
        self, node, ledger,
    ):
        await node.push_own_trade_closed({"trade_id": "t1"})

        assert ledger


class TestTheFollowUpMatch:
    async def test_no_engine_answers_not_matched_with_a_reason(self, node):
        """Silence would leave the Mac waiting on an ack that never comes."""
        ws = _Ws()

        await node._handle_signal_followup(ws, {"channel_name": "GD"})

        assert ws.sent[0]["type"] == MSG_SIGNAL_FOLLOWUP_ACK
        assert ws.sent[0]["matched"] is False
        assert ws.sent[0]["error"]

    async def test_no_matching_trade_answers_not_matched(self, node, monkeypatch):
        """The Mac then opens a new trade. A wrong `matched=True` here means
        it never does."""
        class _Engine:
            async def apply_followup_to_instant_trade(self, *a):
                raise AssertionError("nothing to apply to")
        node._main_engine = _Engine()
        monkeypatch.setattr(
            "backend.src.services.cluster.sync_repo.find_latest_instant_trade",
            lambda ch: None)
        ws = _Ws()

        await node._handle_signal_followup(ws, {"channel_name": "GD", "direction": "BUY"})

        assert ws.sent[0]["matched"] is False

    async def test_the_opposite_direction_does_not_match(self, node, monkeypatch):
        """A SELL follow-up must not be applied to a BUY. Applying it would
        move the stop of a trade running the other way.

        The engine must be a WORKING stub, not a bare object(): with a bare
        one, dropping the direction check still ends at matched=False via the
        AttributeError handler, and the test passes on a broken guard.
        Mutation found exactly that.
        """
        applied: list = []

        class _Engine:
            async def apply_followup_to_instant_trade(self, *a):
                applied.append(a)
        node._main_engine = _Engine()
        monkeypatch.setattr(
            "backend.src.services.cluster.sync_repo.find_latest_instant_trade",
            lambda ch: {"direction": "BUY", "trade_id": "t1"})
        ws = _Ws()

        await node._handle_signal_followup(ws, {"channel_name": "GD", "direction": "SELL"})

        assert ws.sent[0]["matched"] is False
        assert applied == [], "a SELL follow-up was applied to a BUY trade"

    async def test_a_match_applies_the_update_and_says_so(self, node, monkeypatch):
        applied: list = []

        class _Engine:
            async def apply_followup_to_instant_trade(self, *a):
                applied.append(a)
        node._main_engine = _Engine()
        monkeypatch.setattr(
            "backend.src.services.cluster.sync_repo.find_latest_instant_trade",
            lambda ch: {"direction": "BUY", "trade_id": "t1"})
        ws = _Ws()

        await node._handle_signal_followup(
            ws, {"channel_name": "GD", "direction": "BUY",
                 "updates": {"stop_loss": 1.5}, "tg_id": "m1"})

        assert applied
        assert ws.sent[0]["matched"] is True

    async def test_a_failure_answers_not_matched_rather_than_hanging(
        self, node, monkeypatch,
    ):
        """Every path replies. The Mac is waiting with a timeout, and an
        unanswered follow-up costs it the whole wait before it can act."""
        class _Engine:
            async def apply_followup_to_instant_trade(self, *a):
                raise RuntimeError("database locked")
        node._main_engine = _Engine()
        monkeypatch.setattr(
            "backend.src.services.cluster.sync_repo.find_latest_instant_trade",
            lambda ch: {"direction": "BUY", "trade_id": "t1"})
        ws = _Ws()

        await node._handle_signal_followup(ws, {"channel_name": "GD", "direction": "BUY"})

        assert ws.sent[0]["matched"] is False
        assert "locked" in ws.sent[0]["error"]
