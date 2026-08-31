"""A lost response to `place_order` is not a rejection.

stage3/020's rule is that "the broker said no" and "nobody knows" are different
answers. It was implemented inside `mt5_bridge._place_order`, which flags
`unknown: True` when `mt5.order_send` returns None.

That covers a lost answer INSIDE the bridge process. It does not cover a lost
answer between the app and the bridge. `MT5Client.place_order` wraps its HTTP
call in `except Exception: return {"error": str(e)}` — so a read timeout, which
happens AFTER the request reached the bridge and may well have filled, arrives
at `open_trade` looking exactly like a broker rejection:

    _raise_if_send_unknown(mt5_result)      # only fires on unknown=True
    if mt5_result.get("error"):
        raise RuntimeError(f"MT5 order rejected: ...")

A RuntimeError is not a `SendOutcomeUnknown`, so `_route_failed_open` restores
the signal to `pending` and the scheduler sends it again — for an order that
may already be on the book.

The distinction that matters is which transport failure it was:

  * **Connect failed** (bridge not running, connection refused, connect
    timeout) — the request never left. Nothing filled. Retrying is safe, and
    marking it unknown would park signals every time the bridge is restarted.
  * **Sent, then no usable answer** (read timeout, connection dropped
    mid-response, malformed reply) — the bridge may have called `order_send`.
    Unknown.

Only the send paths need this. A lost tick or a lost candle is just a lost
read: the caller retries, and nothing was placed.
"""
from __future__ import annotations

import httpx
import pytest

from backend.src.services.broker.mt5_client import MT5BridgeClient

pytestmark = pytest.mark.asyncio


@pytest.fixture
def client():
    c = MT5BridgeClient.__new__(MT5BridgeClient)
    c._url = "http://127.0.0.1:5001"
    c._http = None
    return c


def _raising(exc):
    async def _request(*_a, **_kw):
        raise exc
    return _request


# Failures that mean the request never reached the bridge.
_NEVER_SENT = [
    httpx.ConnectError("connection refused"),
    httpx.ConnectTimeout("could not connect"),
]

# Failures that mean it may have been received and acted on.
_MAY_HAVE_LANDED = [
    httpx.ReadTimeout("no response within the timeout"),
    httpx.ReadError("connection dropped mid-response"),
    httpx.RemoteProtocolError("server disconnected without sending a response"),
]


class TestPlaceOrder:

    @pytest.mark.parametrize("exc", _MAY_HAVE_LANDED,
                             ids=lambda e: type(e).__name__)
    async def test_a_lost_ANSWER_is_marked_unknown(self, client, monkeypatch,
                                                   exc):
        """The order may be on the book. Anything that reads this as a
        rejection puts the signal back in the queue."""
        monkeypatch.setattr(client, "_request", _raising(exc))

        out = await client.place_order("BUY", 0.01, 3990.0, None, "py:abc")

        assert out.get("unknown") is True, (
            f"{type(exc).__name__} was reported as an ordinary error, which "
            f"open_trade turns into 'MT5 order rejected' and retries"
        )
        assert out.get("error")

    @pytest.mark.parametrize("exc", _NEVER_SENT,
                             ids=lambda e: type(e).__name__)
    async def test_a_failure_to_CONNECT_is_an_ordinary_error(self, client,
                                                             monkeypatch, exc):
        """Nothing was placed, so retrying is safe. Marking this unknown would
        park a signal every time the bridge is restarted, and only
        reconciliation could release it."""
        monkeypatch.setattr(client, "_request", _raising(exc))

        out = await client.place_order("BUY", 0.01, 3990.0, None, "py:abc")

        assert out.get("error")
        assert not out.get("unknown")

    async def test_an_unconfigured_bridge_is_an_ordinary_error(self, client):
        client._url = ""

        out = await client.place_order("BUY", 0.01, 3990.0, None, "py:abc")

        assert out.get("error")
        assert not out.get("unknown")

    async def test_a_broker_rejection_is_passed_through_unchanged(self, client,
                                                                  monkeypatch):
        """A retcode from the bridge IS information: nothing filled. It must
        not be dressed up as unknown, or a genuinely rejected signal parks
        instead of retrying."""
        async def _ok(*_a, **_kw):
            return httpx.Response(200, json={"error": "Invalid stops"})
        monkeypatch.setattr(client, "_request", _ok)

        out = await client.place_order("BUY", 0.01, 3990.0, None, "py:abc")

        assert out == {"error": "Invalid stops"}

    async def test_a_success_is_passed_through_unchanged(self, client,
                                                         monkeypatch):
        async def _ok(*_a, **_kw):
            return httpx.Response(200, json={"ticket": 424242,
                                             "fill_price": 4000.0})
        monkeypatch.setattr(client, "_request", _ok)

        out = await client.place_order("BUY", 0.01, 3990.0, None, "py:abc")

        assert out["ticket"] == 424242
        assert not out.get("unknown")


class TestTheSameAppliesToClosing:
    """A close whose answer was lost is equally unknown: recording it as closed
    would book a P&L that may not have happened, and reporting it as refused
    leaves a position the app thinks is still open. Either way the caller needs
    to know which it is."""

    @pytest.mark.parametrize("exc", _MAY_HAVE_LANDED,
                             ids=lambda e: type(e).__name__)
    async def test_a_lost_answer_is_marked_unknown(self, client, monkeypatch,
                                                   exc):
        monkeypatch.setattr(client, "_request", _raising(exc))

        out = await client.close_position(424242)

        assert out.get("unknown") is True
        assert not out.get("success")

    @pytest.mark.parametrize("exc", _NEVER_SENT,
                             ids=lambda e: type(e).__name__)
    async def test_a_failure_to_connect_is_an_ordinary_error(self, client,
                                                             monkeypatch, exc):
        monkeypatch.setattr(client, "_request", _raising(exc))

        out = await client.close_position(424242)

        assert out.get("error")
        assert not out.get("unknown")

    async def test_a_lost_answer_still_fails_the_success_check(self, client,
                                                               monkeypatch):
        """monitor_loop and the frozen close path both gate on `success`. An
        unknown close must not satisfy it."""
        monkeypatch.setattr(client, "_request",
                            _raising(httpx.ReadTimeout("gone")))

        out = await client.close_position(424242)

        assert out.get("success") is not True


class TestReadsAreNotAffected:
    """A lost tick or candle is a lost read. Nothing was placed, the caller
    retries, and flagging it would put `unknown` into paths that have no idea
    what to do with it."""

    async def test_a_tick_timeout_returns_None_as_before(self, client,
                                                         monkeypatch):
        monkeypatch.setattr(client, "_request",
                            _raising(httpx.ReadTimeout("gone")))

        assert await client.get_tick() is None

    async def test_a_candle_timeout_returns_an_empty_list_as_before(
            self, client, monkeypatch):
        monkeypatch.setattr(client, "_request",
                            _raising(httpx.ReadTimeout("gone")))

        assert await client.get_candles("M5", 10) == []
