"""Three statements that moved out of the trading services into repos.

All three showed as line-covered before the move and none of them was
actually asserted on: dropping the tg-signal UPDATE, swapping the Fixed R:R
stop and target, and no-oping the follow-up acknowledgement each left the
suite green. They are money-path, so they get real tests here.
"""
from __future__ import annotations

import pytest

from backend.src.db import database as db
from backend.src.services.signals import repo as signals_repo
from backend.src.services.signals import tg_repo
from backend.src.services.trading import trade_repo


def _tg_row(conn, tg_id="tg-1"):
    conn.execute(
        "INSERT INTO vantage_tg_signals (tg_message_id, group_id, raw_text, "
        "parsed_at, status) VALUES (?, 'g1', 'raw', 1700000000.0, 'pending')",
        (tg_id,))


def _one(sql, params=()):
    with db.db() as conn:
        row = conn.execute(sql, params).fetchone()
    return tuple(row) if row is not None else None


class TestInsertActivatedGridSignal:
    def test_writes_the_signal_and_marks_the_tg_row_activated(self, fresh_db):
        with fresh_db.db() as conn:
            _tg_row(conn)
        signals_repo.insert_activated_grid_signal(
            "sig-1", "tg-1", "Telegram Auto (X)", "BUY",
            4000.0, 4002.0, 3990.0, (4010.0, 4020.0), 0.05, "notes")

        sig = _one("SELECT status, direction, entry_low, entry_high, stop_loss, "
                   "lot_size, activated_at FROM vantage_signals WHERE signal_id=?",
                   ("sig-1",))
        assert sig[0] == "active"
        assert (sig[1], sig[2], sig[3], sig[4], sig[5]) == ("BUY", 4000.0, 4002.0, 3990.0, 0.05)
        assert sig[6] is not None, "a grid signal is activated on insert, not queued"

        assert _one("SELECT status, signal_id FROM vantage_tg_signals "
                    "WHERE tg_message_id='tg-1'") == ("activated", "sig-1")

    def test_short_tp_tuples_are_padded_not_shifted(self, fresh_db):
        """tp1..tp8 are positional in the INSERT. A two-target signal must
        leave tp3..tp8 NULL, not slide the lot_size into tp3."""
        with fresh_db.db() as conn:
            _tg_row(conn)
        signals_repo.insert_activated_grid_signal(
            "sig-1", "tg-1", "src", "BUY", 4000.0, 4002.0, 3990.0,
            (4010.0, 4020.0), 0.05, "notes")

        tps = _one("SELECT tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8 FROM vantage_signals "
                   "WHERE signal_id='sig-1'")
        assert tps == (4010.0, 4020.0, None, None, None, None, None, None)

    def test_a_full_eight_target_ladder_survives_intact(self, fresh_db):
        with fresh_db.db() as conn:
            _tg_row(conn)
        ladder = tuple(4010.0 + i for i in range(8))
        signals_repo.insert_activated_grid_signal(
            "sig-1", "tg-1", "src", "BUY", 4000.0, 4002.0, 3990.0,
            ladder, 0.05, "notes")
        assert _one("SELECT tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8 FROM vantage_signals "
                    "WHERE signal_id='sig-1'") == ladder

    def test_only_the_named_tg_row_is_activated(self, fresh_db):
        with fresh_db.db() as conn:
            _tg_row(conn, "tg-1")
            _tg_row(conn, "tg-2")
        signals_repo.insert_activated_grid_signal(
            "sig-1", "tg-1", "src", "BUY", 4000.0, 4002.0, 3990.0,
            (4010.0,), 0.05, "notes")
        assert _one("SELECT status FROM vantage_tg_signals "
                    "WHERE tg_message_id='tg-2'")[0] == "pending"

    def test_a_failed_insert_leaves_the_tg_row_alone(self, fresh_db):
        """The reason this is one transaction. An activated tg row with no
        signal row leaves the grid legs resting at the broker with nothing in
        the app pointing at them."""
        with fresh_db.db() as conn:
            _tg_row(conn, "tg-1")
            _tg_row(conn, "tg-2")
        signals_repo.insert_activated_grid_signal(
            "sig-1", "tg-1", "src", "BUY", 4000.0, 4002.0, 3990.0,
            (4010.0,), 0.05, "notes")
        # Same signal_id again -> primary key violation on the INSERT.
        with pytest.raises(Exception):
            signals_repo.insert_activated_grid_signal(
                "sig-1", "tg-2", "src", "BUY", 4000.0, 4002.0, 3990.0,
                (4010.0,), 0.05, "notes")
        assert _one("SELECT status FROM vantage_tg_signals "
                    "WHERE tg_message_id='tg-2'")[0] == "pending"


