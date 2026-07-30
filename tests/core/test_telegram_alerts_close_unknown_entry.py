"""fmt_trade_close must never present a placeholder row's zero entry price as
a real one. The live message that prompted this (2026-07-29, trade
76687f1a) read:

    MT5 Ticket: 0
    Entry: $0.00  →  Exit: $4021.50
    Profit: $-16086.00

for a 0.03-lot trade whose real loss was $15.63 -- every one of those three
numbers wrong, and the ✅/❌ verdict driven by the fabricated figure.
"""
import os
import tempfile

import pytest

from forex_trader.core import database as db
from forex_trader.core import telegram_alerts as ta


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


def _placeholder_trade(**over):
    trade = {
        "trade_id": "t1", "direction": "SELL", "mt5_ticket": 0,
        "entry_price": 0.0, "lot_size": 0.04, "stop_loss": 4021.5,
        "strategy": "template:Sig Gen Grid", "tg_source": "Reversal Engine",
        "close_price": 4021.5, "net_pnl": 0.0, "close_time": 1785354753,
        "activated_at": 1785353007,
    }
    trade.update(over)
    return trade


def test_close_message_says_unknown_instead_of_zero_entry_and_fake_pnl(fresh_db):
    body = ta.fmt_trade_close(
        _placeholder_trade(), {"close_price": 4021.5, "reason": "SL", "net_pnl": 0.0}, {},
    )
    assert "MT5 Ticket: unknown" in body
    assert "Entry: unknown" in body
    assert "$0.00" not in body
    assert "Profit: unknown" in body
    assert "⚠️" in body        # not the ✅ a zero P&L would otherwise imply


def test_close_message_trusts_brokers_realised_profit_even_with_no_entry(fresh_db):
    body = ta.fmt_trade_close(
        _placeholder_trade(mt5_profit=-15.63),
        {"close_price": 4021.5, "reason": "SL"}, {},
    )
    assert "Profit: $-15.63" in body
    assert "Entry: unknown" in body
    assert "❌" in body


def test_normal_close_message_is_unchanged(fresh_db):
    body = ta.fmt_trade_close(
        _placeholder_trade(mt5_ticket=1399606862, entry_price=4016.29, mt5_profit=-15.63),
        {"close_price": 4021.5, "reason": "SL"}, {},
    )
    assert "MT5 Ticket: 1399606862" in body
    assert "Entry: $4016.29  →  Exit: $4021.50" in body
    assert "Profit: $-15.63" in body
