"""The native bridge's send paths, and why a timeout there is *definitely* unknown.

`NativeMT5Bridge` is the default on Windows: `is_available()` is `sys.platform
== "win32"`, `mt5_native_bridge_enabled` defaults to True, and `run.py`
explicitly skips launching `mt5_bridge.py` as a subprocess when it is on. The
HTTP client is the macOS path, where MT5 runs under Wine and the app's own
Python cannot import MetaTrader5 at all.

So this is the send path a Windows install actually uses, and it had the same
hole the HTTP client had: `except Exception: return {"error": str(e)}` made a
lost answer indistinguishable from a broker rejection, so `open_trade` raised
"MT5 order rejected" and the signal was handed back to the scheduler.

Here the timeout case is even clearer than over HTTP. `_call` runs the
MetaTrader5 function with `asyncio.wait_for(asyncio.to_thread(fn, ...))`.
`wait_for` cancels the *await*; it cannot stop the thread. **The MT5 call keeps
running to completion after the timeout fires.** So a timeout on `place_order`
means an order that is very likely still being sent — not one that failed.

The one thing that provably never ran is a bad function name: `getattr` raises
before anything is dispatched. That stays retryable.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.broker.mt5_native import NativeMT5Bridge

pytestmark = pytest.mark.asyncio


@pytest.fixture
def bridge():
    b = NativeMT5Bridge.__new__(NativeMT5Bridge)
    b._mod = object()
    b._lock = asyncio.Lock()
    b._tick_cache = None
    b._tick_cache_ts = 0.0
    b._candle_cache = {}
    b._candle_cache_ts = {}
    return b


def _raising(exc):
    async def _call(*_a, **_kw):
        raise exc
    return _call


_SENDS = ["place_order", "close_position", "partial_close", "modify_order"]


def _args(name):
    return {
        "place_order": ("BUY", 0.01, 3990.0, None, "py:abc"),
        "close_position": (424242,),
        "partial_close": (424242, 0.01),
        "modify_order": (424242, 3990.0, None),
    }[name]


class TestATimeoutIsUnknownOnEverySendPath:
    """`asyncio.to_thread` cannot be cancelled. After the timeout the MT5 call
    is still running, and for `place_order` that means the order may still be
    on its way to the broker."""

    @pytest.mark.parametrize("name", _SENDS)
    async def test_it_is_marked_unknown(self, bridge, monkeypatch, name):
        monkeypatch.setattr(bridge, "_call",
                            _raising(asyncio.TimeoutError("wedged")))

        out = await getattr(bridge, name)(*_args(name))

        assert out.get("unknown") is True, (
            f"{name} reported a timeout as an ordinary error, which open_trade "
            f"turns into a rejection and retries"
        )
        assert out.get("error")
        assert out.get("success") is not True


class TestAMissingFunctionProvablyNeverRan:
    """`getattr(self._mod, fn_name)` raises before anything is dispatched, so
    nothing reached the terminal. Marking it unknown would park signals over a
    plain programming error."""

    @pytest.mark.parametrize("name", _SENDS)
    async def test_it_stays_retryable(self, bridge, monkeypatch, name):
        monkeypatch.setattr(bridge, "_call",
                            _raising(AttributeError("no such bridge function")))

        out = await getattr(bridge, name)(*_args(name))

        assert out.get("error")
        assert not out.get("unknown")


class TestAnUnstartedBridgeIsRetryable:
    @pytest.mark.parametrize("name", _SENDS)
    async def test_nothing_was_sent(self, bridge, name):
        bridge._mod = None

        out = await getattr(bridge, name)(*_args(name))

        assert out.get("error")
        assert not out.get("unknown")


class TestAnythingElseIsTreatedAsUnknown:
    """The conservative direction. `_place_order` in the bridge module catches
    its own exceptions and returns dicts, so anything escaping `_call` happened
    around the call rather than inside it, and we cannot tell which side of the
    send it landed on."""

    @pytest.mark.parametrize("exc", [RuntimeError("terminal went away"),
                                     OSError("ipc channel closed")],
                             ids=lambda e: type(e).__name__)
    async def test_place_order_marks_it_unknown(self, bridge, monkeypatch, exc):
        monkeypatch.setattr(bridge, "_call", _raising(exc))

        out = await bridge.place_order("BUY", 0.01, 3990.0, None, "py:abc")

        assert out.get("unknown") is True


class TestSuccessAndRejectionArePassedThroughUnchanged:

    async def test_a_fill_is_untouched(self, bridge, monkeypatch):
        async def _ok(*_a, **_kw):
            return {"ticket": 424242, "fill_price": 4000.0}
        monkeypatch.setattr(bridge, "_call", _ok)

        out = await bridge.place_order("BUY", 0.01, 3990.0, None, "py:abc")

        assert out["ticket"] == 424242
        assert not out.get("unknown")

    async def test_a_broker_rejection_is_untouched(self, bridge, monkeypatch):
        """A retcode IS information: nothing filled, retrying is safe. Dressing
        it up as unknown parks a signal that should simply be retried."""
        async def _rejected(*_a, **_kw):
            return {"error": "Close failed retcode=10027: AutoTrading disabled"}
        monkeypatch.setattr(bridge, "_call", _rejected)

        out = await bridge.close_position(424242)

        assert out == {"error": "Close failed retcode=10027: AutoTrading disabled"}


class TestReadsAreUnaffected:
    async def test_a_tick_timeout_still_returns_None(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "_call",
                            _raising(asyncio.TimeoutError("wedged")))

        assert await bridge.get_fresh_tick() is None

    async def test_a_candle_timeout_still_returns_an_empty_list(self, bridge,
                                                                monkeypatch):
        monkeypatch.setattr(bridge, "_call",
                            _raising(asyncio.TimeoutError("wedged")))

        assert await bridge.get_candles("M5", 10) == []
