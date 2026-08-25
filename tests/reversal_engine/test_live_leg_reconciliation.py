"""Reversal Engine live reconciliation across EA Template legs.

The signal records ONE mt5_ticket -- the leg that promoted the trade row. A
grid template opens an anchor plus N pending legs as separate broker
positions, so reconciling that single ticket both under-counted the virtual
balance (roughly a quarter of the real result on a 4-leg grid) and closed the
signal while sibling legs were still running.
"""
import asyncio
import os
import tempfile

import pytest

from backend.src.db import database as db
from backend.src.services.broker.ea_bridge import comment_for_trade
from backend.src.services.reversal_engine import reversal_engine_manage as rem


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


TRADE_ID = "5b88a61e-6544-4f"
PROMOTED = 1677417630
SIBLINGS = [1677417633, 1677417637]


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("vsig-1", "Reversal Engine", "SELL", 4048.0, 4051.0, 4056.0, "active", 0),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,direction,"
            "entry_low,entry_high,entry_price,lot_size,remaining_lots,stop_loss,status,"
            "open_time,strategy,tg_source,managed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (TRADE_ID, "vsig-1", PROMOTED, "SELL", 4048.0, 4051.0, 4048.99,
             0.03, 0.03, 4056.0, "closed", 0, "template:Sig Gen Grid",
             "Reversal Engine", "ea"),
        )
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _Bridge:
    """Deal history shaped like the real bridge: an opening deal per leg
    carrying the EA's comment, plus a closing deal with the P&L."""

    def __init__(self, open_tickets=()):
        self.open_tickets = list(open_tickets)
        self.history_calls = []

    async def get_positions(self):
        return [{"ticket": t} for t in self.open_tickets]

    async def get_deal_history(self, days=7):
        base = comment_for_trade(TRADE_ID)
        out = []
        for tag, ticket in ((f"{base}a1", PROMOTED),
                            (f"{base}g2", SIBLINGS[0]),
                            (f"{base}g3", SIBLINGS[1])):
            out.append({"entry": 0, "position_id": ticket, "comment": tag})
        # An unrelated trade's leg, and a broker stop-out comment.
        out.append({"entry": 0, "position_id": 999, "comment": "ea:ffffffff-0a1"})
        out.append({"entry": 0, "position_id": 998, "comment": "[sl 4046.50]"})
        return out

    async def get_position_history(self, ticket):
        self.history_calls.append(ticket)
        return [
            {"entry": 0, "type": 1, "position_id": ticket, "price": 4049.0, "profit": 0.0},
            {"entry": 1, "type": 0, "position_id": ticket, "price": 4040.0,
             "profit": 10.0, "swap": 0.0, "fee": 0.0},
        ]


class _Engine(rem._ManagementMixin):
    def __init__(self, bridge):
        self._bridge = bridge
        self._live_missing_streak = {}

    def _notify_refresh(self):
        pass          # UI push, irrelevant here


def _sig():
    return {"id": 1, "signal_ref": "RE-TEST", "mt5_ticket": PROMOTED,
            "vantage_signal_id": "vsig-1", "direction": "SELL",
            "entry_low": 4048.0, "entry_high": 4051.0, "trigger_price": 4049.0}


def test_all_legs_of_a_template_trade_are_discovered(fresh_db):
    eng = _Engine(_Bridge())
    legs = asyncio.run(eng._template_leg_tickets(_sig(), PROMOTED))
    assert legs == {PROMOTED, *SIBLINGS}


def test_another_trades_legs_are_not_pulled_in(fresh_db):
    eng = _Engine(_Bridge())
    legs = asyncio.run(eng._template_leg_tickets(_sig(), PROMOTED))
    assert 999 not in legs and 998 not in legs


def test_a_still_open_sibling_keeps_the_signal_open(fresh_db, monkeypatch):
    """The promoted leg closing first must not end the signal -- that is what
    truncated the recorded result."""
    closed = {"n": 0}
    monkeypatch.setattr(rem.re_db, "close_signal",
                        lambda *a, **kw: closed.__setitem__("n", closed["n"] + 1))
    eng = _Engine(_Bridge(open_tickets=[SIBLINGS[1]]))   # promoted gone, one leg live
    for _ in range(rem._LIVE_MISSING_THRESHOLD + 2):
        asyncio.run(eng._reconcile_live_signal(_sig()))
    assert closed["n"] == 0


def test_pnl_sums_every_leg_once_they_are_all_closed(fresh_db, monkeypatch):
    banked = {}

    def _close(sig_id, close_price, outcome, pnl_pts, **kw):
        banked.update(kw, outcome=outcome)

    monkeypatch.setattr(rem.re_db, "close_signal", _close)
    # Imported inside the function under test; it needs the RE database, which
    # this test has no reason to stand up.
    from backend.src.services.reversal_engine import ml_engine as re_ml
    monkeypatch.setattr(re_ml, "record_outcome", lambda *a, **kw: None)
    eng = _Engine(_Bridge(open_tickets=[]))
    for _ in range(rem._LIVE_MISSING_THRESHOLD):
        asyncio.run(eng._reconcile_live_signal(_sig()))
    # 3 legs x $10 -- the single-ticket version banked $10.
    assert banked.get("net_pnl_dollars") == pytest.approx(30.0)
    assert banked.get("balance_delta") == pytest.approx(30.0)
    assert banked.get("outcome") == "win"


def test_non_template_trade_reconciles_from_its_own_ticket_only(fresh_db):
    """A built-in strategy has exactly one position; the leg lookup must not
    change its behaviour."""
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET strategy='conservative' "
                     "WHERE trade_id=?", (TRADE_ID,))
    eng = _Engine(_Bridge())
    legs = asyncio.run(eng._template_leg_tickets(_sig(), PROMOTED))
    assert legs == {PROMOTED}


def test_lookup_failure_degrades_to_the_single_ticket(fresh_db):
    class _Broken(_Bridge):
        async def get_deal_history(self, days=7):
            raise RuntimeError("bridge down")

    eng = _Engine(_Broken())
    legs = asyncio.run(eng._template_leg_tickets(_sig(), PROMOTED))
    assert legs == {PROMOTED}
