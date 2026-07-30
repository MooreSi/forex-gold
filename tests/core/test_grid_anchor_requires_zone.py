"""A grid's anchor leg is a MARKET order -- it may only be placed while price
is at (or better than) the signal's own zone.

That used to be guaranteed: a grid was dispatched only once price had reached
the zone. Placing on arrival removed the guarantee, and on 2026-07-30 six
queued Reversal Engine signals with zones from 4084 to 4121 each took a market
anchor at ~4095 within seconds of each other -- four BUYs and two SELLs, none
at a price its own signal named, several in opposite directions at the same
price.

The resting legs are unaffected: they sit AT the zone by construction and are
the entire reason for staging early.
"""
import asyncio
import os
import tempfile
import time

import pytest

from forex_trader.core import core_ea_templates as et
from forex_trader.core import core_open_trade as cot
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
    et.save_ea_template("Grid", {
        "mode": "grid", "anchors": 1, "pendings": 3,
        "lot_anchor": 0.02, "lot_pending": 0.02,
        "tp1_pips": 20.0, "tp1_pct": 100.0,
    })
    et.save_ea_template("AnchorOnly", {
        "mode": "grid", "anchors": 1, "pendings": 0, "grid_legs": 0,
        "lot_anchor": 0.02, "tp1_pips": 20.0, "tp1_pct": 100.0,
    })
    db.update_risk_settings({"ea_bridge_enabled": 1, "max_open_trades": 10})
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("sig-1", "Reversal Engine", "BUY", 4084.0, 4088.0, 4079.0,
             "pending", time.time()),
        )
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _TickInZone:
    """Price inside the 4084-4088 BUY zone."""
    ask = 4086.0
    bid = 4085.8
    spread_points = 20.0


class _TickAboveZone:
    """The live case: price ran to 4095 while the signal waited at 4084-4088."""
    ask = 4095.2
    bid = 4095.0
    spread_points = 20.0


class _RecordingEA:
    def __init__(self):
        self.template = None
        self.calls = 0

    def is_ea_healthy(self):
        return True

    def is_strategy_portable(self, strategy):
        return True

    async def open_trade(self, *a, **kw):
        self.calls += 1
        self.template = kw.get("template")
        return {"type": "trade_opened", "ticket": 0, "fill_price": 0.0}


class _FakeBridge:
    def __init__(self, tick):
        self._tick = tick

    async def get_fresh_tick(self):
        return self._tick


def _open(monkeypatch, ea, tick, template="Grid", direction="BUY",
          low=4084.0, high=4088.0, sl=4079.0):
    from forex_trader.core import ea_bridge as ea_mod
    monkeypatch.setattr(ea_mod, "get_instance", lambda: ea)
    return asyncio.run(cot.open_trade(
        _FakeBridge(tick), "sig-1", direction, low, high, sl,
        tp1=high + 20.0, lot_size=0.02, tick=tick,
        strategy=et.override_for_template(template),
        tg_source="Reversal Engine",
    ))


def test_anchor_is_dropped_when_price_is_past_the_zone(monkeypatch, fresh_db):
    """The regression itself: a BUY zone of 4084-4088 must not take a market
    anchor at 4095."""
    ea = _RecordingEA()
    _open(monkeypatch, ea, _TickAboveZone())
    assert ea.calls == 1
    assert ea.template["anchors"] == 0


def test_resting_legs_are_still_staged_when_the_anchor_is_dropped(monkeypatch, fresh_db):
    """Dropping the anchor must not mean placing nothing -- the resting legs
    at the zone are the whole point of staging early."""
    ea = _RecordingEA()
    _open(monkeypatch, ea, _TickAboveZone())
    assert ea.template["pendings"] == 3


def test_anchor_is_kept_when_price_is_in_the_zone(monkeypatch, fresh_db):
    ea = _RecordingEA()
    _open(monkeypatch, ea, _TickInZone())
    assert ea.template["anchors"] == 1


def test_anchor_is_kept_on_the_better_side_of_a_buy_zone(monkeypatch, fresh_db):
    """Below a BUY zone is an equal-or-better fill, not a chase --
    price_in_entry_range has always allowed it and that is unchanged."""
    class _Below:
        ask = 4080.0
        bid = 4079.9
        spread_points = 20.0

    ea = _RecordingEA()
    _open(monkeypatch, ea, _Below())
    assert ea.template["anchors"] == 1


def test_sell_zone_uses_its_own_side(monkeypatch, fresh_db):
    """A SELL chased below its zone must lose the anchor the same way."""
    class _BelowSellZone:
        ask = 4070.2
        bid = 4070.0
        spread_points = 20.0

    ea = _RecordingEA()
    _open(monkeypatch, ea, _BelowSellZone(), direction="SELL",
          low=4084.0, high=4088.0, sl=4093.0)
    assert ea.template["anchors"] == 0


def test_anchor_only_template_refuses_rather_than_filling_at_market(monkeypatch, fresh_db):
    """With no resting legs to fall back on there is nothing to stage, so the
    open must fail and leave the signal pending -- never quietly become the
    market entry the guard exists to prevent."""
    ea = _RecordingEA()
    with pytest.raises(RuntimeError, match="refusing a market anchor"):
        _open(monkeypatch, ea, _TickAboveZone(), template="AnchorOnly")
    assert ea.calls == 0


def test_the_stored_template_is_not_mutated(monkeypatch, fresh_db):
    """Only the copy on the wire loses its anchors -- the saved template must
    still say 1 for the next signal that arrives in its zone."""
    _open(monkeypatch, _RecordingEA(), _TickAboveZone())
    assert et.get_ea_template("Grid")["anchors"] == 1
