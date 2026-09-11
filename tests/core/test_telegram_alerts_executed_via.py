"""Trade Updated (fmt_instant_followup) now states whether the update was
applied via the EA or via Python, matching fmt_trade_open's existing
"Executed via" line -- same managed_by field, same wording."""

import pytest

from backend.src.db import database as db
from backend.src.services.telegram import alerts as ta


def _instant_trade(managed_by):
    return {
        "trade_id": "t1", "direction": "BUY", "mt5_ticket": 123,
        "entry_price": 2400.0, "entry_low": 2399.0, "entry_high": 2401.0,
        "lot_size": 0.1, "stop_loss": 2390.0, "strategy": "scale_out",
        "managed_by": managed_by,
    }


def test_trade_updated_states_ea_when_ea_managed(fresh_db):
    msg = ta.fmt_instant_followup(_instant_trade("ea"), {"stop_loss": 2390.0}, "TestChannel")
    assert "*XAUUSD — Trade Updated*" in msg
    assert "Executed via: EA" in msg


def test_trade_updated_states_python_when_python_managed(fresh_db):
    msg = ta.fmt_instant_followup(_instant_trade("python"), {"stop_loss": 2390.0}, "TestChannel")
    assert "Executed via: Python" in msg


def test_trade_updated_defaults_to_python_when_managed_by_missing(fresh_db):
    trade = _instant_trade("python")
    del trade["managed_by"]
    msg = ta.fmt_instant_followup(trade, {"stop_loss": 2390.0}, "TestChannel")
    assert "Executed via: Python" in msg
