"""Shared test doubles.

`tests/refactor/test_fixture_dedup.py` has been counting ad-hoc `_FakeBridge`
classes since the 2026-08-11 dedup, with the note: *"ad-hoc fakes drift from
the real bridge surface... When phase-5's shared FakeMT5Bridge lands, new tests
use it instead of another local _FakeBridge."*

This is that shared fake, for the two shapes that were already identical across
fifteen files: one recording `modify_order`, and one recording both
`modify_order` and `partial_close`. The second is a superset of the first, so
one class covers both.

It is still named `_FakeBridge` so the ratchet keeps counting it. Hiding a
shared fake behind a name the regex does not match would make the number look
better without making the codebase better.

Genuine variants -- fakes that raise, return specific ticks, or model a broker
quirk a particular test needs -- stay in their own files. They are not
duplication.
"""
from __future__ import annotations


class _FakeBridge:
    """Records the two calls the position handlers make against a broker.

    `partial_close` fills in `lots_closed` from the request when the canned
    result does not state one, and drops `close_price` when it is None, which
    is what the handlers under test expect to receive back.
    """

    def __init__(self, partial_close_result=None, *,
                 modify_order_result=None, modify_order_raises=False):
        self._result = partial_close_result or {
            "success": True, "close_price": None, "lots_closed": None}
        # modify_order defaults to success, which is what every existing caller
        # expects. The overrides exist because the broker reports a REFUSAL by
        # returning an error dict rather than raising -- see
        # tests/core/test_fixed_rr_post_fill_override.py, and the live incident
        # its source comment points at.
        self._modify_result = modify_order_result or {"success": True}
        self._modify_raises = modify_order_raises
        self.partial_close_calls = []
        self.modify_order_calls = []

    async def partial_close(self, ticket, lots):
        self.partial_close_calls.append({"ticket": ticket, "lots": lots})
        result = dict(self._result)
        if result.get("lots_closed") is None:
            result["lots_closed"] = lots
        if result.get("close_price") is None:
            result.pop("close_price", None)
        return result

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        if self._modify_raises:
            raise RuntimeError("bridge died mid-call")
        return dict(self._modify_result)


class _ReconciliationBridge:
    """A read-only broker double: canned positions, health and deal history.

    Separate from `_FakeBridge` above rather than folded into it, and named for
    what it is rather than to duck the ratchet in
    tests/refactor/test_fixture_dedup.py. Two reasons:

      * `_FakeBridge` is the double for the POSITION HANDLERS -- fifteen files'
        worth -- and it deliberately cannot close a position. Teaching it the
        broker's read surface would also mean teaching it get_account, which
        changes what get_trading_balance() returns under all fifteen.
      * this shape is the reconciliation/close-path one: is the ticket still
        there, what did the deal history say, what is the account worth. A test
        that needs it should take it from here instead of writing the 51st
        local copy, which is exactly what the ratchet is asking for.

    It has no close_position and no order placement at all. A test that needs a
    CONFIRMED broker close subclasses it and says so.
    """

    def __init__(self, positions=None, deal_history=None, position_history=None,
                 account=None, tick=None, configured=True):
        self._positions = positions if positions is not None else []
        self._deal_history = deal_history or []
        self._position_history = position_history
        self._account = account if account is not None else {"balance": 1000.0}
        self._tick = tick
        self._configured = configured

    def is_configured(self):
        return self._configured

    async def get_positions(self):
        return self._positions

    async def get_health(self):
        return {"connected": True}

    async def get_deal_history(self, days):
        return self._deal_history

    async def get_position_history(self, ticket):
        return self._position_history if self._position_history is not None else []

    async def get_account(self):
        return self._account

    async def get_tick(self):
        return self._tick
