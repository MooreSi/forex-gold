"""The Mac's half of forwarding an order to the VPS.

`tests/core/test_sync_server_order_forwarding.py` covers what the VPS does
when the message arrives. This covers what the Mac does to send it, which is
the half that decides whether a message is sent at all.

Three properties, each of which fails silently and each of which is about
money:

  * **A disconnected client must refuse, loudly.** These are reached from the
    Trading tab's own buttons while the Mac is stood down. A send that quietly
    does nothing leaves the operator looking at a button that appeared to work
    and no trade anywhere.
  * **The ack event is cleared before the send, not after.** Every one of
    these waits on a single long-lived asyncio.Event. Left set from the
    previous call, `wait()` returns immediately and the caller is handed the
    PREVIOUS order's result as this one's -- a rejected order reported as
    filled, or one order's ticket reported for two.
  * **A timeout raises.** It does not return the stale ack. The VPS may have
    placed the trade; "I did not hear back" is not "it did not happen", and
    this is the same distinction docs/todo/refactor/stage3/020 exists for.

Plus the payload itself: a stop_loss that goes missing between here and the
VPS is an unprotected position.

Nothing here reaches a broker, a database or a socket. The websocket is a
list, and no order is placed anywhere.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync.protocol import (
    MSG_MARKET_ORDER, MSG_SIGNAL_FOLLOWUP, MSG_SIGNAL_ORDER,
)

pytestmark = pytest.mark.asyncio


class _Ws:
    """A websocket that records instead of sending."""

    def __init__(self, fail=False):
        self.sent: list = []
        self._fail = fail

    async def send(self, raw):
        if self._fail:
            raise ConnectionResetError("link dropped mid-send")
        self.sent.append(json.loads(raw))

    def only(self) -> dict:
        assert len(self.sent) == 1, self.sent
        return self.sent[0]


@pytest.fixture
def node():
    """A SyncClient with its ack plumbing, and nothing else."""
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = sc.CONN_CONNECTED
    cli._ws = _Ws()
    cli._market_order_ack_event = asyncio.Event()
    cli._last_market_order_ack = {}
    cli._signal_order_ack_event = asyncio.Event()
    cli._last_signal_order_ack = {}
    cli._signal_followup_ack_event = asyncio.Event()
    cli._last_signal_followup_ack = {}
    return cli


async def _ack(node, event_name, attr, payload, delay=0.0):
    """Answer the next request, the way the receive loop does."""
    await asyncio.sleep(delay)
    setattr(node, attr, payload)
    getattr(node, event_name).set()


# Each case: the method, its minimum arguments, the message type it sends,
# and the (event, last-ack attribute) pair it waits on.
CASES = {
    "market": (
        "send_market_order", {"direction": "BUY"}, MSG_MARKET_ORDER,
        "_market_order_ack_event", "_last_market_order_ack",
    ),
    "signal": (
        "send_signal_order",
        {"signal_id": "s1", "direction": "BUY", "entry_low": 1.0,
         "entry_high": 2.0, "stop_loss": 0.5},
        MSG_SIGNAL_ORDER,
        "_signal_order_ack_event", "_last_signal_order_ack",
    ),
    "followup": (
        "send_signal_followup",
        {"channel_name": "c", "direction": "BUY", "updates": {}, "tg_id": "1"},
        MSG_SIGNAL_FOLLOWUP,
        "_signal_followup_ack_event", "_last_signal_followup_ack",
    ),
}
ALL = list(CASES)


class TestARefusalIsLoud:
    @pytest.mark.parametrize("case", ALL)
    async def test_a_disconnected_client_raises(self, node, case):
        method, args, *_ = CASES[case]
        node.conn_state = sc.CONN_DISCONNECTED

        with pytest.raises(ConnectionError):
            await getattr(node, method)(**args)

        assert node._ws.sent == [], "a message went out on a dead link"

    @pytest.mark.parametrize("case", ALL)
    async def test_no_socket_at_all_raises(self, node, case):
        method, args, *_ = CASES[case]
        node._ws = None

        with pytest.raises(ConnectionError):
            await getattr(node, method)(**args)

    @pytest.mark.parametrize("case", ALL)
    @pytest.mark.parametrize("state", [sc.CONN_CONNECTING, sc.CONN_REJECTED])
    async def test_a_socket_that_is_not_yet_connected_raises(self, node, case, state):
        """Having a socket object is not being connected. During the handshake
        `_ws` is set and `conn_state` is not yet CONN_CONNECTED, and a check on
        the socket alone would send an order into a link the VPS has not
        authenticated -- or has just rejected."""
        method, args, *_ = CASES[case]
        node.conn_state = state

        with pytest.raises(ConnectionError):
            await getattr(node, method)(**args)

        assert node._ws.sent == []

    @pytest.mark.parametrize("case", ALL)
    async def test_a_send_that_fails_propagates(self, node, case):
        """A link that drops mid-send must not leave the caller waiting on an
        ack that can never arrive."""
        method, args, *_ = CASES[case]
        node._ws = _Ws(fail=True)

        with pytest.raises(ConnectionResetError):
            await getattr(node, method)(**args)


class TestTheAckBelongsToThisRequest:
    @pytest.mark.parametrize("case", ALL)
    async def test_a_stale_ack_does_not_answer_a_new_request(self, node, case):
        """The event is long-lived. Left set by the previous call, wait()
        returns at once and the caller is handed the previous order's result
        as this one's."""
        method, args, _t, event, attr = CASES[case]
        setattr(node, attr, {"ok": True, "ticket": "FROM-THE-LAST-ORDER"})
        getattr(node, event).set()

        task = asyncio.create_task(getattr(node, method)(timeout=0.5, **args))
        await asyncio.sleep(0.05)

        assert not task.done(), (
            "returned before the VPS answered, using the previous ack"
        )

        await _ack(node, event, attr, {"ok": True, "ticket": "THIS-ONE"})

        assert (await task)["ticket"] == "THIS-ONE"

    @pytest.mark.parametrize("case", ALL)
    async def test_it_returns_the_ack_it_waited_for(self, node, case):
        method, args, _t, event, attr = CASES[case]

        task = asyncio.create_task(getattr(node, method)(timeout=1.0, **args))
        asyncio.create_task(_ack(node, event, attr, {"ok": True, "n": 7}, 0.02))

        assert (await task) == {"ok": True, "n": 7}

    @pytest.mark.parametrize("case", ALL)
    async def test_a_timeout_raises_rather_than_returning_a_stale_answer(
        self, node, case,
    ):
        """Silence is not a refusal. The VPS may have placed the trade, so the
        caller has to be told it does not know -- returning the empty or
        previous ack reports "no trade" for a trade that may exist."""
        method, args, _t, _e, attr = CASES[case]
        setattr(node, attr, {"ok": False, "error": "an older failure"})

        with pytest.raises(asyncio.TimeoutError):
            await getattr(node, method)(timeout=0.05, **args)


