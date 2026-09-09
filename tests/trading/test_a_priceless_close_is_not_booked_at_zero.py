"""An EA close with no price must not become a $0.00 exit.

bugs/025, live on 2026-09-04, ticket 1935433548: entry 4478.35, 0.1 lots. The
EA's restore path reported the ticket as gone with no `close_price`, that
absence was read as 0.0, and `record_close` computed **-$44,783.50** — writing
it to `net_pnl`, `realised_pnl` and the simulated balance, and feeding it to the
daily-loss and give-back guards, which halt trading. The broker had no closing
deal at all, which is why History never showed it.

Two more of these were found on 2026-09-09 in the same database, and seven in
the pre-migration one (reversal-engine/010).

**The guard had no test.** It is asserted here through the real
`_on_trade_closed` handler, with the broker and the engine faked at their own
boundaries, because the whole point is what happens when the two disagree.

The runbook's demo 6 covers this at a terminal. This is the offline half that
was missing.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.broker.ea_bridge import _events


class _Engine:
    def __init__(self, deal_price=None):
        self._bridge = object()
        self.recorded = []
        self._deal_price = deal_price

    async def record_close(self, trade_id, close_price, reason):
        self.recorded.append((trade_id, close_price, reason))
        return {"already_closed": False}

    async def get_mt5_account(self):
        return {"balance": 1000.0}


class _Handler(_events.EventsMixin):
    """The mixin under test, with only the collaborators it reaches."""

    def __init__(self, engine, broker_price):
        self._engine = engine
        self._broker_price = broker_price
        self._active = {}
        self.alerts = []

    async def _fetch_trade(self, trade_id):
        return {"trade_id": trade_id, "mt5_ticket": 1935433548,
                "status": "open", "strategy": "template:X",
                "entry_price": 4478.35, "channel_name": "GD", "tg_source": "GD"}

    async def _broker_exit_price(self, ticket):
        return self._broker_price


@pytest.fixture
def alerts(monkeypatch):
    sent = []

    async def _send(text, trade_id=None, event_type=None, **kw):
        sent.append((event_type, text))
    monkeypatch.setattr(_events.telegram_alerts, "send_message", _send)
    return sent


def _closed_msg(price):
    msg = {"type": "trade_closed", "trade_id": "aaaaaaaa-bbbb-cccc",
           "ticket": 1935433548, "reason": "closed_while_disconnected"}
    if price is not None:
        msg["close_price"] = price
    return msg


class TestWhenTheBrokerHasNoClosingDeal:
    def test_nothing_is_recorded(self, alerts):
        """The whole bug: record_close must not be reached with 0.0."""
        engine = _Engine()
        h = _Handler(engine, broker_price=None)

        asyncio.run(h._on_trade_closed(_closed_msg(0.0)))

        assert engine.recorded == [], (
            f"a close was booked anyway: {engine.recorded}"
        )

    def test_the_operator_is_told(self, alerts):
        """A row silently left open is its own failure — bugs/016 was a
        phantom open row nobody noticed for 26 hours."""
        h = _Handler(_Engine(), broker_price=None)

        asyncio.run(h._on_trade_closed(_closed_msg(0.0)))

        assert any(ev == "ea_close_unverified" for ev, _ in alerts), (
            "the trade was left open with no alert"
        )

    def test_a_missing_close_price_key_is_treated_the_same(self, alerts):
        """The live message did not carry the field at all; reading its
        absence as 0.0 is what started this."""
        engine = _Engine()
        h = _Handler(engine, broker_price=None)

        asyncio.run(h._on_trade_closed(_closed_msg(None)))

        assert engine.recorded == []


class TestWhenTheBrokerDOESHaveADeal:
    def test_the_brokers_price_is_used_not_zero(self, alerts):
        """The guard must not refuse a real close — that would leave genuinely
        closed trades open forever."""
        engine = _Engine()
        h = _Handler(engine, broker_price=4470.10)

        asyncio.run(h._on_trade_closed(_closed_msg(0.0)))

        assert len(engine.recorded) == 1
        assert engine.recorded[0][1] == 4470.10, (
            "the close was booked at something other than the broker's price"
        )


class TestTheOrdinaryPath:
    def test_a_normal_close_still_books_at_its_own_price(self, alerts):
        """The control. Without it, a guard that refused EVERY close would
        pass every test above."""
        engine = _Engine()
        h = _Handler(engine, broker_price=None)

        asyncio.run(h._on_trade_closed(_closed_msg(4465.55)))

        assert len(engine.recorded) == 1
        assert engine.recorded[0][1] == 4465.55
