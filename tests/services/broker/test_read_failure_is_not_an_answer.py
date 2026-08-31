"""A failed broker read must not look like an empty broker.

`dedup.find_trade` states the contract in its own docstring:

    None means "could not look"; [] means "nothing there". Treating them
    alike is the mistake this whole module exists to prevent.

It branches on both. **Until 2026-08-31 neither real client could ever produce
the None.** `MT5BridgeClient.get_positions` and `NativeMT5Bridge.get_positions`
swallowed every failure and returned `[]`, so a failed position query read as
"the broker has no record of this trade" and the order was sent again.

That defeated stage3/010's duplicate-order guard on the only two clients
production uses. The dedup unit tests passed throughout, because their fake
bridge raises or returns None — shapes the real clients never produced.

This is the third instance of the same root cause found today, after
`place_order`'s lost response and `send_outcome_is_unknown` not knowing about
httpx: something that could not be asked being recorded as an answer.
"""
from __future__ import annotations

import httpx
import pytest

from backend.src.services.broker import dedup
from backend.src.services.broker.mt5_client import MT5BridgeClient
from backend.src.services.broker.mt5_native import NativeMT5Bridge

pytestmark = pytest.mark.asyncio

_READS = ["get_positions", "get_deal_history", "get_position_history"]


def _args(name):
    return {"get_positions": (), "get_deal_history": (7,),
            "get_position_history": (424242,)}[name]


@pytest.fixture
def http():
    c = MT5BridgeClient.__new__(MT5BridgeClient)
    c._url = "http://127.0.0.1:5001"
    c._http = None
    return c


@pytest.fixture
def native():
    b = NativeMT5Bridge.__new__(NativeMT5Bridge)
    b._mod = object()
    return b


class TestTheHttpClientTellsTheDifference:

    @pytest.mark.parametrize("name", _READS)
    async def test_a_failed_read_is_None(self, http, monkeypatch, name):
        async def _boom(*_a, **_kw):
            raise httpx.ReadTimeout("no answer")
        monkeypatch.setattr(http, "_request", _boom)

        assert await getattr(http, name)(*_args(name)) is None

    @pytest.mark.parametrize("name", _READS)
    async def test_a_non_200_is_None(self, http, monkeypatch, name):
        async def _bad(*_a, **_kw):
            return httpx.Response(503, text="bridge is restarting")
        monkeypatch.setattr(http, "_request", _bad)

        assert await getattr(http, name)(*_args(name)) is None

    @pytest.mark.parametrize("name", _READS)
    async def test_an_unconfigured_bridge_is_None(self, http, name):
        http._url = ""

        assert await getattr(http, name)(*_args(name)) is None

    async def test_a_GENUINELY_EMPTY_broker_is_an_empty_list(self, http,
                                                             monkeypatch):
        """The other half. If a real answer of "nothing open" also came back as
        None, dedup would park every signal and trading would stop."""
        async def _ok(*_a, **_kw):
            return httpx.Response(200, json={"positions": []})
        monkeypatch.setattr(http, "_request", _ok)

        assert await http.get_positions() == []

    async def test_a_populated_answer_comes_through(self, http, monkeypatch):
        async def _ok(*_a, **_kw):
            return httpx.Response(200, json={"positions": [{"ticket": 1}]})
        monkeypatch.setattr(http, "_request", _ok)

        assert await http.get_positions() == [{"ticket": 1}]


class TestTheNativeClientTellsTheDifferenceToo:

    @pytest.mark.parametrize("name", _READS)
    async def test_a_failed_read_is_None(self, native, monkeypatch, name):
        async def _boom(*_a, **_kw):
            raise RuntimeError("terminal gone")
        monkeypatch.setattr(native, "_call", _boom)

        assert await getattr(native, name)(*_args(name)) is None

    @pytest.mark.parametrize("name", _READS)
    async def test_an_unstarted_bridge_is_None(self, native, name):
        native._mod = None

        assert await getattr(native, name)(*_args(name)) is None

    async def test_a_None_FROM_THE_BRIDGE_stays_None(self, native, monkeypatch):
        """`_call(...) or []` swallowed it twice over: an exception AND a
        genuine None from the bridge function both became []."""
        async def _none(*_a, **_kw):
            return None
        monkeypatch.setattr(native, "_call", _none)

        assert await native.get_positions() is None

    async def test_a_genuinely_empty_broker_is_an_empty_list(self, native,
                                                             monkeypatch):
        async def _empty(*_a, **_kw):
            return []
        monkeypatch.setattr(native, "_call", _empty)

        assert await native.get_positions() == []


