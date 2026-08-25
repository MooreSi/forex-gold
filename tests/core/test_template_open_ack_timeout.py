"""EA Template open when the EA's acknowledgement times out.

The 2026-07-30 runaway: a template's ack is only sent once the EA has staged
every leg, and with 1 anchor + 3 pendings that exceeded the flat 5s timeout.
The TimeoutError aborted open_trade BEFORE its INSERT, so no row existed, the
signal stayed 'pending', and PendingWatcher re-activated it every 20s. Five
signals became ~133 opens and 36 live positions the app could not see, manage
or close. Only a lucky Global Harvest at +$139 closed them.

Two properties are pinned here:
  * a timeout is treated as "unknown, possibly placed" -- a placeholder row
    is recorded for reconciliation, and the signal is marked active so the
    re-activation loop stops;
  * the ack timeout scales with the number of legs the EA has to stage.
"""
import asyncio
import os
import tempfile
import time

import pytest

from backend.src.services.broker import ea_templates as et
from backend.src.services.trading import open_trade as cot
from backend.src.db import database as db


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
    et.save_ea_template("Grid", {
        "mode": "grid", "anchors": 1, "pendings": 3,
        "lot_anchor": 0.03, "lot_pending": 0.03,
        "tp1_pips": 20.0, "tp1_pct": 100.0,
    })
    db.update_risk_settings({"ea_bridge_enabled": 1, "max_open_trades": 10})
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("sig-1", "Reversal Engine", "SELL", 4063.0, 4066.0, 4071.5,
             "pending", time.time()),
        )
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _Tick:
    ask = 4064.2
    bid = 4064.0
    spread_points = 20.0


class _TimingOutEA:
    """An EA that accepts the order and stages legs for real, but whose ack
    never gets back in time -- exactly the live failure."""

    def __init__(self):
        self.calls = 0
        self.timeout_seen = None

    def is_ea_healthy(self):
        return True

    def is_strategy_portable(self, strategy):
        return True

    async def open_trade(self, *a, **kw):
        self.calls += 1
        self.timeout_seen = kw.get("timeout")
        raise asyncio.TimeoutError()


class _FakeBridge:
    async def get_fresh_tick(self):
        return _Tick()


def _open(monkeypatch, ea):
    from backend.src.services.broker import ea_bridge as ea_mod
    monkeypatch.setattr(ea_mod, "get_instance", lambda: ea)
    return asyncio.run(cot.open_trade(
        _FakeBridge(), "sig-1", "SELL", 4063.0, 4066.0, 4071.5,
        tp1=4060.0, lot_size=0.04, tick=_Tick(),
        strategy=et.override_for_template("Grid"),
        tg_source="Reversal Engine",
    ))


def test_timeout_records_a_placeholder_instead_of_raising(monkeypatch, fresh_db):
    """The core fix: the EA may already hold real legs, so the trade must be
    recorded, not discarded."""
    result = _open(monkeypatch, _TimingOutEA())
    assert result["trade_id"]
    with db.db() as conn:
        row = db.row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id=?",
            (result["trade_id"],),
        ).fetchone())
    assert row, "no trade row was written for a timed-out template open"
    # The placeholder shape core_template_placeholder_repair already adopts.
    assert not row["mt5_ticket"]
    assert not row["entry_price"]
    assert row["managed_by"] == "ea"


def test_timeout_marks_the_signal_active_so_reactivation_stops(monkeypatch, fresh_db):
    """This is what actually breaks the loop: PendingWatcher only re-activates
    signals still sitting at status='pending'."""
    _open(monkeypatch, _TimingOutEA())
    with db.db() as conn:
        status = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id='sig-1'"
        ).fetchone()[0]
    assert status == "active"


def test_ack_timeout_scales_with_leg_count(monkeypatch, fresh_db):
    """1 anchor + 3 pendings is four synchronous broker round trips; the old
    flat 5s could not cover it."""
    ea = _TimingOutEA()
    _open(monkeypatch, ea)
    assert ea.timeout_seen == pytest.approx(30.0)   # 10 + 5*4


def test_one_timeout_produces_exactly_one_open_attempt(monkeypatch, fresh_db):
    ea = _TimingOutEA()
    _open(monkeypatch, ea)
    assert ea.calls == 1


def test_non_template_timeout_still_raises(monkeypatch, fresh_db):
    """Only templates get the placeholder treatment -- a built-in strategy has
    a Python-managed fallback path and must not silently record a ghost."""
    from backend.src.services.broker import ea_bridge as ea_mod
    ea = _TimingOutEA()
    monkeypatch.setattr(ea_mod, "get_instance", lambda: ea)

    class _RejectingBridge(_FakeBridge):
        async def place_order(self, *a, **kw):
            return {"error": "bridge down"}

    with pytest.raises(Exception):
        asyncio.run(cot.open_trade(
            _RejectingBridge(), "sig-1", "SELL", 4063.0, 4066.0, 4071.5,
            tp1=4060.0, lot_size=0.04, tick=_Tick(),
            strategy="scale_out", tg_source="Reversal Engine",
        ))