class TestWhatGoesOnTheWire:
    @pytest.mark.parametrize("case", ALL)
    async def test_the_message_type_is_the_one_the_vps_routes_on(self, node, case):
        method, args, msg_type, event, attr = CASES[case]

        task = asyncio.create_task(getattr(node, method)(timeout=1.0, **args))
        asyncio.create_task(_ack(node, event, attr, {"ok": True}, 0.02))
        await task

        assert node._ws.only()["type"] == msg_type

    async def test_the_market_order_carries_its_stop(self, node):
        """A stop_loss that goes missing between here and the VPS is an
        unprotected position on a live account."""
        task = asyncio.create_task(node.send_market_order(
            "SELL", stop_loss=1234.5, lot_size=0.02, strategy="scalp",
            timeout=1.0,
        ))
        asyncio.create_task(_ack(
            node, "_market_order_ack_event", "_last_market_order_ack",
            {"ok": True}, 0.02,
        ))
        await task

        sent = node._ws.only()
        assert sent["direction"] == "SELL"
        assert sent["stop_loss"] == 1234.5
        assert sent["lot_size"] == 0.02
        assert sent["strategy"] == "scalp"

    async def test_an_absent_stop_is_sent_as_null_not_dropped(self, node):
        """The VPS reads stop_loss off the payload. A key that is absent
        rather than null is the difference between "no stop was asked for"
        and "the field never arrived", and only one of those is a bug the VPS
        can notice."""
        task = asyncio.create_task(node.send_market_order("BUY", timeout=1.0))
        asyncio.create_task(_ack(
            node, "_market_order_ack_event", "_last_market_order_ack",
            {"ok": True}, 0.02,
        ))
        await task

        sent = node._ws.only()
        assert "stop_loss" in sent and sent["stop_loss"] is None

    async def test_the_optional_report_fields_are_omitted_when_unused(self, node):
        """take_profit and source_name were added for the ORB report's Execute
        button. The plain Market Order button does not send them, and adding
        them as nulls would change what the VPS sees for every existing
        caller."""
        task = asyncio.create_task(node.send_market_order("BUY", timeout=1.0))
        asyncio.create_task(_ack(
            node, "_market_order_ack_event", "_last_market_order_ack",
            {"ok": True}, 0.02,
        ))
        await task

        sent = node._ws.only()
        assert "take_profit" not in sent
        assert "source_name" not in sent

    async def test_the_report_fields_are_sent_when_given(self, node):
        """Negative control for the test above."""
        task = asyncio.create_task(node.send_market_order(
            "BUY", take_profit=2000.0, source_name="ORB", timeout=1.0,
        ))
        asyncio.create_task(_ack(
            node, "_market_order_ack_event", "_last_market_order_ack",
            {"ok": True}, 0.02,
        ))
        await task

        sent = node._ws.only()
        assert sent["take_profit"] == 2000.0
        assert sent["source_name"] == "ORB"

    async def test_the_signal_order_carries_every_take_profit(self, node):
        """The VPS calls open_trade() with these exact values -- sizing and
        SL/TP were already resolved here. A ladder rung dropped in transit is
        a trade that runs past where it was meant to close."""
        tps = {f"tp{i}": 100.0 + i for i in range(1, 9)}
        task = asyncio.create_task(node.send_signal_order(
            "sig-1", "BUY", 1.0, 2.0, 0.5, lot_size=0.05,
            strategy="ladder", tg_source="chan", timeout=1.0, **tps,
        ))
        asyncio.create_task(_ack(
            node, "_signal_order_ack_event", "_last_signal_order_ack",
            {"ok": True}, 0.02,
        ))
        await task

        sent = node._ws.only()
        for key, value in tps.items():
            assert sent[key] == value, key
        assert sent["signal_id"] == "sig-1"
        assert sent["entry_low"] == 1.0 and sent["entry_high"] == 2.0
        assert sent["stop_loss"] == 0.5
        assert sent["lot_size"] == 0.05
        assert sent["tg_source"] == "chan"

    async def test_the_signal_order_lot_size_is_not_silently_defaulted_away(
        self, node,
    ):
        """0.01 is the signature default. A caller passing its own size must
        see its own size on the wire."""
        task = asyncio.create_task(node.send_signal_order(
            "sig-2", "SELL", 1.0, 2.0, 3.0, lot_size=0.5, timeout=1.0,
        ))
        asyncio.create_task(_ack(
            node, "_signal_order_ack_event", "_last_signal_order_ack",
            {"ok": True}, 0.02,
        ))
        await task

        assert node._ws.only()["lot_size"] == 0.5

    async def test_the_followup_carries_the_updates_untouched(self, node):
        updates = {"stop_loss": 1.5, "tp1": 9.0}
        task = asyncio.create_task(node.send_signal_followup(
            "chan", "BUY", updates, "tg-9", timeout=1.0,
        ))
        asyncio.create_task(_ack(
            node, "_signal_followup_ack_event", "_last_signal_followup_ack",
            {"matched": True}, 0.02,
        ))
        await task

        sent = node._ws.only()
        assert sent["updates"] == updates
        assert sent["channel_name"] == "chan" and sent["tg_id"] == "tg-9"


class TestPushTradeClosed:
    """The one that must NOT raise: it is called after a close has already
    happened locally, and a failure to tell the peer must not unwind that."""

    async def test_it_sends_the_trade_with_this_node_s_id(self, node, monkeypatch):
        monkeypatch.setattr(sc.db_module, "get_or_create_node_id", lambda: "node-A")

        await node.push_trade_closed({"id": "t1", "pnl": 12.0})

        sent = node._ws.only()
        assert sent["node_id"] == "node-A"
        assert sent["trade"] == {"id": "t1", "pnl": 12.0}

    async def test_a_dead_link_is_a_no_op_not_an_error(self, node):
        node.conn_state = sc.CONN_DISCONNECTED

        await node.push_trade_closed({"id": "t1"})

        assert node._ws.sent == []

    async def test_a_failing_send_is_swallowed(self, node, monkeypatch):
        """Deliberately unlike the order sends above. The close already
        happened; raising here would surface a network problem as a close
        failure and could unwind bookkeeping that is already correct."""
        monkeypatch.setattr(sc.db_module, "get_or_create_node_id", lambda: "node-A")
        node._ws = _Ws(fail=True)

        await node.push_trade_closed({"id": "t1"})
