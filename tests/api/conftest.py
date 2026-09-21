"""Fakes for the API suite.

**No test under tests/api/ may reach a broker, live or demo.** The engine every
test here uses is `SentinelEngine`: it records the call and returns a canned
dict. It has no bridge, no network and no MT5 import, and
`test_orders_router.py::test_no_test_in_this_file_can_reach_a_real_engine`
asserts that rather than assuming it.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.src.api import deps
from backend.src.api.server import build_app


class SentinelEngine:
    """Records what it was asked to do and returns a canned answer.

    Deliberately not a Mock: a Mock answers every attribute, so a router that
    called `engine.place_the_order_please()` would pass a test written against
    `open_manual_market_order`. This raises AttributeError like the real thing.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.candles: list[dict] = []
        self.tick: Any = None
        self.account: dict = {"login": 123, "is_demo": True}
        self.health: dict = {"connected": True}
        self.open_trades: list = []
        self.positions_view: list | None = None
        self.raises: Exception | None = None

    def _record(self, name: str, args: tuple, kwargs: dict):
        self.calls.append((name, args, kwargs))
        if self.raises is not None:
            raise self.raises
        return {"ok": True, "call": name}

    def call_named(self, name: str) -> tuple[tuple, dict]:
        """The args of the one call to `name`. Fails loudly if it happened
        zero or several times — 'it was called' is a weaker claim than 'it was
        called once, with these arguments'."""
        matching = [(a, k) for n, a, k in self.calls if n == name]
        assert len(matching) == 1, f"{name} called {len(matching)} times: {self.calls}"
        return matching[0]

    # ── market data ──────────────────────────────────────────────────────────
    async def get_candles(self, timeframe: str = "M5", count: int = 200):
        self.calls.append(("get_candles", (timeframe, count), {}))
        return self.candles

    async def get_tick(self):
        self.calls.append(("get_tick", (), {}))
        return self.tick

    async def get_mt5_account(self):
        return self.account

    async def get_bridge_health(self):
        return self.health

    # ── money path — canned, never real ──────────────────────────────────────
    async def open_manual_market_order(self, *args, **kwargs):
        return self._record("open_manual_market_order", args, kwargs)

    async def open_manual_limit_order(self, *args, **kwargs):
        return self._record("open_manual_limit_order", args, kwargs)

    async def close_trade(self, *args, **kwargs):
        return self._record("close_trade", args, kwargs)

    async def partial_close_trade(self, *args, **kwargs):
        return self._record("partial_close_trade", args, kwargs)

    async def open_trade_from_signal(self, *args, **kwargs):
        return self._record("open_trade_from_signal", args, kwargs)

    async def cancel_signal(self, *args, **kwargs):
        return self._record("cancel_signal", args, kwargs)

    # ── reads that are not market data ───────────────────────────────────────
    def get_open_trades(self):
        """Synchronous, like the real runtime's. Used by the handover handler
        only to report how many positions keep running to their own SL/TP."""
        self.calls.append(("get_open_trades", (), {}))
        return self.open_trades

    async def get_open_positions_view(self, open_trades):
        """The Positions table's merged view. The real one asks the bridge for
        a running P&L and for positions the app has no record of; this returns
        whatever the test put in `positions_view`, defaulting to the stored
        rows so a test that does not care about the merge sees the table it
        always saw."""
        self.calls.append(("get_open_positions_view", (open_trades,), {}))
        return self.open_trades if self.positions_view is None else self.positions_view


@pytest.fixture
def sentinel_engine() -> SentinelEngine:
    return SentinelEngine()


@pytest.fixture
def make_client(sentinel_engine, tmp_path):
    """A TestClient over a freshly built app.

    `install_auth_gate` defaults to False for the routing and forwarding tests:
    they are about what a handler does, and threading a login through each one
    would test the gate over and over instead of the handler once. The gate has
    its own file, where it is on.
    """
    created = []

    def _make(*, auth: bool = False, bundle_dir=None, **kwargs):
        app = build_app(
            engine_provider=lambda: sentinel_engine,
            bundle_dir=bundle_dir if bundle_dir is not None else tmp_path / "no-bundle",
            install_auth_gate=auth,
            session_secret="test-secret",
            **kwargs,
        )
        client = TestClient(app, raise_server_exceptions=False)
        created.append(client)
        return client

    yield _make
    for c in created:
        c.close()
    # A provider that leaks into the next test is a live runtime in a test that
    # never asked for one.
    deps.clear_engine_provider()
