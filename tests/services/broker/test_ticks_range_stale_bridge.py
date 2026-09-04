"""MT5Bridge.get_ticks_range must not report a STALE bridge as "no data".

From a live diagnosis on 2026-09-04. The bridge process running on this
machine had been started at 16:28:08 on 2026-09-03; mt5_bridge.py gained its
/ticks endpoint ten minutes later, at 16:38, in commit c919996. So every tick
request 404'd with {"error": "Unknown path"} against a bridge that was
otherwise healthy -- /tick, /candles, /account and /health all answered 200.

get_ticks_range swallowed the 404 and returned [], and the backtest page
rendered that as "No ticks returned -- ensure the bridge is connected". The
bridge WAS connected. The message sent the investigation to the Days field
and the one-day range cap, neither of which had anything to do with it.

A 404 on this endpoint has exactly one cause -- the bridge predates the
endpoint -- so it is worth naming. Every other failure keeps the old
empty-list behaviour: this feeds a page button, not a trading path, and a
network blip should stay a quiet empty result.
"""
import asyncio

import httpx
import pytest

from backend.src.services.broker.mt5_client import MT5BridgeClient


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.request = httpx.Request("GET", "http://bridge/ticks")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=self.request, response=self,
            )

    def json(self):
        return self._payload


def _bridge(response: _FakeResponse) -> MT5BridgeClient:
    b = MT5BridgeClient.__new__(MT5BridgeClient)
    b._url = "http://bridge"
    b._http = None

    async def _fake_request(method, url, timeout=10.0, **kwargs):
        return response

    b._request = _fake_request
    return b


def test_a_404_says_the_bridge_is_out_of_date_rather_than_returning_nothing():
    """The whole point: a stale bridge must be distinguishable from a quiet
    one, because the remedy (restart the bridge) is nothing like the remedy
    the old message implied."""
    bridge = _bridge(_FakeResponse(404, {"error": "Unknown path"}))

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(bridge.get_ticks_range(1_788_000_000, 1_788_003_600))

    msg = str(exc.value).lower()
    assert "restart" in msg
    assert "tick" in msg


def test_a_successful_fetch_still_returns_its_ticks():
    bridge = _bridge(_FakeResponse(200, {"count": 2, "ticks": [
        {"time": 1.0, "bid": 2400.0, "ask": 2400.2},
        {"time": 2.0, "bid": 2400.1, "ask": 2400.3},
    ]}))

    ticks = asyncio.run(bridge.get_ticks_range(1_788_000_000, 1_788_003_600))
    assert [t["bid"] for t in ticks] == [2400.0, 2400.1]


def test_an_empty_window_is_still_an_empty_list_not_an_error():
    """A weekend window legitimately has no ticks. That is data, not a fault."""
    bridge = _bridge(_FakeResponse(200, {"count": 0, "ticks": []}))
    assert asyncio.run(bridge.get_ticks_range(1_788_000_000, 1_788_003_600)) == []


def test_other_transport_failures_stay_quiet():
    """A 500 or a dropped connection is a blip on a page button, and the
    caller already renders an empty result sensibly. Only the 404 carries a
    diagnosis worth interrupting for."""
    bridge = _bridge(_FakeResponse(503))
    assert asyncio.run(bridge.get_ticks_range(1_788_000_000, 1_788_003_600)) == []


def test_no_configured_url_returns_empty():
    b = MT5BridgeClient.__new__(MT5BridgeClient)
    b._url = ""
    b._http = None
    assert asyncio.run(b.get_ticks_range(1.0, 2.0)) == []
