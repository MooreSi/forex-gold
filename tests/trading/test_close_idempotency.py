"""Recording the same close twice must not pay out twice (stage1 2/040).

`apply_full_close` had no status guard:

    UPDATE vantage_simulated_trades SET status='closed', ... WHERE trade_id=?
    UPDATE vantage_simulation_account SET balance = balance + ?
    UPDATE vantage_signals SET status='closed' WHERE signal_id=?

Nothing stopped it running a second time, and the middle statement is
`balance = balance + ?`. So a duplicate call **credits the P&L again**. The
books drift, and the circuit breaker is fed the same outcome twice -- a losing
streak counted double, or a win that cancels a real loss.

Five callers can race into this: the monitor loop, reconciliation, a manual
close, partial-close completion, and the sync channel. Most of them became
more likely, not less, with stage3: 030's reconciler exists precisely to close
trades the app missed, and 040 leaves rows open for it to settle.

The rule: only an `open` trade may transition to `closed`. A second caller
finds it already closed and changes nothing -- no second credit, no second
breaker outcome.
"""
from __future__ import annotations

import pytest

from backend.src.services.trading import trade_repo


def _insert_open_trade(trade_id="t-1", signal_id="sig-1"):
    from backend.src.db.database import db
    import time as _t
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,"
            "entry_low,entry_high,stop_loss,lot_size,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, "Test", "BUY", 4000.0, 4002.0, 3990.0, 0.1, "active", _t.time()))
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,"
            "direction,entry_low,entry_high,entry_price,lot_size,remaining_lots,"
            "stop_loss,status,open_time,strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, 111, "BUY", 4000.0, 4002.0, 4000.0, 0.1, 0.1,
             3990.0, "open", _t.time(), "scalp"))
        conn.execute(
            "INSERT OR REPLACE INTO vantage_simulation_account (id,balance,reset_at) "
            "VALUES (1,?,?)", (1000.0, _t.time()))


def _balance():
    from backend.src.db.database import db
    with db() as conn:
        return float(conn.execute(
            "SELECT balance FROM vantage_simulation_account WHERE id=1").fetchone()[0])


def _row(trade_id="t-1"):
    from backend.src.db.database import db, row_to_dict
    with db() as conn:
        return row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,)).fetchone())


def _close(trade_id="t-1", net_delta=25.0, price=4010.0, reason="tp1"):
    trade_repo.apply_full_close(
        trade_id, now=1_000_000.0, close_price=price, reason=reason,
        gross_pnl=net_delta, realised_total=net_delta,
        net_pnl_total=net_delta, net_delta=net_delta, signal_id="sig-1")


class TestTheFirstCloseStillWorks:
    """The negative control. Without it the tests below would pass against a
    guard that blocks every close."""

    def test_it_records_the_close(self, fresh_db):
        _insert_open_trade()

        _close()

        row = _row()
        assert row["status"] == "closed"
        assert row["close_price"] == 4010.0
        assert row["exit_reason"] == "tp1"

    def test_it_credits_the_account_once(self, fresh_db):
        _insert_open_trade()

        _close(net_delta=25.0)

        assert _balance() == pytest.approx(1025.0)

    def test_it_closes_the_signal(self, fresh_db):
        from backend.src.db.database import db
        _insert_open_trade()

        _close()

        with db() as conn:
            assert conn.execute(
                "SELECT status FROM vantage_signals WHERE signal_id='sig-1'"
            ).fetchone()[0] == "closed"

    def test_a_LOSS_debits_the_account(self, fresh_db):
        _insert_open_trade()

        _close(net_delta=-30.0)

        assert _balance() == pytest.approx(970.0)


class TestASecondCloseChangesNothing:
    def test_THE_BALANCE_IS_NOT_CREDITED_TWICE(self, fresh_db):
        """The money consequence. `balance = balance + ?` run twice pays the
        same profit out twice, and the books never recover on their own."""
        _insert_open_trade()

        _close(net_delta=25.0)
        _close(net_delta=25.0)

        assert _balance() == pytest.approx(1025.0), "the close paid out twice"

    def test_a_second_LOSS_is_not_debited_twice(self, fresh_db):
        """Same in the other direction, and worse for the breaker: a doubled
        loss is a doubled losing streak."""
        _insert_open_trade()

        _close(net_delta=-30.0)
        _close(net_delta=-30.0)

        assert _balance() == pytest.approx(970.0)

    def test_the_original_close_details_are_not_overwritten(self, fresh_db):
        """A later caller with different numbers must not rewrite history.
        Reconciliation, for instance, may arrive with a price from the deal
        history well after the monitor loop recorded the real one."""
        _insert_open_trade()

        _close(price=4010.0, reason="tp1")
        _close(price=3990.0, reason="sl")

        row = _row()
        assert row["close_price"] == 4010.0
        assert row["exit_reason"] == "tp1"

    def test_three_racing_callers_credit_once(self, fresh_db):
        """Five callers can reach this: the monitor loop, reconciliation, a
        manual close, partial-close completion, and sync."""
        _insert_open_trade()

        for _ in range(3):
            _close(net_delta=25.0)

        assert _balance() == pytest.approx(1025.0)


class TestOnlyAnOpenTradeMayClose:
    def test_a_cancelled_trade_is_not_resurrected_as_closed(self, fresh_db):
        """Any status other than open is a finished trade. A late close
        arriving for one must not rewrite it."""
        from backend.src.db.database import db
        _insert_open_trade()
        with db() as conn:
            conn.execute("UPDATE vantage_simulated_trades SET status='cancelled' "
                         "WHERE trade_id='t-1'")

        _close(net_delta=25.0)

        assert _row()["status"] == "cancelled"
        assert _balance() == pytest.approx(1000.0)

    def test_an_unknown_trade_id_credits_nothing(self, fresh_db):
        """A typo or a stale id must not move the balance on its own."""
        _insert_open_trade()

        _close(trade_id="no-such-trade", net_delta=25.0)

        assert _balance() == pytest.approx(1000.0)
