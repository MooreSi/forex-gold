"""What makes two partial closes "the same" close.

Migration 30 made a take-profit level unique per trade:

    UNIQUE(trade_id, reason) WHERE reason GLOB 'TP[0-9]'

That was written for bugs/018 -- the Python handler and the EA can both report
one TP hit, and crediting it twice pays twice. The key looked right.

**It is wrong, and the owner's own account proved it on 2026-09-01** before the
migration had ever run there (his database was still at schema 29). Trade
9f1fd2ea closed like this:

    16:38:16  TP1  0.01 lots @ 4366.33  $3.04
    16:38:17  TP1  0.01 lots @ 4366.53  $3.24
    16:38:19  TP1  0.01 lots @ 4366.27  $2.98

Three genuine broker closes -- three different prices, 0.03 lots in total,
the whole position -- all labelled TP1. They sum to exactly the trade's
realised $9.26, which ProfitSync then confirmed against MT5's own $9.27.

Under `UNIQUE(trade_id, reason)` the second and third are silently dropped by
`INSERT OR IGNORE`. That is not a near miss:

  * realised P&L short by $6.22, and
  * worse, `remaining_lots` left at 0.02 for a position the broker has already
    closed -- a phantom holding, which is the failure this whole stage exists
    to prevent.

So the identity of a close is not its LABEL. It is what actually happened: how
much, and at what price. A duplicate REPORT of one close carries the same lots
and the same price. Three distinct closes do not.
"""
from __future__ import annotations

import sqlite3

import pytest


def _table(conn):
    conn.execute(
        "CREATE TABLE vantage_partial_closes ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT, ts REAL,"
        " lots_closed REAL, close_price REAL, pnl REAL, reason TEXT)")


def _index(conn):
    from backend.migrations import registry
    for num, _name, steps in registry.MIGRATIONS:
        if num != 32:
            continue
        for step in steps:
            if isinstance(step, str):
                conn.execute(step)
        return
    raise AssertionError("migration 32 not found")


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    _table(conn)
    _index(conn)
    yield conn
    conn.close()


def _insert(conn, reason, lots, price, pnl, trade="t-1"):
    cur = conn.execute(
        "INSERT OR IGNORE INTO vantage_partial_closes "
        "(trade_id,ts,lots_closed,close_price,pnl,reason) VALUES (?,?,?,?,?,?)",
        (trade, 0.0, lots, price, pnl, reason))
    return cur.rowcount


class TestTheRealSequenceIsKept:
    def test_all_three_of_the_owners_closes_survive(self, db):
        """The exact rows from 2026-09-01. Every one is a real broker close and
        every one must be recorded, or the trade's own accounting is wrong."""
        assert _insert(db, "TP1", 0.01, 4366.33, 3.04) == 1
        assert _insert(db, "TP1", 0.01, 4366.53, 3.24) == 1
        assert _insert(db, "TP1", 0.01, 4366.27, 2.98) == 1

        n, total = db.execute(
            "SELECT COUNT(*), ROUND(SUM(pnl),2) FROM vantage_partial_closes").fetchone()

        assert (n, total) == (3, 9.26)

    def test_the_old_key_would_have_dropped_two(self, db):
        """States the defect directly, so the reason this key changed cannot
        be lost. Under UNIQUE(trade_id, reason) only the first survives."""
        db.execute("DROP INDEX IF EXISTS idx_partial_close_one_per_tp")
        db.execute(
            "CREATE UNIQUE INDEX old_key ON vantage_partial_closes "
            "(trade_id, reason) WHERE reason GLOB 'TP[0-9]'")

        kept = sum((_insert(db, "TP1", 0.01, p, v)
                    for p, v in ((4366.33, 3.04), (4366.53, 3.24), (4366.27, 2.98))))

        assert kept == 1, "the old key kept more than one; the premise has changed"


class TestADuplicateReportIsStillRefused:
    def test_the_same_close_reported_twice_is_recorded_once(self, db):
        """bugs/018: the Python handler and the EA can both report one TP hit.
        Same lots, same price -- one close, one row."""
        assert _insert(db, "TP1", 0.015, 4366.33, 3.04) == 1
        assert _insert(db, "TP1", 0.015, 4366.33, 3.04) == 0

    def test_it_is_refused_even_with_a_different_pnl(self, db):
        """The two reporters compute P&L slightly differently -- one from the
        broker's figure, one estimated. Same lots and price is the same close,
        whatever each of them thinks it was worth."""
        assert _insert(db, "TP1", 0.015, 4366.33, 3.04) == 1
        assert _insert(db, "TP1", 0.015, 4366.33, 3.11) == 0

    def test_a_different_level_is_always_its_own_row(self, db):
        assert _insert(db, "TP1", 0.01, 4366.33, 3.04) == 1
        assert _insert(db, "TP2", 0.01, 4366.33, 3.04) == 1


class TestOtherTrades:
    def test_two_trades_do_not_collide(self, db):
        assert _insert(db, "TP1", 0.01, 4366.33, 3.04, trade="t-1") == 1
        assert _insert(db, "TP1", 0.01, 4366.33, 3.04, trade="t-2") == 1


class TestBrokerSourcedClosesAreUntouched:
    def test_mt5_close_can_still_repeat_freely(self, db):
        """The partial index covers only TP<n> markers. MT5_close legitimately
        repeats and was the reason the index was made partial in the first
        place -- that must not regress."""
        for _ in range(4):
            assert _insert(db, "MT5_close", 0.01, 4366.33, 3.04) == 1

        assert db.execute("SELECT COUNT(*) FROM vantage_partial_closes").fetchone()[0] == 4
