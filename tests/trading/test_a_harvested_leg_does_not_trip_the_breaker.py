"""The close path, end to end, with a basket registered.

`docs/todo/bugs/041`. `tests/risk/test_basket_breaker.py` pins the decision;
this pins the **wiring** — that `record_close` actually asks
`basket_breaker.score_close` rather than `record_live_trade_outcome`, on the
frozen close path, with a real database underneath.

Without this, the decision could be perfect and the close path could carry on
counting every leg, which is the state the app was in until today.

The live incident these numbers come from, 2026-09-10:

    10:46:04  harvest closes five: +42.49 +24.20 +19.20 +22.10 -0.80  (net +$107.19)
    11:33:24  SL -$49.80
    11:35:33  SL -$52.50
    11:35:33  [CB] Circuit breaker triggered — live trading blocked for 15 min.

Threshold 3 and only two real losses after the basket, so the **eighty-cent**
leg had been counted as the first.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.services.risk import basket_breaker
from backend.src.services.trading import close_trade as ct


class _Bridge:
    """No broker. record_close is given the exit price directly."""

    async def get_account(self):
        return {"balance": 1000.0, "equity": 1000.0, "margin_free": 900.0}

    async def get_position_history(self, ticket):
        return []

    async def get_deal_history(self, days):
        return []


@pytest.fixture
def ctx(fresh_db):
    return ct.CloseTradeContext(_Bridge(), starting_balance=1000.0)


@pytest.fixture(autouse=True)
def cb_armed(fresh_db):
    db.update_risk_settings({
        "circuit_breaker_enabled": 1,
        "circuit_breaker_losses": 3,
        "circuit_breaker_cooldown_mins": 60,
        "circuit_breaker_consec_losses": 0,
        "circuit_breaker_active_until": 0.0,
    })
    basket_breaker.forget_all()
    yield
    basket_breaker.forget_all()


def _insert(trade_id, ticket, direction="BUY", entry=2400.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("s-" + trade_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()))
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, "
            "direction, entry_low, entry_high, entry_price, lot_size, remaining_lots, "
            "stop_loss, status, open_time, net_pnl, realised_pnl, strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "s-" + trade_id, ticket, direction, 2399.0, 2401.0, entry,
             0.10, 0.10, 2390.0, "open", time.time(), 0.0, 0.0, "scale_out"))


def _consec() -> int:
    return int(db.get_risk_settings().get("circuit_breaker_consec_losses", 0) or 0)


def _close(ctx, trade_id, price):
    return asyncio.run(ct.record_close(trade_id, price, "MT5_close", ctx))


class TestTheWiring:
    def test_an_ordinary_loss_still_counts(self, ctx):
        """The negative control. Without it every test below would pass against
        a close path that had stopped feeding the breaker anything at all —
        the one failure worse than counting a harvested leg."""
        _insert("t-1", 111)

        _close(ctx, "t-1", 2380.0)

        assert _consec() == 1

    def test_a_leg_of_a_winning_basket_does_not_count(self, ctx):
        """The eighty-cent leg. It closes at a loss, and after it the counter
        is 0 rather than 1."""
        db.update_risk_settings({"circuit_breaker_consec_losses": 0})
        _insert("t-1", 111)
        basket_breaker.register("b1", net_pnl=107.19, tickets=[111, 222, 333])

        _close(ctx, "t-1", 2380.0)

        assert _consec() == 0

    def test_a_leg_of_a_losing_basket_leaves_the_counter_alone(self, ctx):
        db.update_risk_settings({"circuit_breaker_consec_losses": 1})
        _insert("t-1", 111)
        basket_breaker.register("b1", net_pnl=-20.0, tickets=[111, 222])

        _close(ctx, "t-1", 2380.0)

        assert _consec() == 1

    def test_the_2026_09_10_halt_does_not_happen(self, ctx):
        """The incident, replayed: a winning basket's losing leg, then the two
        real losses that followed it. Threshold 3, so the breaker must NOT be
        active at the end — it was."""
        _insert("harvested", 111)
        _insert("loss-1", 222)
        _insert("loss-2", 333)
        basket_breaker.register("b1", net_pnl=107.19, tickets=[111, 444, 555])

        _close(ctx, "harvested", 2380.0)
        _close(ctx, "loss-1", 2380.0)
        _close(ctx, "loss-2", 2380.0)

        assert _consec() == 2
        assert db.get_circuit_breaker_state()["is_active"] is False, (
            "the breaker tripped on two real losses plus a leg of a profitable "
            "basket — which is bugs/041, live on 2026-09-10")

    def test_three_real_losses_still_trip_it(self, ctx):
        """The other half: the basket rule must not defuse the breaker itself.
        Same three closes, no basket registered."""
        _insert("loss-1", 111)
        _insert("loss-2", 222)
        _insert("loss-3", 333)

        _close(ctx, "loss-1", 2380.0)
        _close(ctx, "loss-2", 2380.0)
        _close(ctx, "loss-3", 2380.0)

        assert db.get_circuit_breaker_state()["is_active"] is True
