"""The "Create Limit Order" form's backend.

`manual_limit_order.py` was 31.4% covered -- 35 of its 51 statements never
executed -- in `services/trading`, one of the three areas the 2026-08-25 merge
pushed below its coverage floor.

It exists because of a real bug, and the fix is exactly the thing worth
pinning. The form used to route through the automatic zone-signal path, which
waits for price to re-enter the zone and then fills at MARKET -- so "Save &
Open Trade" only worked when price already happened to sit inside Entry
Low-High, and rejected otherwise. That is backwards for a resting limit order,
whose whole point is to be placed when price is NOT there yet.

**Nothing here places a real order.** The EA singleton is replaced with a
recorder, so `place_pending_order` is a coroutine belonging to this file. The
Telegram alert is stubbed too -- the real one sends a message, and a test suite
must not.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_bridge as ea_bridge_mod
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.trading import manual_limit_order as mlo


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """The module fires an alert on success via asyncio.create_task."""
    sent = []

    async def _send(text, trade_id=None, kind=None):
        sent.append(text)

    monkeypatch.setattr(telegram_alerts, "send_message", _send)
    return sent


def _fake_ea(healthy=True, ack=None, recorder=None):
    async def place_pending_order(trade_id, direction, price, lot, sl, tps, pcts,
                                  be_at_pos, **kw):
        if recorder is not None:
            recorder.append(dict(trade_id=trade_id, direction=direction, price=price,
                                 lot=lot, sl=sl, tps=tps, pcts=pcts,
                                 be_at_pos=be_at_pos, **kw))
        return ack if ack is not None else {"type": "pending_order_placed", "ticket": 987}

    return types.SimpleNamespace(
        is_ea_healthy=lambda: healthy,
        place_pending_order=place_pending_order,
    )


@pytest.fixture
def ea(monkeypatch):
    """Install a recording EA and hand back the list of what it was asked to do."""
    calls = []

    def _install(healthy=True, ack=None):
        monkeypatch.setattr(ea_bridge_mod, "get_instance",
                            lambda: _fake_ea(healthy, ack, calls))
        return calls

    return _install


def _place(**over):
    kw = dict(direction="BUY", entry_low=2390.0, entry_high=2400.0,
              stop_loss=2380.0, tp1=2420.0)
    kw.update(over)
    return asyncio.run(mlo.open_manual_limit_order(None, **kw))


# ── Refusals ──────────────────────────────────────────────────────────────────

def test_a_nonsense_direction_is_rejected(fresh_db, ea):
    ea()
    with pytest.raises(ValueError, match="Invalid direction"):
        _place(direction="SIDEWAYS")


def test_no_healthy_ea_means_no_order_and_no_silent_fallback(fresh_db, ea):
    """There is deliberately no Python-bridge fallback: that path fills at
    market, which is the bug this module was written to fix. Failing loudly is
    the correct behaviour."""
    calls = ea(healthy=False)
    with pytest.raises(ConnectionError, match="healthy EA bridge"):
        _place()
    assert calls == [], "nothing may be placed without a healthy EA"


def test_no_ea_at_all_is_the_same_refusal(fresh_db, monkeypatch):
    monkeypatch.setattr(ea_bridge_mod, "get_instance", lambda: None)
    with pytest.raises(ConnectionError):
        _place()


def test_at_least_one_take_profit_is_required(fresh_db, ea):
    calls = ea()
    with pytest.raises(ValueError, match="TP1"):
        _place(tp1=None)
    assert calls == []


def test_an_ea_rejection_is_raised_not_swallowed(fresh_db, ea):
    """A rejected pending order must not leave the user thinking it rested."""
    ea(ack={"type": "error", "error": "invalid stops"})
    with pytest.raises(RuntimeError, match="invalid stops"):
        _place()


# ── The resting-limit semantics ───────────────────────────────────────────────

def test_a_buy_limit_rests_at_the_top_of_the_zone(fresh_db, ea):
    """BUY takes entry_high, SELL takes entry_low. This is the whole fix: the
    order is placed where price is not, and waits."""
    calls = ea()
    out = _place(direction="BUY", entry_low=2390.0, entry_high=2400.0)

    assert calls[0]["price"] == 2400.0
    assert calls[0]["direction"] == "BUY"
    assert out["price"] == 2400.0


def test_a_sell_limit_rests_at_the_bottom_of_the_zone(fresh_db, ea):
    calls = ea()
    out = _place(direction="SELL", entry_low=2390.0, entry_high=2400.0,
                 stop_loss=2410.0, tp1=2370.0)

    assert calls[0]["price"] == 2390.0
    assert out["price"] == 2390.0


def test_a_lowercase_direction_is_accepted(fresh_db, ea):
    calls = ea()
    _place(direction="buy")
    assert calls[0]["direction"] == "BUY"


# ── Sizing ────────────────────────────────────────────────────────────────────
#
# MONEY PATH. These assert the existing behaviour rather than proposing any --
# an explicit fixed lot always wins and is never capped, matching every other
# sizing layer.

def test_an_explicit_lot_size_wins(fresh_db, ea):
    calls = ea()
    db.update_risk_settings({"strategy_lot_size": 0.5})
    _place(lot_size=0.07)
    assert calls[0]["lot"] == 0.07


def test_the_strategy_lot_is_used_when_no_explicit_lot_is_given(fresh_db, ea):
    calls = ea()
    db.update_risk_settings({"strategy_lot_size": 0.25})
    _place()
    assert calls[0]["lot"] == 0.25


def test_the_lot_never_goes_below_the_broker_minimum(fresh_db, ea):
    calls = ea()
    _place(lot_size=0.0001)
    assert calls[0]["lot"] == 0.01


def test_the_take_profits_are_numbered_from_one(fresh_db, ea):
    """The EA is handed {1: price, 2: price, ...}; an off-by-one here would
    attach TP levels to the wrong rungs."""
    calls = ea()
    _place(tp1=2420.0, tp2=2430.0, tp3=2440.0)
    assert calls[0]["tps"] == {1: 2420.0, 2: 2430.0, 3: 2440.0}


def test_gaps_in_the_take_profit_ladder_are_dropped_not_renumbered(fresh_db, ea):
    """TP1 and TP3 with no TP2 keeps its original numbering."""
    calls = ea()
    _place(tp1=2420.0, tp3=2440.0)
    assert calls[0]["tps"] == {1: 2420.0, 3: 2440.0}


# ── What it records ───────────────────────────────────────────────────────────

def test_a_placed_order_is_written_as_a_working_pending_row(fresh_db, ea):
    ea()
    out = _place()

    assert out["mt5_ticket"] == 987
    with fresh_db.db() as conn:
        row = conn.execute(
            "SELECT status, ea_ticket, strategy, price, lot_size "
            "FROM vantage_pending_orders WHERE trade_id=?",
            (out["trade_id"],)).fetchone()
    assert row is not None, "the pending order was placed but never recorded"
    assert row["status"] == "working"
    assert row["ea_ticket"] == 987
    assert row["price"] == 2400.0, "the recorded rest price must match what the EA was given"


def test_the_user_is_told_the_order_was_placed(fresh_db, ea, no_telegram):
    ea()
    out = _place()
    asyncio.run(asyncio.sleep(0))      # let the fire-and-forget alert task run

    assert no_telegram, "a placed limit order must announce itself"
    assert "Manual Limit Order Placed" in no_telegram[0]
    assert str(out["mt5_ticket"]) in no_telegram[0]
