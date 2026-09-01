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

**Fixed 2026-09-01**, on the owner's answer to docs/simon-handover/018:
*"one partial per take-profit level per trade"*. Migration 30 adds a UNIQUE
index on (trade_id, reason), and the repo uses `INSERT OR IGNORE` and only
moves money when the insert actually happened.

The index is **partial**, and that is the point. Broker-sourced reasons
("MT5_close", "MT5_sync_TP", "MT5_SL") legitimately repeat — a position really
can be part-closed at the terminal more than once — so a blanket
UNIQUE(trade_id, reason) would have silently dropped the second one,
under-recording the money and leaving `remaining_lots` wrong. That would have
been a worse bug than the one being fixed. Only the strategy ladder's own
`TP<n>` markers are constrained, where a repeat is always a duplicate.
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

    def test_it_does_not_credit_the_balance_twice(self, fresh_db):
        _insert_open_trade()

        _apply(reason="TP1")
        _apply(reason="TP1")

        assert _balance() == pytest.approx(1025.0), (
            "the same TP1 credited the account twice"
        )

    def test_it_does_not_record_two_partial_rows(self, fresh_db):
        _insert_open_trade()

        _apply(reason="TP1")
        _apply(reason="TP1")

        assert _partials() == 1

    def test_the_realised_pnl_is_not_double_counted_either(self, fresh_db):
        """Not just the balance. The circuit breaker and the daily-loss halt
        both read realised P&L, so a doubled win could cancel a real loss."""
        _insert_open_trade()

        _apply(reason="TP1")
        _apply(reason="TP1")

        assert _row()["realised_pnl"] == pytest.approx(25.0)
        assert _row()["net_pnl"] == pytest.approx(25.0)

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


class TestBrokerSourcedPartialsMayStillRepeat:
    """The reason the index is partial rather than blanket.

    `position_sync` records broker-side partial closes as "MT5_close",
    "MT5_sync_TP" or "MT5_SL". A position really can be part-closed at the
    terminal more than once — someone closes half by hand, then half again —
    and both arrive under the same reason. Constraining those would silently
    drop the second, under-recording the money and leaving `remaining_lots`
    describing a position that is smaller than the database thinks.

    That would be a worse bug than the one the guard fixes, which is why the
    owner's "one per TP level" was implemented as exactly that and no wider.
    """

    @pytest.mark.parametrize("reason", ["MT5_close", "MT5_sync_TP", "MT5_SL"])
    def test_two_broker_partials_with_the_same_reason_both_apply(
            self, fresh_db, reason):
        _insert_open_trade(lots=0.20)

        _apply(reason=reason, pnl=10.0, new_remaining=0.15, lots=0.05)
        _apply(reason=reason, pnl=12.0, new_remaining=0.10, lots=0.05)

        assert _partials() == 2, f"a legitimate second {reason} was dropped"
        assert _balance() == pytest.approx(1022.0)
        assert _row()["remaining_lots"] == pytest.approx(0.10)

    def test_a_manual_target_close_is_not_constrained_either(self, fresh_db):
        _insert_open_trade(lots=0.20)

        _apply(reason="Target", pnl=10.0, new_remaining=0.15, lots=0.05)
        _apply(reason="Target", pnl=10.0, new_remaining=0.10, lots=0.05)

        assert _partials() == 2


class TestTheIndexCoversTheWholeLadder:
    """TP1 through TP10 — the ladder was widened from 8 to 10 in migration 18,
    so a two-digit marker has to be covered as well."""

    @pytest.mark.parametrize("tp", ["TP1", "TP6", "TP8", "TP9", "TP10"])
    def test_each_level_is_guarded(self, fresh_db, tp):
        _insert_open_trade()

        _apply(reason=tp)
        _apply(reason=tp)

        assert _partials() == 1, f"{tp} was not covered by the index"
        assert _balance() == pytest.approx(1025.0)

    def test_the_marker_match_is_case_sensitive(self, fresh_db):
        """GLOB rather than LIKE, so a lowercase "tp1" is not silently treated
        as the same marker as "TP1"."""
        _insert_open_trade()

        _apply(reason="TP1")
        _apply(reason="tp1")

        assert _partials() == 2


