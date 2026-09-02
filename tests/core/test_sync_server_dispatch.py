"""Routing on the VPS: which message reaches which handler, and no other.

`SyncServer._dispatch` is a seventeen-branch elif chain and the whole of the
trading node's inbound behaviour. Three of those branches place or modify
orders (`MSG_MARKET_ORDER`, `MSG_SIGNAL_ORDER`, `MSG_SIGNAL_FOLLOWUP`), one
stands the node down, and one changes its risk settings.

A copy-paste slip in a chain this long is invisible: every branch is two lines
that look like every other pair, the message still gets "handled", and the
wrong thing happens. Routing a settings proposal into the market-order handler
is not a crash, it is an order.

The equivalent for the Mac is pinned in test_sync_client_dispatch.py. This is
the side that trades.

Every handler is replaced by a recorder; nothing here reaches a broker, a
database or a socket.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync import protocol as P

pytestmark = pytest.mark.asyncio

# message type -> the handler it must reach, and no other.
ROUTES = [
    (P.MSG_SETTINGS_PROPOSE, "_handle_settings_propose"),
    (P.MSG_CHANNEL_STRATEGY_PROPOSE, "_handle_channel_strategy_propose"),
    (P.MSG_TRADING_SCHEDULE_PROPOSE, "_handle_trading_schedule_propose"),
    (P.MSG_STRATEGY_PARAMS_PROPOSE, "_handle_strategy_params_propose"),
    (P.MSG_STAND_DOWN, "_handle_stand_down"),
    (P.MSG_RESUME, "_handle_resume"),
    (P.MSG_TRADE_CLOSED, "_handle_trade_closed"),
    (P.MSG_LEARNED_RULE_SYNC, "_handle_learned_rule_sync"),
    (P.MSG_AI_CONFIG_SYNC, "_handle_ai_config_sync"),
    (P.MSG_LEDGER_PULL, "_handle_ledger_pull"),
    (P.MSG_AI_RECOVERED_SIGNAL_SYNC, "_handle_ai_recovered_signal_sync"),
    (P.MSG_AI_RECOVERED_PULL, "_handle_ai_recovered_pull"),
    (P.MSG_MODEL_SNAPSHOT_REQUEST, "_handle_model_snapshot_request"),
    (P.MSG_ENGINE_CONTROL, "_handle_engine_control"),
    (P.MSG_MARKET_ORDER, "_handle_market_order"),
    (P.MSG_SIGNAL_ORDER, "_handle_signal_order"),
    (P.MSG_SIGNAL_FOLLOWUP, "_handle_signal_followup"),
]
HANDLERS = [h for _t, h in ROUTES]
IDS = [t for t, _h in ROUTES]

# The three that place or modify an order. A message routed into one of these
# by mistake is not a crash, it is a trade.
ORDER_HANDLERS = {"_handle_market_order", "_handle_signal_order",
                  "_handle_signal_followup"}


class _Ws:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw):
        self.sent.append(raw)


@pytest.fixture
def node(monkeypatch):
    srv = ss.SyncServer.__new__(ss.SyncServer)
    called: list = []

    for name in HANDLERS:
        async def _async(*a, _n=name):
            called.append(_n)

        def _sync(*a, _n=name):
            called.append(_n)

        import inspect
        real = getattr(ss.SyncServer, name)
        monkeypatch.setattr(srv, name,
                            _async if inspect.iscoroutinefunction(real) else _sync,
                            raising=False)
    monkeypatch.setattr(srv, "_apply_peer_clock_offset",
                        lambda msg: called.append("_apply_peer_clock_offset"),
                        raising=False)
    srv.called = called
    return srv


@pytest.mark.parametrize("msg_type,handler", ROUTES, ids=IDS)
class TestEachMessageReachesItsOwnHandler:
    async def test_it_reaches_the_right_one(self, node, msg_type, handler):
        await node._dispatch(_Ws(), {"type": msg_type})

        assert handler in node.called

    async def test_it_reaches_no_other(self, node, msg_type, handler):
        """The one that matters. Every branch here is two lines that look
        like every other pair, and a slip still "handles" the message."""
        await node._dispatch(_Ws(), {"type": msg_type})

        assert node.called == [handler], node.called

    async def test_it_does_not_reach_an_order_handler(self, node, msg_type,
                                                       handler):
        """Stated separately because it is the consequence, not the rule:
        three of these branches place or modify a trade."""
        await node._dispatch(_Ws(), {"type": msg_type})

        strayed = ORDER_HANDLERS.intersection(node.called) - {handler}

        assert strayed == set(), strayed


class TestThePing:
    async def test_it_is_ponged(self, node):
        ws = _Ws()

        await node._dispatch(ws, {"type": P.MSG_PING})

        assert ws.sent

    async def test_it_applies_the_peer_clock_offset(self, node):
        """The ping carries the Mac's UTC offset so a link that stays up
        across a clock change still follows it -- see
        test_clock_offset_sync.py."""
        await node._dispatch(_Ws(), {"type": P.MSG_PING})

        assert "_apply_peer_clock_offset" in node.called

    async def test_it_reaches_no_handler(self, node):
        await node._dispatch(_Ws(), {"type": P.MSG_PING})

        assert not set(HANDLERS).intersection(node.called)


class TestAnythingElse:
    async def test_an_unknown_type_reaches_nothing(self, node):
        """The Mac may be a newer build sending a type this VPS has never
        heard of. That must not route into the nearest branch."""
        await node._dispatch(_Ws(), {"type": "MSG_FROM_A_LATER_VERSION"})

        assert node.called == []

    async def test_a_message_with_no_type_reaches_nothing(self, node):
        await node._dispatch(_Ws(), {})

        assert node.called == []

    async def test_every_route_in_this_table_still_exists(self):
        """The table is the test's own premise. A handler renamed without
        updating it would leave a branch silently unchecked."""
        for _t, handler in ROUTES:
            assert hasattr(ss.SyncServer, handler), handler

    async def test_the_table_covers_every_branch_in_the_chain(self):
        """And the other direction: a NEW branch added to _dispatch without a
        row here is a route nothing checks."""
        import inspect
        import re

        src = inspect.getsource(ss.SyncServer._dispatch)
        branches = set(re.findall(r"self\.(_handle_\w+)", src))

        assert branches == set(HANDLERS), branches.symmetric_difference(HANDLERS)
