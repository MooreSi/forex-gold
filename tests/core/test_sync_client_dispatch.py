"""Routing on the Mac: which message sets which state, and what a bad row does.

`_dispatch` is a 60-line elif chain and the whole of the Mac's inbound
behaviour. Two kinds of thing can go wrong in it and neither is visible:

  * **A message routed to the wrong slot.** Every order-sending method waits
    on its own asyncio.Event, and a copy-paste slip that set the market-order
    event from a signal-order ack would release the wrong caller with the
    wrong result. That is one order's outcome reported as another's.
  * **A row that cannot be stored taking the connection down.** `_dispatch`
    is called from inside `async for raw in ws`, and `_run_loop` catches
    everything and reconnects. An exception here is not "one row skipped": it
    ends the receive loop, drops the link, and abandons every remaining row
    in the batch. On reconnect the periodic pull fetches the same batch and
    the same row does it again.

Nothing here touches a real database or socket.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import protocol as P

pytestmark = pytest.mark.asyncio


@pytest.fixture
def node(monkeypatch):
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = sc.CONN_CONNECTED
    cli.remote_status = {}
    cli.remote_signal_gen_stats = {}
    cli.remote_settings = {}
    cli._pending_settings = {}
    cli._pending_channel_strategy = {}
    cli._pending_trading_schedule = None
    cli._pending_strategy_params = None
    for name in ("_stand_down_ack_event", "_resume_ack_event",
                 "_engine_control_ack_event", "_market_order_ack_event",
                 "_signal_order_ack_event", "_signal_followup_ack_event"):
        setattr(cli, name, asyncio.Event())
    for name in ("_last_stand_down_ack", "_last_engine_control_ack",
                 "_last_market_order_ack", "_last_signal_order_ack",
                 "_last_signal_followup_ack"):
        setattr(cli, name, {})
    return cli


@pytest.fixture
def recorded(monkeypatch):
    """Capture consolidated-ledger writes instead of making them.

    It refuses what the real table refuses: consolidated_trades declares
    node_id and trade_id NOT NULL and UNIQUE(node_id, trade_id) on top of
    them. A fake that accepts anything would make the rows-after-a-bad-one
    tests below pass while the real thing raised.
    """
    rows: list = []

    def _record(node_id, trade):
        if not node_id or not (trade or {}).get("trade_id"):
            raise ValueError("NOT NULL constraint failed: consolidated_trades")
        rows.append((node_id, trade))

    monkeypatch.setattr(sc.db_module, "record_consolidated_trade", _record)
    return rows


ACKS = [
    (P.MSG_STAND_DOWN_ACK, "_stand_down_ack_event", "_last_stand_down_ack"),
    (P.MSG_ENGINE_CONTROL_ACK, "_engine_control_ack_event", "_last_engine_control_ack"),
    (P.MSG_MARKET_ORDER_ACK, "_market_order_ack_event", "_last_market_order_ack"),
    (P.MSG_SIGNAL_ORDER_ACK, "_signal_order_ack_event", "_last_signal_order_ack"),
    (P.MSG_SIGNAL_FOLLOWUP_ACK, "_signal_followup_ack_event", "_last_signal_followup_ack"),
]
ALL_EVENTS = [e for _t, e, _a in ACKS] + ["_resume_ack_event"]


class TestAnAckReleasesItsOwnWaiterAndNoOther:
    @pytest.mark.parametrize("msg_type,event,attr", ACKS,
                             ids=[t for t, _e, _a in ACKS])
    async def test_it_sets_its_own_event_and_stores_its_own_payload(
        self, node, msg_type, event, attr,
    ):
        await node._dispatch({"type": msg_type, "marker": msg_type})

        assert getattr(node, event).is_set()
        assert getattr(node, attr)["marker"] == msg_type

    @pytest.mark.parametrize("msg_type,event,attr", ACKS,
                             ids=[t for t, _e, _a in ACKS])
    async def test_it_sets_no_other_event(self, node, msg_type, event, attr):
        """The one that matters. Every order send waits on its own event, so
        setting a neighbour's hands the wrong caller the wrong result."""
        await node._dispatch({"type": msg_type})

        others = [name for name in ALL_EVENTS
                  if name != event and getattr(node, name).is_set()]

        assert others == [], f"{msg_type} also released {others}"

    async def test_the_resume_ack_sets_only_its_event(self, node):
        """It carries no payload -- there is nothing to report, only that the
        VPS heard. Still must not release anyone else."""
        await node._dispatch({"type": P.MSG_RESUME_ACK})

        assert node._resume_ack_event.is_set()
        assert not node._market_order_ack_event.is_set()

    async def test_no_ack_fires_for_an_unrelated_message(self, node):
        """Negative control: if some event were set by the fixture or by any
        message at all, every test above would pass regardless of routing."""
        await node._dispatch({"type": P.MSG_PONG})

        assert not any(getattr(node, name).is_set() for name in ALL_EVENTS)


class TestStateMessages:
    async def test_a_heartbeat_is_stored_whole(self, node):
        await node._dispatch({"type": P.MSG_STATUS_HEARTBEAT, "equity": 1000})

        assert node.remote_status["equity"] == 1000

    async def test_an_unknown_type_is_ignored_rather_than_raising(self, node):
        """The peer may be a newer version sending a type this one has never
        heard of. That must not drop the link."""
        await node._dispatch({"type": "MSG_FROM_THE_FUTURE", "x": 1})

    async def test_a_message_with_no_type_at_all_is_ignored(self, node):
        await node._dispatch({})


