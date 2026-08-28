"""The circuit breaker that record_close arms after a losing live trade.

Three consecutive live losses block new execution for a cooldown. It is one of
the protective halts the readiness checklist still lists as Simon-gated, and it
sat entirely uncovered -- the whole block was in close_trade.py's uncovered
lines.

record_close is FROZEN (CLAUDE.md rule 4). Nothing here changes it; these tests
exist precisely because it cannot be reshaped, so the only way to notice
someone breaking it is to have pinned what it does.

Two properties matter most and neither is obvious from reading the call:

  * only LIVE trades count. The gate is `row.get("mt5_ticket")`, so paper
    trades must not arm a breaker that blocks real execution.
  * the outcome is the trade's TOTAL P&L -- the row's already-realised
    net_pnl plus this close -- not just the amount closed here. A trade that
    banked +50 at TP1 and gives back -10 at the end is a WIN.

No real or demo order is placed; the bridge is a fake and its call log is
asserted on.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.services.trading import close_trade as ct


class _Bridge:
    """Minimal broker stand-in. close_position is recorded, never real."""

    def __init__(self):
        self.close_position_calls = []

    async def get_tick(self):
        return SimpleNamespace(bid=2410.0, ask=2410.2)

    async def close_position(self, ticket):
        self.close_position_calls.append(ticket)
        return {"success": True, "close_price": 2410.0}

    async def get_account(self):
        return {"balance": 1000.0}


@pytest.fixture
def ctx(fresh_db):
    return ct.CloseTradeContext(_Bridge(), starting_balance=1000.0)


@pytest.fixture
def cb_armed(fresh_db):
    """Circuit breaker on, threshold 3, nothing counted yet."""
    db.update_risk_settings({
        "circuit_breaker_enabled": 1,
        "circuit_breaker_losses": 3,
        "circuit_breaker_cooldown_mins": 60,
        "circuit_breaker_consec_losses": 0,
    })


def _insert(trade_id, *, mt5_ticket, net_pnl=0.0, direction="BUY", entry=2400.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("s-" + trade_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()))
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, "
            "direction, entry_low, entry_high, entry_price, lot_size, remaining_lots, "
            "stop_loss, status, open_time, net_pnl, realised_pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "s-" + trade_id, mt5_ticket, direction, 2399.0, 2401.0, entry,
             0.10, 0.10, 2390.0, "open", time.time(), net_pnl, 0.0))


def _consec() -> int:
    return int(db.get_risk_settings().get("circuit_breaker_consec_losses", 0) or 0)


def _close(trade_id, price, ctx):
    return asyncio.run(ct.record_close(trade_id, price, "manual_close", ctx))


class TestOnlyLiveTradesCount:
    def test_a_losing_live_trade_increments_the_counter(self, cb_armed, ctx):
        _insert("t-live", mt5_ticket=555)
        _close("t-live", 2380.0, ctx)          # BUY closed below entry -> loss
        assert _consec() == 1

    def test_a_losing_PAPER_trade_does_not(self, cb_armed, ctx):
        """The gate is row["mt5_ticket"]. A paper trade arming a breaker that
        blocks REAL execution would halt live trading over a simulation."""
        _insert("t-paper", mt5_ticket=None)
        _close("t-paper", 2380.0, ctx)
        assert _consec() == 0


class TestTheOutcomeIsTheWholeTrade:
    def test_a_win_resets_the_counter(self, cb_armed, ctx):
        db.update_risk_settings({"circuit_breaker_consec_losses": 2})
        _insert("t-win", mt5_ticket=555)
        _close("t-win", 2420.0, ctx)          # BUY closed above entry -> win
        assert _consec() == 0

    def test_previously_banked_profit_counts_toward_the_verdict(self, cb_armed, ctx):
        """net_pnl on the row is what earlier partial closes already realised.
        A trade that banked +50 and gives back a little at the end is a win,
        and judging only the final leg would count it as a loss and march the
        breaker toward tripping on profitable trades."""
        db.update_risk_settings({"circuit_breaker_consec_losses": 2})
        _insert("t-mixed", mt5_ticket=555, net_pnl=50.0)
        _close("t-mixed", 2399.0, ctx)        # small give-back on the last leg

        # Assert the breaker is NOT engaged, not merely that the counter is 0.
        # Tripping RESETS consec_losses, so `_consec() == 0` is true both when
        # this was judged a win and when it was judged the third loss and
        # tripped -- a test on the counter alone passes either way, which is
        # exactly how the "judge only this leg" mutant survived first time.
        assert db.get_circuit_breaker_state()["is_active"] is False, (
            "total P&L is positive, so this is a win and must not trip")
        assert _consec() == 0


    def test_a_scratch_trade_counts_as_a_win(self, cb_armed, ctx):
        """The comparison is `total_pnl >= 0`, not `> 0`. A trade that closes
        exactly flat has not lost, and marching the breaker toward a halt on a
        scratch would block live trading over nothing."""
        db.update_risk_settings({"circuit_breaker_consec_losses": 2})
        _insert("t-flat", mt5_ticket=555, net_pnl=0.0)
        _close("t-flat", 2400.0, ctx)         # closed exactly at entry

        assert db.get_circuit_breaker_state()["is_active"] is False, (
            "a flat trade must not trip the breaker")


class TestTripping:
    def test_the_third_consecutive_live_loss_trips_it(self, cb_armed, ctx):
        db.update_risk_settings({"circuit_breaker_consec_losses": 2})
        _insert("t-3rd", mt5_ticket=555)
        _close("t-3rd", 2380.0, ctx)
        state = db.get_circuit_breaker_state()
        assert state["is_active"] is True, (
            f"threshold reached but the breaker is not engaged: {state}")
        assert state["remaining_secs"] > 0

    def test_it_does_not_trip_early(self, cb_armed, ctx):
        _insert("t-1st", mt5_ticket=555)
        _close("t-1st", 2380.0, ctx)
        state = db.get_circuit_breaker_state()
        assert state["is_active"] is False, "one loss must not block live trading"


class TestItNeverBreaksTheClose:
    def test_a_failing_circuit_breaker_does_not_abort_the_close(self, cb_armed, ctx, monkeypatch):
        """The block is wrapped in try/except for a reason: the trade is
        already closed at the broker by this point. Losing the DB row because
        the breaker errored would leave the app blind to a real position."""
        def _boom(*a, **k):
            raise RuntimeError("circuit breaker table missing")
        monkeypatch.setattr(db, "record_live_trade_outcome", _boom)

        _insert("t-cb-err", mt5_ticket=555)
        result = _close("t-cb-err", 2380.0, ctx)

        assert result is not None
        with db.db() as conn:
            status = conn.execute(
                "SELECT status FROM vantage_simulated_trades WHERE trade_id=?",
                ("t-cb-err",)).fetchone()[0]
        assert status == "closed", "the close must land even if the breaker fails"

    def test_no_broker_order_is_placed_by_any_of_this(self, cb_armed, ctx):
        _insert("t-safe", mt5_ticket=None)
        _close("t-safe", 2380.0, ctx)
        assert ctx.bridge.close_position_calls == []
