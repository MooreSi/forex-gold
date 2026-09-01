"""The PARTIAL close has the shape the full close was fixed for.

`stage1 2/040` fixed `apply_full_close`: it ran `balance = balance + ?` with no
guard, so a duplicate call credited the P&L twice. `apply_partial_close_with_reason`
is the same shape and has not been fixed:

    INSERT INTO vantage_partial_closes (...)
    UPDATE vantage_simulated_trades SET realised_pnl = realised_pnl + ?, net_pnl = net_pnl + ?
    UPDATE vantage_simulation_account SET balance = balance + ?

Nothing makes it idempotent. Two calls for the same take-profit insert two
rows and credit the money twice.

**Why the upstream guard does not cover it.** `partial_close_trade` refuses a
trade whose status is not `open` — but a partial LEAVES the trade open, which
is the whole point. So the status check cannot see a repeat of the same TP. The
thing that does is `get_triggered_tps`, and that is:

  * an in-memory cache with a 2.5s TTL that is never invalidated after a
    partial (it works only because the handler mutates the cached set in
    place), and
  * read before an `await`, then acted on after it.

And there are at least two independent callers that can close the same TP on
the same trade: the strategy handlers (`handle_conservative`,
`handle_protected_scale`, `handle_scalp_runner`, ...) and the EA event path
(`ea_bridge/_events.py`), which fires on the EA's own TP report and never
touches that cache at all.

**Nothing here is fixed.** `partial_close_trade` is on the frozen close path,
and making the partial idempotent needs a design decision — what makes a
partial unique, and does it want a UNIQUE index and a migration — which is the
owner's call, not something to change while he is asleep. Raised as
docs/simon-handover/018.

These tests document what is true today. The two marked `xfail` assert the
behaviour we want: when the guard lands they will XPASS, and whoever fixes it
is told to remove the marker.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.trading import trade_repo


def _insert_open_trade(trade_id="t-1", signal_id="sig-1", lots=0.10):
    from backend.src.db.database import db
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,"
            "entry_low,entry_high,stop_loss,lot_size,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, "Test", "BUY", 4000.0, 4002.0, 3990.0, lots, "active",
             time.time()))
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,"
            "direction,entry_low,entry_high,entry_price,lot_size,remaining_lots,"
            "stop_loss,status,open_time,strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, 111, "BUY", 4000.0, 4002.0, 4000.0, lots, lots,
             3990.0, "open", time.time(), "conservative"))
        conn.execute(
            "INSERT OR REPLACE INTO vantage_simulation_account (id,balance,reset_at) "
            "VALUES (1,?,?)", (1000.0, time.time()))


def _balance():
    from backend.src.db.database import db
    with db() as conn:
        return float(conn.execute(
            "SELECT balance FROM vantage_simulation_account WHERE id=1"
        ).fetchone()[0])


def _row(trade_id="t-1"):
    from backend.src.db.database import db, row_to_dict
    with db() as conn:
        return row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,)).fetchone())


def _partials(trade_id="t-1"):
    from backend.src.db.database import db
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM vantage_partial_closes WHERE trade_id=?",
            (trade_id,)).fetchone()[0]


def _apply(reason="TP1", pnl=25.0, new_remaining=0.05, lots=0.05):
    trade_repo.apply_partial_close_with_reason(
        "t-1", now=1_000_000.0, lots_to_close=lots, close_price=4010.0,
        partial_pnl=pnl, new_remaining=new_remaining, entry_price=4000.0,
        row=_row(), reason=reason,
    )


class TestOnePartialWorks:
    """The control. Everything below is measured against this."""

    def test_it_credits_once(self, fresh_db):
        _insert_open_trade()

        _apply()

        assert _balance() == pytest.approx(1025.0)
        assert _partials() == 1
        assert _row()["remaining_lots"] == pytest.approx(0.05)
        assert _row()["realised_pnl"] == pytest.approx(25.0)

    def test_a_partial_leaves_the_trade_OPEN(self, fresh_db):
        """Which is exactly why `partial_close_trade`'s status check cannot
        catch a repeat of the same take-profit."""
        _insert_open_trade()

        _apply()

        assert _row()["status"] == "open"


class TestTheSameTakeProfitTwice:

    @pytest.mark.xfail(reason="known gap — see docs/simon-handover/018; "
                              "remove this marker when the guard lands",
                       strict=False)
    def test_it_should_not_credit_the_balance_twice(self, fresh_db):
        _insert_open_trade()

        _apply(reason="TP1")
        _apply(reason="TP1")

        assert _balance() == pytest.approx(1025.0), (
            "the same TP1 credited the account twice"
        )

    @pytest.mark.xfail(reason="known gap — see docs/simon-handover/018; "
                              "remove this marker when the guard lands",
                       strict=False)
    def test_it_should_not_record_two_partial_rows(self, fresh_db):
        _insert_open_trade()

        _apply(reason="TP1")
        _apply(reason="TP1")

        assert _partials() == 1

    def test_what_actually_happens_today(self, fresh_db):
        """Stated plainly rather than left implied, so the size of the gap is
        on the record: the money is credited twice and the realised P&L is
        double-counted."""
        _insert_open_trade()

        _apply(reason="TP1")
        _apply(reason="TP1")

        assert _balance() == pytest.approx(1050.0)
        assert _row()["realised_pnl"] == pytest.approx(50.0)
        assert _partials() == 2

    def test_the_lot_count_does_NOT_double_decrement(self, fresh_db):
        """The one thing that is safe: `remaining_lots` is an absolute
        assignment rather than a decrement, so a repeat sets the same value.
        Only the money is wrong -- which is why this is a bookkeeping and
        circuit-breaker problem rather than an exposure one."""
        _insert_open_trade()

        _apply(reason="TP1", new_remaining=0.05)
        _apply(reason="TP1", new_remaining=0.05)

        assert _row()["remaining_lots"] == pytest.approx(0.05)


class TestDifferentTakeProfitsAreNotDuplicates:
    """The control that any future guard must not break: TP1 and TP2 on the
    same trade are two legitimate partials, not a repeat."""

    def test_two_different_tps_both_apply(self, fresh_db):
        _insert_open_trade()

        _apply(reason="TP1", pnl=25.0, new_remaining=0.05, lots=0.05)
        _apply(reason="TP2", pnl=15.0, new_remaining=0.02, lots=0.03)

        assert _balance() == pytest.approx(1040.0)
        assert _partials() == 2
        assert _row()["remaining_lots"] == pytest.approx(0.02)
