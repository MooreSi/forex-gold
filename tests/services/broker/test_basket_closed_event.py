"""The EA saying "I am about to close these as one basket".

`docs/todo/bugs/041`. A harvested leg reaches Python indistinguishable from a
manual close — the reason is derived from the deal comment and comes out
`MT5_close` either way — so the only way to tell is for the EA to say so
BEFORE it closes. `basket_closed` is that message, and this handler is all
Python needs to receive it.

Registering ahead of the closes is deliberate: the legs then arrive as ordinary
`trade_closed` events and are recognised by ticket, so no other part of the
close path changes shape.

**Inert until an EA that sends it is deployed.** Nothing sends this today, and
with nothing registered the breaker behaves exactly as it did.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.broker.ea_bridge import _events
from backend.src.services.risk import basket_breaker


class _Node(_events.EventsMixin):
    def __init__(self):
        self._engine = None
        self._active: dict = {}
        self._pending_orders: dict = {}


@pytest.fixture(autouse=True)
def clean():
    basket_breaker.forget_all()
    yield
    basket_breaker.forget_all()


def _dispatch(msg):
    asyncio.run(_Node()._dispatch(msg))


class TestItRegistersTheBasket:
    def test_a_basket_message_registers_its_tickets(self):
        _dispatch({"type": "basket_closed", "basket_id": "gh-1",
                   "net": 107.19, "tickets": [111, 222, 333]})

        # Proven through the behaviour it exists for: a losing leg of a
        # winning basket must not count against the breaker.
        assert basket_breaker.registered_ticket_count() == 3

    def test_the_net_is_carried_through(self):
        _dispatch({"type": "basket_closed", "basket_id": "gh-1",
                   "net": -40.0, "tickets": [111]})

        assert basket_breaker.basket_net("gh-1") == pytest.approx(-40.0)


class TestItRefusesNonsenseQuietly:
    """This runs on the EA's inbound path. A malformed message must not take
    the dispatcher down with it -- that would cost every later event too."""

    def test_no_tickets_is_not_a_basket(self):
        _dispatch({"type": "basket_closed", "basket_id": "gh-1",
                   "net": 10.0, "tickets": []})

        assert basket_breaker.registered_ticket_count() == 0

    def test_a_missing_basket_id_is_ignored(self):
        _dispatch({"type": "basket_closed", "net": 10.0, "tickets": [111]})

        assert basket_breaker.registered_ticket_count() == 0

    def test_a_junk_net_does_not_raise(self):
        _dispatch({"type": "basket_closed", "basket_id": "gh-1",
                   "net": "not a number", "tickets": [111]})

        assert basket_breaker.registered_ticket_count() == 0

    def test_an_unknown_message_type_is_still_ignored(self):
        """The dispatcher's existing contract, re-asserted beside a new
        branch: adding one must not turn an unknown type into an error."""
        _dispatch({"type": "something_new", "x": 1})

        assert basket_breaker.registered_ticket_count() == 0
