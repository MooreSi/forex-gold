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

    def __init__(self, partial_close_result=None):
        self._result = partial_close_result or {
            "success": True, "close_price": None, "lots_closed": None}
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
        return {"success": True}