def _insert_trade(conn, trade_id="t1", **over):
    conn.execute(
        "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
        "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
        ("s-" + trade_id, "BUY", 4000.0, 4001.0, 3990.0, "active", 1700000000.0))
    row = {
        "trade_id": trade_id, "signal_id": "s-" + trade_id, "direction": "BUY",
        "entry_low": 4000.0, "entry_high": 4001.0, "entry_price": 4000.5,
        "lot_size": 0.01, "remaining_lots": 0.01, "stop_loss": 3990.0,
        "open_time": 1700000000.0, "status": "open",
        "tp1": 4010.0, "tp2": 4020.0, "tp3": 4030.0,
    }
    row.update(over)
    conn.execute(
        f"INSERT INTO vantage_simulated_trades ({', '.join(row)}) "
        f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


class TestApplyFixedRrLevels:
    def test_sets_the_stop_and_the_single_target(self, fresh_db):
        """Order matters and is not symmetric: a swapped pair puts the stop
        above a BUY's entry and the target below it."""
        with fresh_db.db() as conn:
            _insert_trade(conn)
        trade_repo.apply_fixed_rr_levels("t1", 3995.0, 4015.0)
        assert _one("SELECT stop_loss, tp1 FROM vantage_simulated_trades "
                    "WHERE trade_id='t1'") == (3995.0, 4015.0)

    def test_clears_the_rest_of_the_ladder(self, fresh_db):
        """Fixed R:R is unmanaged after open, so a leftover tp2 from the
        pre-fill proxy row is a target nothing is watching."""
        with fresh_db.db() as conn:
            _insert_trade(conn)
        trade_repo.apply_fixed_rr_levels("t1", 3995.0, 4015.0)
        assert _one("SELECT tp2,tp3,tp4,tp5,tp6,tp7,tp8 FROM vantage_simulated_trades "
                    "WHERE trade_id='t1'") == (None,) * 7

    def test_only_the_named_trade_is_touched(self, fresh_db):
        with fresh_db.db() as conn:
            _insert_trade(conn, "t1")
            _insert_trade(conn, "t2")
        trade_repo.apply_fixed_rr_levels("t1", 3995.0, 4015.0)
        assert _one("SELECT stop_loss, tp2 FROM vantage_simulated_trades "
                    "WHERE trade_id='t2'") == (3990.0, 4020.0)


class TestMarkFollowupApplied:
    def test_moves_the_row_out_of_pending_and_links_the_signal(self, fresh_db):
        """It must leave 'pending' or the IME watchdog waits on it forever."""
        with fresh_db.db() as conn:
            _tg_row(conn)
        tg_repo.mark_followup_applied("tg-1", "sig-9")
        assert _one("SELECT status, signal_id FROM vantage_tg_signals "
                    "WHERE tg_message_id='tg-1'") == ("followup_applied", "sig-9")

    def test_only_the_named_row(self, fresh_db):
        with fresh_db.db() as conn:
            _tg_row(conn, "tg-1")
            _tg_row(conn, "tg-2")
        tg_repo.mark_followup_applied("tg-1", "sig-9")
        assert _one("SELECT status FROM vantage_tg_signals "
                    "WHERE tg_message_id='tg-2'")[0] == "pending"
