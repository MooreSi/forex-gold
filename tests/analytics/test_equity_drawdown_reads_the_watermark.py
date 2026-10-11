"""`get_equity_drawdown_pct` reads the bugs/042 watermark.

It used to ask `vantage_simulation_account` for a `peak_balance` column that
the table has not had since bugs/042 moved the watermark to `app_config`. The
OperationalError went into a bare `except Exception: pass`, so the function
returned 0.0 on every call: 7,553 of 7,553 Reversal training vectors and 158
of 158 Breakout ones carry 0 for this feature on the owner's Mac (2026-10-11).

Current is the LIVE MetaTrader balance, never the simulation ledger: the
watermark is set from the live balance (`close_trade._update_peak_balance`),
and on the owner's demo database the ledger reads -$2,342.65 against a
$1,457.91 peak. With no live balance the answer is 0.0 -- which is also what
every engine sees today, because whether they start passing one is the
owner's decision (simon-handover/053).

The numbers below are the owner's demo account on 2026-10-11.
"""
from __future__ import annotations

from backend.src.db import database as db_module

PEAK = 1457.91
LIVE_BALANCE = 1000.00
LEDGER_BALANCE = -2342.65


def _seed(peak=PEAK, ledger=LEDGER_BALANCE):
    with db_module.db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO vantage_simulation_account "
            "(id, balance, currency, reset_at) VALUES (1, ?, 'USD', 0)",
            (ledger,))
    if peak is not None:
        db_module.set_app_config("peak_balance", str(peak))


class TestTheSchemaThisIsAbout:
    def test_the_simulation_account_has_no_peak_column(self, fresh_db):
        """The premise. If this ever fails the column came back and the
        reader's source should be re-read, not this test loosened."""
        with db_module.db() as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(vantage_simulation_account)")}
        assert cols == {"id", "balance", "currency", "reset_at"}


class TestDrawdownFromTheWatermark:
    def test_live_balance_below_the_peak_is_a_drawdown(self, fresh_db):
        _seed()
        assert db_module.get_equity_drawdown_pct(LIVE_BALANCE) == 0.3141

    def test_the_simulation_ledger_is_never_the_current_balance(self, fresh_db):
        """No live balance means "unknown", not "use the ledger". The ledger
        would give (1457.91 + 2342.65) / 1457.91, capped at 1.0."""
        _seed()
        assert db_module.get_equity_drawdown_pct() == 0.0

    def test_no_watermark_is_no_drawdown(self, fresh_db):
        """The state bugs/042 leaves an account in until its next close."""
        _seed(peak=None)
        assert db_module.get_equity_drawdown_pct(LIVE_BALANCE) == 0.0

    def test_a_balance_at_or_above_the_peak_is_no_drawdown(self, fresh_db):
        _seed()
        assert db_module.get_equity_drawdown_pct(PEAK) == 0.0
        assert db_module.get_equity_drawdown_pct(PEAK + 50) == 0.0

    def test_capped_at_one(self, fresh_db):
        _seed()
        assert db_module.get_equity_drawdown_pct(-10.0) == 1.0
