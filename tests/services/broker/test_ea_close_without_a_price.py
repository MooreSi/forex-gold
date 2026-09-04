"""A `trade_closed` that carries no price is an observation, not an exit.

Live, 2026-09-04, ticket 1935433548. The bridge restarted; the app pushed its
open EA-managed rows back to the EA (`restore_trade`); the EA could not select
that ticket as a position and replied with

    {"type":"trade_closed","trade_id":"...","ticket":1935433548,
     "reason":"closed_while_disconnected"}

and nothing else. No `close_price` -- HandleRestoreTrade is the only sender
that omits it; every real close goes through ReportTradeClosed, which always
sends one. `float(msg.get("close_price", 0))` read the absence as an exit at
$0.00, and record_close computed `(0 - 4478.35) x 0.1 x 100` = **-$44,783.50**
on a 0.1-lot trade. That figure went into net_pnl, into realised_pnl, into
vantage_simulation_account.balance, and into the daily-loss and give-back
guards, which halt trading. Telegram announced it. The broker had no closing
deal for the ticket at all, which is why History -- built from MT5 deal
history -- never showed the trade.

The rule this file pins: with no price from the EA, ask the BROKER for the
closing deal. Its price, or no close at all. "The EA cannot see the ticket" and
"the position is gone" are different facts, and only the broker settles the
second one.

Nothing here reaches a broker or an order: `record_close` is the frozen close
path and is recorded rather than run, and the only bridge is a fake with a
canned deal list.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.broker.ea_bridge import _events

pytestmark = pytest.mark.asyncio

TRADE  = "9f2c4ab1"
TICKET = 1935433548
ENTRY  = 4478.35


def _deal(entry: int, price: float, ts: float, volume: float = 0.1) -> dict:
    """One MT5 deal. `entry` 0 is the opening deal; anything else is an exit."""
    return {"position_id": TICKET, "entry": entry, "price": price, "time": ts,
            "volume": volume, "profit": 0.0, "swap": 0.0, "fee": 0.0}


class _Bridge:
    """Deal history only -- the fake has no way to place, close or modify
    anything, which is the point."""

    def __init__(self, deals=None, raises: bool = False):
        self._deals = deals if deals is not None else []
        self._raises = raises
        self.history_calls: list = []

    async def get_position_history(self, ticket: int) -> list:
        self.history_calls.append(int(ticket))
        if self._raises:
            raise RuntimeError("bridge offline")
        return [d for d in self._deals if int(d["position_id"]) == int(ticket)]


class _Engine:
    def __init__(self, bridge):
        self._bridge = bridge
        self.record_close_calls: list = []
        self.profit_syncs: list = []

    async def record_close(self, trade_id: str, close_price: float, reason: str) -> dict:
        self.record_close_calls.append((trade_id, close_price, reason))
        return {"trade_id": trade_id, "close_price": close_price,
                "gross_pnl": 0.0, "net_pnl": 0.0, "reason": reason}

    async def get_mt5_account(self) -> dict:
        return {"balance": 5000.0, "equity": 5000.0, "margin_free": 4800.0}

    async def schedule_profit_sync(self, trade_id: str, ticket: int) -> None:
        self.profit_syncs.append((trade_id, int(ticket)))


class _Node(_events.EventsMixin):
    """EABridge stripped to what this handler touches."""

    def __init__(self, bridge, row=None):
        self._engine = _Engine(bridge)
        self._active: dict = {}
        self._row = row if row is not None else _row()

    async def _fetch_trade(self, trade_id):
        return self._row if trade_id == self._row["trade_id"] else None


def _row(**over) -> dict:
    row = {
        "trade_id": TRADE, "status": "open", "mt5_ticket": TICKET,
        "direction": "BUY", "lot_size": 0.1, "remaining_lots": 0.1,
        "entry_price": ENTRY, "stop_loss": 4428.35, "initial_sl": 4428.35,
        "initial_risk": 500.0, "tp1": 4508.35, "mt5_profit": None,
        "strategy": "template:30 TP1 SL50 and Trail", "tg_source": "Reversal Engine",
        "activated_at": 1_757_000_000.0, "close_time": 1_757_003_600.0,
        "net_pnl": 0.0, "realised_pnl": 0.0,
    }
    row.update(over)
    return row


@pytest.fixture
def alerts(monkeypatch):
    sent: list = []

    async def _send(text, tid=None, kind=None):
        sent.append({"text": text, "trade_id": tid, "kind": kind})
    monkeypatch.setattr(_events.telegram_alerts, "send_message", _send)
    return sent


async def _closed(node, **over):
    """Run the handler, then let its fire-and-forget alerts actually run."""
    msg = {"type": "trade_closed", "trade_id": TRADE, "ticket": TICKET,
           "reason": "closed_while_disconnected"}
    msg.update(over)
    await node._on_trade_closed(msg)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestACloseThatCarriesItsOwnPrice:
    """The negative control. Without it every test below would pass against a
    guard that simply refused to close anything."""

    async def test_it_is_recorded_at_the_price_the_ea_sent(self, alerts):
        node = _Node(_Bridge())

        await _closed(node, close_price=4490.10, reason="TP")

        assert node._engine.record_close_calls == [(TRADE, 4490.10, "TP")]

    async def test_the_broker_is_not_consulted_when_the_ea_sent_a_price(self, alerts):
        """A normal close must not depend on deal history being reachable --
        the EA watched the position exit and its price is the fact."""
        bridge = _Bridge()
        node = _Node(bridge)

        await _closed(node, close_price=4490.10, reason="TP")

        assert bridge.history_calls == []


class TestACloseWithNoPriceAtAll:
    """`closed_while_disconnected` -- the restore path, which sends no price."""

    async def test_the_brokers_own_closing_deal_settles_the_price(self, alerts):
        node = _Node(_Bridge([_deal(entry=0, price=ENTRY, ts=1_757_000_000.0),
                              _deal(entry=1, price=4471.05, ts=1_757_003_600.0)]))

        await _closed(node)

        assert node._engine.record_close_calls == [
            (TRADE, 4471.05, "closed_while_disconnected")]

    async def test_the_last_closing_deal_wins_when_the_exit_was_staged(self, alerts):
        """A laddered exit leaves several closing deals. The trade is out at
        the last of them, not the first."""
        node = _Node(_Bridge([_deal(entry=0, price=ENTRY, ts=1_757_000_000.0),
                              _deal(entry=1, price=4482.00, ts=1_757_001_000.0, volume=0.05),
                              _deal(entry=1, price=4495.50, ts=1_757_003_600.0, volume=0.05)]))

        await _closed(node)

        assert node._engine.record_close_calls == [
            (TRADE, 4495.50, "closed_while_disconnected")]

    async def test_no_closing_deal_means_no_close_is_recorded(self, alerts):
        """The live incident. The EA could not see the ticket; the broker has
        no exit for it. Leave the row alone -- an unmanaged open position is
        recoverable, a fabricated -$44,783 loss in the books is not."""
        node = _Node(_Bridge([_deal(entry=0, price=ENTRY, ts=1_757_000_000.0)]))

        await _closed(node)

        assert node._engine.record_close_calls == []

    async def test_no_closing_deal_says_so_instead_of_announcing_a_close(self, alerts):
        node = _Node(_Bridge([_deal(entry=0, price=ENTRY, ts=1_757_000_000.0)]))

        await _closed(node)

        assert [a["kind"] for a in alerts] == ["ea_close_unverified"]
        assert str(TICKET) in alerts[0]["text"]

    async def test_the_fabricated_loss_never_reaches_telegram(self, alerts):
        """The exact figure Simon was sent on 2026-09-04, from entry 4478.35
        against an exit of $0.00 at 0.1 lots."""
        node = _Node(_Bridge([_deal(entry=0, price=ENTRY, ts=1_757_000_000.0)]))

        await _closed(node)

        assert not any("44783" in a["text"] for a in alerts)

    async def test_an_explicit_zero_is_treated_the_same_as_no_price(self, alerts):
        """$0.00 is not an exit price for gold. Whether the EA omits the field
        or sends a zero in it, the app has no price either way."""
        node = _Node(_Bridge([_deal(entry=0, price=ENTRY, ts=1_757_000_000.0)]))

        await _closed(node, close_price=0.0)

        assert node._engine.record_close_calls == []

    async def test_an_opening_deal_alone_is_not_evidence_of_a_close(self, alerts):
        """Read as an exit it would book the trade shut at its own entry, for
        a tidy $0.00 -- wrong, and wrong in the direction that looks fine."""
        node = _Node(_Bridge([_deal(entry=0, price=ENTRY, ts=1_757_000_000.0)]))

        await _closed(node)

        assert node._engine.record_close_calls == []

    async def test_a_failed_deal_lookup_is_not_a_close(self, alerts):
        """"We could not ask" and "it closed" are different facts."""
        node = _Node(_Bridge(raises=True))

        await _closed(node)

        assert node._engine.record_close_calls == []

    async def test_no_bridge_at_all_is_not_a_close(self, alerts):
        node = _Node(_Bridge())
        node._engine._bridge = None

        await _closed(node)

        assert node._engine.record_close_calls == []

    async def test_nothing_unverified_is_handed_to_profit_sync(self, alerts):
        """profit_sync exists to correct a recorded close. There is no close."""
        node = _Node(_Bridge([_deal(entry=0, price=ENTRY, ts=1_757_000_000.0)]))

        await _closed(node)

        assert node._engine.profit_syncs == []


class TestASiblingLegIsStillJustANote:
    """A leg that does not own the row's ticket never wrote trade state, and
    the guard must not change that -- it reports and returns."""

    async def test_a_sibling_leg_close_records_nothing_and_asks_no_broker(self, alerts):
        bridge = _Bridge()
        node = _Node(bridge, row=_row(mt5_ticket=1935400000))

        await node._on_trade_closed({
            "type": "trade_closed", "trade_id": f"{TRADE}-g2", "ticket": TICKET,
            "close_price": 4471.05, "reason": "TP",
        })
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert node._engine.record_close_calls == []
        assert bridge.history_calls == []
        assert [a["kind"] for a in alerts] == ["ea_close_sibling_leg"]
