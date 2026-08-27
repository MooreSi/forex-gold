"""The two SQL statements ea_bridge ran inline, now in the broker repo.

Neither had a test. Both are money-path: one decides which trades the EA is
handed back when it reconnects, the other is the counter that decides when a
grid's placeholder gets closed as dead. They are pinned here before the move so
the move has something to fail against.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker import repo as broker_repo


def _insert_trade(conn, trade_id, **over):
    """The NOT NULL columns of vantage_simulated_trades plus the ones these
    two statements actually read. The values are inert: nothing here asserts
    on price or size, only on which rows come back and what the counter says."""
    row = {
        "trade_id": trade_id, "signal_id": "s-" + trade_id,
        "direction": "BUY", "entry_low": 4000.0, "entry_high": 4001.0,
        "entry_price": 4000.5, "lot_size": 0.01, "remaining_lots": 0.01,
        "stop_loss": 3990.0, "open_time": 1700000000.0,
        "status": "open", "managed_by": "ea", "mt5_ticket": 555,
        "grid_legs_cancelled": 0,
    }
    row.update(over)
    # signal_id is a foreign key -- the parent has to exist first.
    conn.execute(
        "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
        "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (row["signal_id"], "BUY", 4000.0, 4001.0, 3990.0, "activated", 1700000000.0))
    cols = ", ".join(row)
    conn.execute(
        f"INSERT INTO vantage_simulated_trades ({cols}) "
        f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


class TestFetchOpenEaManagedTrades:
    """What the EA gets handed on "hello". Each part of the WHERE clause is a
    separate reason a trade must NOT be restored, so each gets its own case --
    a single "returns the good one" test passes even if the clause is `1=1`."""

    def test_returns_an_open_ea_managed_trade_with_a_ticket(self, fresh_db):
        with fresh_db.db() as conn:
            _insert_trade(conn, "good")
        assert [r["trade_id"] for r in broker_repo.fetch_open_ea_managed_trades()] == ["good"]

    @pytest.mark.parametrize("why, over", [
        # A closed trade restored to the EA would put management back on a
        # position that is already gone.
        ("closed",            {"status": "closed"}),
        # Not ours to manage -- the EA must not be told to trail it.
        ("not ea managed",    {"managed_by": "python"}),
        # No broker position exists yet. Restoring a ticketless row is how a
        # placeholder becomes a ghost the EA reports on forever.
        ("no ticket",         {"mt5_ticket": None}),
        ("zero ticket",       {"mt5_ticket": 0}),
    ])
    def test_excludes(self, fresh_db, why, over):
        with fresh_db.db() as conn:
            _insert_trade(conn, "bad", **over)
        assert broker_repo.fetch_open_ea_managed_trades() == [], why


class TestIncrGridLegCancelled:
    """The counter that decides a grid never filled. It must return the value
    AFTER its own increment -- the caller compares the return against the leg
    total to decide whether to close the placeholder, so an off-by-one either
    closes a grid that still has a resting leg or never closes a dead one."""

    def test_returns_the_post_increment_count(self, fresh_db):
        with fresh_db.db() as conn:
            _insert_trade(conn, "t1")
        assert broker_repo.incr_grid_leg_cancelled("t1") == 1

    def test_accumulates_across_calls(self, fresh_db):
        with fresh_db.db() as conn:
            _insert_trade(conn, "t1")
        seen = [broker_repo.incr_grid_leg_cancelled("t1") for _ in range(3)]
        assert seen == [1, 2, 3]

    def test_only_bumps_the_named_trade(self, fresh_db):
        with fresh_db.db() as conn:
            _insert_trade(conn, "t1")
            _insert_trade(conn, "t2")
        broker_repo.incr_grid_leg_cancelled("t1")
        assert broker_repo.incr_grid_leg_cancelled("t2") == 1

    def test_an_unknown_trade_returns_zero_rather_than_raising(self, fresh_db):
        """Called from a cancel handler. Raising there would abort the rest of
        the cancel path over a row that is already gone."""
        assert broker_repo.incr_grid_leg_cancelled("nope") == 0
