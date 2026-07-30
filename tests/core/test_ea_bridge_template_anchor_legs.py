"""EA Template Anchor legs: the EA opens each anchor as its own broker
position and reports it under "<trade_id>-a<N>" -- an unsolicited
"trade_opened" (an anchor is a market fill, so it never arrives as
"pending_order_filled") plus tp_hit/sl_moved/trade_closed under the same
suffixed id.

Confirmed live 2026-07-29 (trades 76687f1a / e93f3fe7 / c2ebb432): none of
those events mapped to anything. The unsolicited trade_opened matched no
open_trade() ack callback and was dropped, so the placeholder row kept
mt5_ticket=0 / entry_price=0 for life; every later event logged "unknown
trade_id" and was discarded; the trade sat in Active Trades at a $0 entry;
and Python's own SL check eventually closed it locally with a P&L computed
off the zero entry (-$16,086 reported for a real -$15.63 loss).
"""
import asyncio
import os
import tempfile
import time
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import ea_bridge


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


def _insert_placeholder(trade_id="tpl1", strategy="template:Sig Gen Grid",
                        tg_source="Reversal Engine", lot=0.04):
    now = time.time()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,entry_high,"
            "stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", tg_source, "SELL", 4016.0, 4016.0, 4021.5, lot, "active", now),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,direction,"
            "entry_low,entry_high,entry_price,lot_size,remaining_lots,stop_loss,tp1,status,"
            "open_time,strategy,managed_by,tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", 0, "SELL", 4016.0, 4016.0, 0.0, lot, lot, 4021.5,
             4010.0, "open", now, strategy, "ea", tg_source),
        )


# ── id parsing ────────────────────────────────────────────────────────────

def test_split_leg_trade_id_handles_anchor_grid_and_plain_ids():
    assert ea_bridge.split_leg_trade_id("abc-1234-a1") == ("abc-1234", "a", "1")
    assert ea_bridge.split_leg_trade_id("abc-1234-g12") == ("abc-1234", "g", "12")
    # A bare trade_id must come back untouched with no leg kind -- uuid slices
    # contain hyphens of their own, so this cannot be a plain "-" split.
    assert ea_bridge.split_leg_trade_id("c2ebb432-8def-41") == ("c2ebb432-8def-41", None, "")
    assert ea_bridge.leg_label("a", "1") == "Anchor Leg 1"
    assert ea_bridge.leg_label("g", "3") == "Grid Leg 3"


# ── anchor fill promotion ─────────────────────────────────────────────────

def test_unsolicited_trade_opened_for_anchor_leg_promotes_placeholder(fresh_db):
    _insert_placeholder()
    bridge = ea_bridge.EABridge(engine=None)
    sent = []

    async def _capture(msg, *a, **k):
        sent.append(msg)

    with mock.patch("forex_trader.core.telegram_alerts.send_message", side_effect=_capture):
        asyncio.run(bridge._dispatch({
            "type": "trade_opened", "trade_id": "tpl1-a1",
            "ticket": 1399606862, "fill_price": 4016.29,
        }))
        asyncio.run(asyncio.sleep(0))

    with db.db() as conn:
        ticket, entry, order_type = conn.execute(
            "SELECT mt5_ticket,entry_price,order_type FROM vantage_simulated_trades "
            "WHERE trade_id='tpl1'"
        ).fetchone()
    assert ticket == 1399606862
    assert entry == 4016.29
    assert order_type == "market"      # an anchor is a market fill, not a resting order
    assert bridge._active["tpl1"]["ticket"] == 1399606862

    assert len(sent) == 1
    body = sent[0]
    assert "Anchor Leg 1" in body
    assert "Trade Opened" in body      # a leg going live is a trade execution
    assert "1399606862" in body
    assert "4016.29" in body
    assert "Sig Gen Grid" in body
    assert "Reversal Engine" in body


def test_plain_trade_opened_with_no_waiting_ack_is_not_treated_as_a_leg(fresh_db):
    """Only leg-suffixed ids may promote a row -- an un-suffixed
    trade_opened with no waiting ack (a late ack after open_trade() timed
    out) must not rewrite trade state behind the caller's back."""
    _insert_placeholder()
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._dispatch({
        "type": "trade_opened", "trade_id": "tpl1", "ticket": 777, "fill_price": 4000.0,
    }))
    with db.db() as conn:
        ticket = conn.execute(
            "SELECT mt5_ticket FROM vantage_simulated_trades WHERE trade_id='tpl1'"
        ).fetchone()[0]
    assert ticket == 0