class TestTheGuardNowWorksThroughARealClient:
    """The joined-up case, and the one that was actually broken. Driven through
    the real client rather than a fake, because the fake was the reason this
    went unnoticed."""

    async def test_a_failed_position_read_is_UNKNOWN_not_absent(self, http,
                                                                monkeypatch):
        async def _boom(*_a, **_kw):
            raise httpx.ReadTimeout("no answer")
        monkeypatch.setattr(http, "_request", _boom)

        res = await dedup.find_trade(http, "abc123def4")

        assert res.unknown is True
        assert res.safe_to_send is False, (
            "a failed position read was treated as 'the broker does not have "
            "this trade', and the order would be sent again"
        )

    async def test_a_failed_DEAL_read_is_unknown_too(self, http, monkeypatch):
        """A trade can fill AND close while an ack is outstanding, so the deal
        history is the second half of the same question."""
        calls = {"n": 0}

        async def _positions_ok_then_fail(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json={"positions": []})
            raise httpx.ReadTimeout("no answer")
        monkeypatch.setattr(http, "_request", _positions_ok_then_fail)

        res = await dedup.find_trade(http, "abc123def4")

        assert res.unknown is True

    async def test_a_REACHABLE_broker_with_nothing_is_still_safe_to_send(
            self, http, monkeypatch):
        """The control that matters most. If an empty broker also read as
        unknown, every signal would park and the app would stop trading."""
        async def _empty(*_a, **_kw):
            return httpx.Response(200, json={"positions": [], "history": []})
        monkeypatch.setattr(http, "_request", _empty)

        res = await dedup.find_trade(http, "abc123def4")

        assert res.unknown is False
        assert res.found is False
        assert res.safe_to_send is True

    async def test_an_existing_order_is_still_FOUND(self, http, monkeypatch):
        async def _has_it(*_a, **_kw):
            return httpx.Response(200, json={"positions": [
                {"ticket": 555, "comment": "py:abc123def4", "open_price": 2400.0},
            ]})
        monkeypatch.setattr(http, "_request", _has_it)

        res = await dedup.find_trade(http, "abc123def4")

        assert res.found is True
        assert res.ticket == 555


class TestTheCloseSyncSkipsRatherThanClosingEverything:
    """`sync_closed_mt5_positions` decides whether a trade has CLOSED by its
    absence from the broker's position list. A failed read that arrives as an
    empty list therefore means *every* open trade looks closed — recorded shut
    in the database, with a Telegram alert for each.

    That is the single worst consequence of the swallowing, which is why it has
    its own test rather than relying on the client-level one.
    """

    def _ctx(self, positions, health=None):
        from backend.src.services.broker.position_sync import PositionSyncCtx

        class _Bridge:
            def is_configured(self):
                return True

            async def get_positions(self):
                return positions

            async def get_health(self):
                return health if health is not None else {"connected": True}

            async def get_deal_history(self, days):
                return []

        return PositionSyncCtx(bridge=_Bridge())

    async def _run(self, ctx, monkeypatch, open_trades):
        from backend.src.services.broker import position_sync as ps
        from backend.src.db import database as db_module

        closed: list = []

        async def _to_db_thread(fn, *a, **kw):
            return fn(*a, **kw)
        monkeypatch.setattr(db_module, "to_db_thread", _to_db_thread)
        monkeypatch.setattr(ps._broker_repo, "fetch_python_managed_open_trades",
                            lambda: open_trades)

        async def _record_close(*a, **kw):
            closed.append(a)
            return {}
        ctx.record_close = _record_close

        async def _tick():
            return None
        ctx.get_tick = _tick

        await ps.sync_closed_mt5_positions(ctx)
        return closed

    async def test_a_FAILED_read_closes_nothing(self, monkeypatch):
        trades = [{"trade_id": "t1", "mt5_ticket": 111, "status": "open"},
                  {"trade_id": "t2", "mt5_ticket": 222, "status": "open"}]

        closed = await self._run(self._ctx(positions=None), monkeypatch, trades)

        assert closed == [], (
            "a failed position read closed every open trade in the database"
        )

    async def test_a_GENUINELY_EMPTY_broker_on_a_connected_bridge_proceeds(
            self, monkeypatch, fresh_db):
        """Control: the sync must still do its job when the broker really has
        nothing open, or a closed trade is never recorded."""
        trades = [{"trade_id": "t1", "mt5_ticket": 111, "status": "open"}]
        ctx = self._ctx(positions=[], health={"connected": True})

        # It reaches the per-trade logic rather than returning early; the miss
        # threshold means nothing is closed on the first pass, which is the
        # documented behaviour and not what this test is about.
        await self._run(ctx, monkeypatch, trades)

        assert ctx.mt5_sync_missing_streak.get("t1") == 1, (
            "the sync returned early on a genuinely empty broker"
        )

    async def test_an_empty_list_from_a_DISCONNECTED_bridge_still_skips(
            self, monkeypatch):
        """The older workaround for the same ambiguity, kept because a
        disconnected-but-responding bridge can still answer with []."""
        trades = [{"trade_id": "t1", "mt5_ticket": 111, "status": "open"}]
        ctx = self._ctx(positions=[], health={"connected": False})

        await self._run(ctx, monkeypatch, trades)

        assert ctx.mt5_sync_missing_streak == {}