class TestTheConsolidatedLedger:
    async def test_a_single_closed_trade_is_recorded(self, node, recorded):
        await node._dispatch({
            "type": P.MSG_TRADE_CLOSED, "node_id": "vps",
            "trade": {"trade_id": "t1", "pnl_dollars": 5.0},
        })

        assert recorded == [("vps", {"trade_id": "t1", "pnl_dollars": 5.0})]

    @pytest.mark.parametrize("msg", [
        {"type": P.MSG_TRADE_CLOSED, "trade": {"trade_id": "t1"}},
        {"type": P.MSG_TRADE_CLOSED, "node_id": "vps", "trade": {}},
        {"type": P.MSG_TRADE_CLOSED, "node_id": "", "trade": {"trade_id": "t1"}},
        {"type": P.MSG_TRADE_CLOSED, "node_id": "vps"},
    ])
    async def test_a_half_row_is_not_written(self, node, recorded, msg):
        """Both columns are NOT NULL and together they are the row's identity.
        A row missing either is not a trade this node can store or later
        recognise as the same trade."""
        await node._dispatch(msg)

        assert recorded == []

    async def test_a_ledger_push_records_every_row(self, node, recorded):
        await node._dispatch({"type": P.MSG_LEDGER_PUSH, "trades": [
            {"node_id": "vps", "trade_id": "t1"},
            {"node_id": "vps", "trade_id": "t2"},
        ]})

        assert [t["trade_id"] for _n, t in recorded] == ["t1", "t2"]

    async def test_an_empty_push_is_fine(self, node, recorded):
        await node._dispatch({"type": P.MSG_LEDGER_PUSH, "trades": []})
        await node._dispatch({"type": P.MSG_LEDGER_PUSH})

        assert recorded == []

    async def test_a_row_that_cannot_be_stored_does_not_drop_the_link(
        self, node, recorded,
    ):
        """The single-trade path above guards on both identity columns; this
        bulk path did not. `_dispatch` runs inside `async for raw in ws`, and
        `_run_loop` catches everything and reconnects -- so an exception here
        is not one skipped row. It ends the receive loop, drops the
        connection, and the periodic pull refetches the same batch on
        reconnect, where the same row does it again.
        """
        await node._dispatch({"type": P.MSG_LEDGER_PUSH, "trades": [
            {"node_id": "vps", "trade_id": "t1"},
            {"node_id": "vps"},                      # no trade_id: NOT NULL
            {"node_id": "vps", "trade_id": "t3"},
        ]})

    async def test_the_rows_after_a_bad_one_are_still_recorded(
        self, node, recorded,
    ):
        """The quieter half of the same defect. Aborting the loop abandons
        every remaining row in the batch, and nothing reports that they were
        dropped."""
        await node._dispatch({"type": P.MSG_LEDGER_PUSH, "trades": [
            {"node_id": "vps", "trade_id": "t1"},
            {"node_id": "vps"},
            {"node_id": "vps", "trade_id": "t3"},
        ]})

        assert [t["trade_id"] for _n, t in recorded] == ["t1", "t3"]

    async def test_a_write_that_raises_does_not_drop_the_link(
        self, node, monkeypatch,
    ):
        """Not only malformed rows. A locked database or a constraint the
        sender does not know about has the same effect on the connection."""
        def _boom(node_id, trade):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(sc.db_module, "record_consolidated_trade", _boom)

        await node._dispatch({"type": P.MSG_LEDGER_PUSH, "trades": [
            {"node_id": "vps", "trade_id": "t1"},
        ]})


class TestTheAiRecoveredSnapshot:
    """The other bulk-apply loop on the same receive path, and so the same
    hazard: one row that will not store must not take the link down or
    silently abandon the rest of the batch."""

    @pytest.fixture
    def applied(self, node, monkeypatch):
        seen: list = []
        monkeypatch.setattr(node, "_apply_ai_recovered_snapshot_row", seen.append)
        return seen

    async def test_every_row_is_applied(self, node, applied):
        await node._dispatch({"type": P.MSG_AI_RECOVERED_PUSH, "signals": [
            {"tg_message_id": "m1"}, {"tg_message_id": "m2"},
        ]})

        assert [r["tg_message_id"] for r in applied] == ["m1", "m2"]

    async def test_an_empty_snapshot_is_fine(self, node, applied):
        await node._dispatch({"type": P.MSG_AI_RECOVERED_PUSH, "signals": []})
        await node._dispatch({"type": P.MSG_AI_RECOVERED_PUSH})

        assert applied == []

    async def test_a_row_that_raises_does_not_drop_the_link(self, node, monkeypatch):
        def _boom(row):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(node, "_apply_ai_recovered_snapshot_row", _boom)

        await node._dispatch({"type": P.MSG_AI_RECOVERED_PUSH,
                              "signals": [{"tg_message_id": "m1"}]})

    async def test_the_rows_after_a_bad_one_are_still_applied(self, node):
        applied: list = []

        def _maybe(row):
            if row.get("tg_message_id") == "bad":
                raise RuntimeError("no")
            applied.append(row["tg_message_id"])

        node._apply_ai_recovered_snapshot_row = _maybe

        await node._dispatch({"type": P.MSG_AI_RECOVERED_PUSH, "signals": [
            {"tg_message_id": "m1"}, {"tg_message_id": "bad"},
            {"tg_message_id": "m3"},
        ]})

        assert applied == ["m1", "m3"]