# ── lifecycle events under a leg id ───────────────────────────────────────

class _FakeEngine:
    def __init__(self):
        self.closed = []
        self.partials = []
        self.profit_syncs = []
        self._bridge = None

    async def _record_close(self, trade_id, close_price, reason):
        with db.db() as conn:
            conn.execute(
                "UPDATE vantage_simulated_trades SET status='closed',close_price=?,"
                "exit_reason=?,close_time=? WHERE trade_id=?",
                (close_price, reason, time.time(), trade_id),
            )
        self.closed.append((trade_id, close_price, reason))
        return {"trade_id": trade_id, "close_price": close_price, "net_pnl": -15.63,
                "reason": reason}

    async def partial_close_trade(self, trade_id, lots, price, reason):
        self.partials.append((trade_id, lots, price, reason))
        return {"partial_pnl": 1.0}

    async def get_mt5_account(self):
        return {"balance": 599.59, "equity": 599.59, "margin_free": 538.70}

    async def _schedule_profit_sync(self, trade_id, ticket):
        self.profit_syncs.append((trade_id, ticket))


def test_trade_closed_under_anchor_leg_id_closes_the_parent_row(fresh_db):
    _insert_placeholder()
    engine = _FakeEngine()
    bridge = ea_bridge.EABridge(engine=engine)
    # The row already tracks this anchor's ticket (promoted at fill time).
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET mt5_ticket=1399606862,"
                     "entry_price=4016.29 WHERE trade_id='tpl1'")
    sent = []

    async def _capture(msg, *a, **k):
        sent.append(msg)

    with mock.patch("forex_trader.core.telegram_alerts.send_message", side_effect=_capture):
        asyncio.run(bridge._dispatch({
            "type": "trade_closed", "trade_id": "tpl1-a1", "ticket": 1399606862,
            "close_price": 4021.5, "reason": "SL",
        }))
        asyncio.run(asyncio.sleep(0))

    assert engine.closed == [("tpl1", 4021.5, "SL")]
    assert engine.profit_syncs == [("tpl1", 1399606862)]
    assert len(sent) == 1
    assert "1399606862" in sent[0]
    assert "4016.29" in sent[0]        # the real entry, not $0.00
    assert "$0.00" not in sent[0]


def test_tp_hit_for_a_sibling_leg_does_not_touch_the_tracked_trade(fresh_db):
    """A second concurrent leg (cancel_pending off) has no DB row of its
    own. Its TP must not deduct lots from the leg the row does track."""
    _insert_placeholder()
    engine = _FakeEngine()
    bridge = ea_bridge.EABridge(engine=engine)
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET mt5_ticket=555,"
                     "entry_price=4016.29 WHERE trade_id='tpl1'")
    sent = []

    async def _capture(msg, *a, **k):
        sent.append(msg)

    with mock.patch("forex_trader.core.telegram_alerts.send_message", side_effect=_capture):
        asyncio.run(bridge._dispatch({
            "type": "tp_hit", "trade_id": "tpl1-a1", "ticket": 999,
            "tp_num": 1, "price": 4010.0, "lots_closed": 0.03,
        }))
        asyncio.run(asyncio.sleep(0))

    assert engine.partials == []       # nothing deducted from the tracked leg
    assert len(sent) == 1
    assert "Anchor Leg 1" in sent[0]
    assert "sibling" in sent[0].lower()


def test_trade_closed_for_a_sibling_leg_does_not_close_the_trade(fresh_db):
    _insert_placeholder()
    engine = _FakeEngine()
    bridge = ea_bridge.EABridge(engine=engine)
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET mt5_ticket=555,"
                     "entry_price=4016.29 WHERE trade_id='tpl1'")

    with mock.patch("forex_trader.core.telegram_alerts.send_message",
                    new=mock.AsyncMock()):
        asyncio.run(bridge._dispatch({
            "type": "trade_closed", "trade_id": "tpl1-g2", "ticket": 999,
            "close_price": 4021.5, "reason": "SL",
        }))

    assert engine.closed == []
    with db.db() as conn:
        status = conn.execute(
            "SELECT status FROM vantage_simulated_trades WHERE trade_id='tpl1'"
        ).fetchone()[0]
    assert status == "open"
