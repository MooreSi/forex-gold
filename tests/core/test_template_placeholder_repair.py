"""Polling self-heal for EA Template placeholder rows (mt5_ticket=0,
entry_price=0) whose leg-fill event never reached this node -- the same
$0-entry ghost the user hit twice: trade eb8ca404 (2026-07-28) and
c2ebb432 (2026-07-29, its anchor leg opened AND closed at the broker while
every leg event was being discarded as an "unknown trade_id").

Legs are matched by the comment the EA stamps on each one,
"ea:<first 10 chars of trade_id><a|g><N>".
"""
import asyncio
import os
import tempfile
import time
from unittest import mock

import pytest

from forex_trader.core import core_template_placeholder_repair as repair
from forex_trader.core import database as db


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


TRADE_ID = "c2ebb432-8def-41"   # first 10 chars -> "c2ebb432-8", as the EA slices it


class _FakeBridge:
    def __init__(self, positions=None, deals=None):
        self._positions = positions if positions is not None else []
        self._deals = deals or []

    def is_configured(self):
        return True

    async def get_positions(self):
        return self._positions

    async def get_deal_history(self, days):
        return self._deals

    async def get_account(self):
        return {"balance": 599.59, "equity": 599.59, "margin_free": 538.70}

    async def close_position(self, ticket):
        raise AssertionError("repair must never place or close a broker order")


def _insert_placeholder(trade_id=TRADE_ID, entry_price=0.0, mt5_ticket=0):
    now = time.time()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,entry_high,"
            "stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "Reversal Engine", "BUY", 4015.0, 4018.0, 4011.5, 0.04,
             "active", now),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,direction,"
            "entry_low,entry_high,entry_price,lot_size,remaining_lots,stop_loss,status,open_time,"
            "strategy,managed_by,tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, "BUY", 4015.0, 4018.0, entry_price,
             0.04, 0.04, 4011.5, "open", now, "template:Sig Gen Grid", "ea", "Reversal Engine"),
        )


def _row(trade_id=TRADE_ID):
    with db.db() as conn:
        return db.row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone())


def test_adopts_placeholder_onto_still_open_leg_position(fresh_db):
    _insert_placeholder()
    bridge = _FakeBridge(positions=[{
        "ticket": 1399609711, "volume": 0.03, "open_price": 4015.46,
        "comment": "ea:c2ebb432-8a1", "type": "BUY",
    }])
    n = asyncio.run(repair.repair_template_placeholders(bridge))
    assert n == 1
    row = _row()
    assert row["mt5_ticket"] == 1399609711
    assert row["entry_price"] == 4015.46
    assert row["lot_size"] == 0.03          # the EA's own anchor lot, not Python's sizing
    assert row["remaining_lots"] == 0.03
    assert row["status"] == "open"


def test_closes_placeholder_from_broker_deal_history(fresh_db):
    """The real c2ebb432 case: the anchor opened at 4015.46 and closed at
    4035.50 for +$60.12 while every leg event was being dropped, leaving the
    row open at a $0 entry indefinitely."""
    _insert_placeholder()
    bridge = _FakeBridge(positions=[], deals=[
        {"ticket": 1399609711, "order": 1672649002, "position_id": 1672649002,
         "entry": 0, "type": 0, "volume": 0.03, "price": 4015.46, "profit": 0.0,
         "swap": 0.0, "fee": 0.0, "time": 1785353027, "comment": "ea:c2ebb432-8a1"},
        {"ticket": 1399806615, "order": 1672926348, "position_id": 1672649002,
         "entry": 1, "type": 1, "volume": 0.03, "price": 4035.5, "profit": 60.12,
         "swap": 0.0, "fee": 0.0, "time": 1785355457, "comment": ""},
    ])
    sent = []

    async def _capture(msg, *a, **k):
        sent.append(msg)

    with mock.patch("forex_trader.core.telegram_alerts.send_message", side_effect=_capture):
        n = asyncio.run(repair.repair_template_placeholders(bridge))
        asyncio.run(asyncio.sleep(0))

    assert n == 1
    row = _row()
    assert row["status"] == "closed"
    assert row["entry_price"] == 4015.46
    assert row["close_price"] == 4035.5
    assert row["mt5_profit"] == 60.12
    assert len(sent) == 1
    assert "4015.46" in sent[0]     # real entry quoted, never "$0.00"
    assert "60.12" in sent[0]


def test_leaves_placeholder_alone_when_no_leg_has_filled(fresh_db):
    """Legs may still be resting as pending orders -- nothing to repair, and
    certainly nothing to close."""
    _insert_placeholder()
    bridge = _FakeBridge(positions=[], deals=[])
    assert asyncio.run(repair.repair_template_placeholders(bridge)) == 0
    assert _row()["status"] == "open"


def test_ignores_rows_that_already_have_a_real_entry_price(fresh_db):
    """A ticket-less row WITH an entry price is a legitimately simulated
    trade, not an unpromoted template placeholder."""
    _insert_placeholder(entry_price=4015.0)
    bridge = _FakeBridge(positions=[{
        "ticket": 1, "volume": 0.03, "open_price": 4000.0,
        "comment": "ea:c2ebb432-8a1", "type": "BUY",
    }])
    assert asyncio.run(repair.repair_template_placeholders(bridge)) == 0
    assert _row()["mt5_ticket"] == 0


def test_does_not_match_another_trades_leg_comment(fresh_db):
    _insert_placeholder()
    bridge = _FakeBridge(positions=[{
        "ticket": 42, "volume": 0.03, "open_price": 4000.0,
        "comment": "ea:99999999-0a1", "type": "BUY",
    }])
    assert asyncio.run(repair.repair_template_placeholders(bridge)) == 0
    assert _row()["mt5_ticket"] == 0
