"""fmt_signal()'s AUTO-EXECUTED message never included the MT5 ticket at
all -- the caller (engine.py) had it on `trade_result["mt5_ticket"]` and
simply never threaded it through. Every other trade-notification
formatter (fmt_trade_open, fmt_instant_followup) already states the
ticket; this was the one gap."""
import os
import tempfile

import pytest

from backend.src.db import database as db
from backend.src.services.telegram import alerts as ta


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    os.remove(path)


_PARSED = {"direction": "BUY", "entry_low": 2399.0, "entry_high": 2401.0,
          "stop_loss": 2390.0, "tp1": 2410.0}


def test_executed_signal_includes_mt5_ticket(fresh_db):
    msg = ta.fmt_signal(
        _PARSED, "TestChannel", executed=True, exec_lot=0.10, exec_price=2400.0,
        strategy_name="Signal Climber", mt5_ticket=1666852148,
    )
    assert "MT5 Ticket: 1666852148" in msg


def test_grid_template_placeholder_ticket_reported_as_pending(fresh_db):
    """An EA Template grid parent row genuinely has ticket=0 (real fills
    land as separate per-leg tickets) -- that's a real value, not a
    missing one, so it must read as an explicit placeholder rather than
    being silently omitted or shown as a bare 0."""
    msg = ta.fmt_signal(
        _PARSED, "TestChannel", executed=True, exec_lot=0.10, exec_price=0.0,
        strategy_name="Template: Sig Gen Grid", mt5_ticket=0,
    )
    assert "MT5 Ticket:" in msg
    assert "pending" in msg.lower()
    assert "MT5 Ticket: 0\n" not in msg


def test_unexecuted_signal_has_no_ticket_line(fresh_db):
    msg = ta.fmt_signal(
        _PARSED, "TestChannel", executed=False,
        skip_reason="Auto-execution is OFF",
    )
    assert "MT5 Ticket" not in msg


def test_ticket_omitted_when_not_supplied_at_all(fresh_db):
    """Backward compatible -- a caller that doesn't pass mt5_ticket (e.g.
    the stale-signal path, which never executes) gets the old behaviour,
    not a spurious ticket line."""
    msg = ta.fmt_signal(
        _PARSED, "TestChannel", executed=True, exec_lot=0.10, exec_price=2400.0,
        strategy_name="Scale Out",
    )
    assert "MT5 Ticket" not in msg