class TestTheMigrationSurvivesAnInstallWhereItAlreadyHappened:
    """The index cannot simply be created on an existing database.

    If the bug has already fired, there are duplicate rows on disk, and
    `CREATE UNIQUE INDEX` on those raises `IntegrityError` — which the
    migration runner treats as fatal, so **the app would refuse to start**.
    Found by running migration 30 against a legacy database rather than a fresh
    one; a fresh-schema test would never have shown it.

    The duplicates are renamed rather than deleted. Deleting would make this
    table disagree with an account balance that was already credited twice,
    quietly rewriting history to look consistent. Renaming keeps every row,
    frees the index, and leaves the evidence visible.
    """

    def _legacy_db(self, tmp_path, rows):
        import sqlite3
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE vantage_partial_closes (id INTEGER PRIMARY KEY, "
            "trade_id TEXT, ts REAL, lots_closed REAL, close_price REAL, "
            "pnl REAL, reason TEXT)")
        conn.executemany(
            "INSERT INTO vantage_partial_closes "
            "(trade_id,ts,lots_closed,close_price,pnl,reason) VALUES (?,?,?,?,?,?)",
            rows)
        conn.commit()
        return conn

    def _run_migration_30(self, conn):
        from backend.migrations.registry import MIGRATIONS
        for step in next(m for m in MIGRATIONS if m[0] == 30)[2]:
            conn.execute(step)
        conn.commit()

    def test_it_completes_on_a_database_that_already_has_duplicates(
            self, tmp_path):
        conn = self._legacy_db(tmp_path, [
            ("t1", 1, 0.05, 4010, 25, "TP1"),
            ("t1", 2, 0.05, 4010, 25, "TP1"),
            ("t1", 3, 0.05, 4010, 25, "TP1"),
        ])

        self._run_migration_30(conn)      # must not raise

        assert conn.execute(
            "SELECT COUNT(*) FROM vantage_partial_closes").fetchone()[0] == 3

    def test_no_row_is_deleted_and_each_marked_one_is_distinct(self, tmp_path):
        conn = self._legacy_db(tmp_path, [
            ("t1", 1, 0.05, 4010, 25, "TP1"),
            ("t1", 2, 0.05, 4010, 25, "TP1"),
            ("t1", 3, 0.05, 4010, 25, "TP1"),
        ])

        self._run_migration_30(conn)

        reasons = [r[0] for r in conn.execute(
            "SELECT reason FROM vantage_partial_closes ORDER BY id")]
        assert reasons == ["TP1", "TP1_dup2", "TP1_dup3"], (
            "a count-based suffix labelled two rows the same, because an "
            "UPDATE rewriting the column it groups on sees partly-updated data"
        )

    def test_the_earliest_row_keeps_its_original_reason(self, tmp_path):
        """The first partial is the real one; the repeats are the artefacts."""
        conn = self._legacy_db(tmp_path, [
            ("t1", 1, 0.05, 4010, 25, "TP1"),
            ("t1", 2, 0.05, 4010, 25, "TP1"),
        ])

        self._run_migration_30(conn)

        assert conn.execute(
            "SELECT reason FROM vantage_partial_closes WHERE id=1").fetchone()[0] == "TP1"

    def test_it_leaves_non_ladder_reasons_alone(self, tmp_path):
        conn = self._legacy_db(tmp_path, [
            ("t1", 1, 0.05, 4010, 9, "MT5_close"),
            ("t1", 2, 0.05, 4010, 9, "MT5_close"),
        ])

        self._run_migration_30(conn)

        reasons = [r[0] for r in conn.execute(
            "SELECT reason FROM vantage_partial_closes ORDER BY id")]
        assert reasons == ["MT5_close", "MT5_close"]

    def test_a_different_trades_same_TP_is_not_a_duplicate(self, tmp_path):
        conn = self._legacy_db(tmp_path, [
            ("t1", 1, 0.05, 4010, 25, "TP1"),
            ("t2", 2, 0.05, 4010, 25, "TP1"),
        ])

        self._run_migration_30(conn)

        reasons = [r[0] for r in conn.execute(
            "SELECT reason FROM vantage_partial_closes ORDER BY id")]
        assert reasons == ["TP1", "TP1"]

    def test_the_index_is_live_afterwards(self, tmp_path):
        """Control: the clean-up must not leave the constraint unenforced."""
        import sqlite3
        conn = self._legacy_db(tmp_path, [("t1", 1, 0.05, 4010, 25, "TP1"),
                                          ("t1", 2, 0.05, 4010, 25, "TP1")])

        self._run_migration_30(conn)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO vantage_partial_closes "
                "(trade_id,ts,lots_closed,close_price,pnl,reason) "
                "VALUES ('t1',9,0.05,4010,25,'TP1')")
