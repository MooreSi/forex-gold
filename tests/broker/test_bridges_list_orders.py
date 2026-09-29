"""Every bridge can list the orders MT5 still holds (bug 070, 2026-09-29).

An order MT5 has sent and not heard back about ("started" in the Trade tab)
is neither a position nor a deal, so until this existed every broker read the
app made said "no trace" about it -- and the placeholder repair wrote such a
trade off as never filled while it could still fill.

`None` means the list could not be read; `[]` means MT5 holds no order. The
two must never meet (tests/services/broker/test_read_failure_is_not_an_answer.py
states why for positions).

Nothing here reaches a broker: the MetaTrader5 module is a stand-in, the HTTP
client's transport is monkeypatched, and the fake bridge has no network code.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

import mt5_orders
from backend.src.services.broker.fake_bridge import FakeMT5Bridge
from backend.src.services.broker.mt5_client import MT5BridgeClient
from backend.src.services.broker.mt5_native import NativeMT5Bridge

STARTED = SimpleNamespace(
    ticket=2107562994, type=0, state=0, comment="ea:0ed7581e-c326",
    volume_initial=0.02, volume_current=0.02, price_open=0.0,
    sl=4149.80, tp=4156.14, time_setup=1790693713, symbol="XAUUSD",
)


class _MT5:
    """The MetaTrader5 module's orders_get, and nothing else."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def orders_get(self, **kw):
        self.calls.append(kw)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


# ── mt5_orders.read ──────────────────────────────────────────────────────────

def test_an_order_mt5_holds_is_listed_with_its_comment():
    mt5 = _MT5((STARTED,))
    rows = mt5_orders.read(mt5, "XAUUSD", lambda: True)
    assert mt5.calls == [{"symbol": "XAUUSD"}]
    assert rows == [{
        "ticket": 2107562994, "type": 0, "state": 0, "comment": "ea:0ed7581e-c326",
        "volume": 0.02, "volume_current": 0.02, "price_open": 0.0,
        "sl": 4149.80, "tp": 4156.14, "setup_time": 1790693713,
    }]


def test_no_orders_is_an_empty_list():
    assert mt5_orders.read(_MT5(()), "XAUUSD", lambda: True) == []


def test_orders_get_returning_None_is_unknown():
    # MetaTrader5 returns None on an error, an empty tuple for "none".
    assert mt5_orders.read(_MT5(None), "XAUUSD", lambda: True) is None


def test_a_raising_orders_get_is_unknown():
    assert mt5_orders.read(_MT5(RuntimeError("ipc")), "XAUUSD", lambda: True) is None


def test_a_disconnected_terminal_is_unknown_and_is_not_asked():
    mt5 = _MT5((STARTED,))
    assert mt5_orders.read(mt5, "XAUUSD", lambda: False) is None
    assert mt5.calls == []


# ── The HTTP client (the Mac) ────────────────────────────────────────────────

@pytest.fixture
def http():
    c = MT5BridgeClient.__new__(MT5BridgeClient)
    c._url = "http://127.0.0.1:5001"
    c._http = None
    return c


def _respond(monkeypatch, client, status, body):
    async def _req(method, url, **kw):
        assert method == "get" and url.endswith("/orders")
        return httpx.Response(status, json=body)
    monkeypatch.setattr(client, "_request", _req)


def test_the_http_client_returns_the_bridges_orders(http, monkeypatch):
    _respond(monkeypatch, http, 200, {"orders": [{"ticket": 1, "comment": "ea:x"}]})
    assert asyncio.run(http.get_orders()) == [{"ticket": 1, "comment": "ea:x"}]


def test_a_bridge_that_predates_orders_is_unknown(http, monkeypatch):
    # An mt5_bridge.py started before /orders existed answers 404. That is
    # "could not look", not "nothing there".
    _respond(monkeypatch, http, 404, {"error": "Unknown path"})
    assert asyncio.run(http.get_orders()) is None


def test_a_failed_http_read_is_unknown(http, monkeypatch):
    async def _boom(*_a, **_kw):
        raise httpx.ReadTimeout("no answer")
    monkeypatch.setattr(http, "_request", _boom)
    assert asyncio.run(http.get_orders()) is None


def test_an_unconfigured_client_is_unknown():
    c = MT5BridgeClient.__new__(MT5BridgeClient)
    c._url = ""
    c._http = None
    assert asyncio.run(c.get_orders()) is None


# ── The in-process bridge (the VPS) ──────────────────────────────────────────

def _native(mod):
    b = NativeMT5Bridge.__new__(NativeMT5Bridge)
    b._mod = mod
    b._lock = asyncio.Lock()
    return b


def test_the_native_bridge_reads_through_its_own_module():
    mod = SimpleNamespace(mt5=_MT5((STARTED,)), SYMBOL="XAUUSD",
                          _ensure_connected=lambda: True)
    rows = asyncio.run(_native(mod).get_orders())
    assert [r["ticket"] for r in rows] == [2107562994]


def test_the_native_bridge_not_started_is_unknown():
    assert asyncio.run(_native(None).get_orders()) is None


def test_the_native_bridge_passes_unknown_through():
    mod = SimpleNamespace(mt5=_MT5(None), SYMBOL="XAUUSD",
                          _ensure_connected=lambda: True)
    assert asyncio.run(_native(mod).get_orders()) is None


# ── The fake ─────────────────────────────────────────────────────────────────

def test_the_fake_fills_at_once_so_holds_no_orders():
    bridge = FakeMT5Bridge(seed=1, clock=lambda: 1_700_000_000.0)
    assert asyncio.run(bridge.get_orders()) == []


def test_the_fake_can_be_handed_an_order_to_report():
    bridge = FakeMT5Bridge(seed=1, clock=lambda: 1_700_000_000.0)
    bridge.inject_error("get_orders", [{"ticket": 7, "comment": "ea:y"}])
    assert asyncio.run(bridge.get_orders()) == [{"ticket": 7, "comment": "ea:y"}]
