"""A grid's anchor leg is a MARKET order. Until 2026-08-03 it was only
placed while price was at (or better than) the signal's own zone --
otherwise the whole anchor count was zeroed and only the resting legs
staged, added after six queued Reversal Engine signals on 2026-07-30 all
took a market anchor at ~4095 within seconds of each other, several in
opposite directions at the same price, none at a price their own signal
named.

2026-08-04 (explicit trading-policy directive): that guard is deliberately
removed. Every EA template now always fires its anchor leg(s) at market the
instant the signal triggers, regardless of the signal's own zone -- missing
the market move is treated as worse than entering somewhat outside the
zone. The resting legs are unaffected either way: they sit AT the zone by
construction and are the entire reason for staging early.
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


def test_anchor_still_fires_when_price_is_past_the_zone(monkeypatch, fresh_db):
    """A BUY zone of 4084-4088 takes its market anchor at 4095 -- the
    always-fire policy, not the old regression it replaced."""
    ea = _RecordingEA()
    _open(monkeypatch, ea, _TickAboveZone())
    assert ea.calls == 1
    assert ea.template["anchors"] == 1


def test_resting_legs_are_still_staged_alongside_the_anchor(monkeypatch, fresh_db):
    """The resting legs at the zone are staged the same way regardless of
    whether the anchor also fires."""
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


def test_sell_anchor_still_fires_chased_below_its_zone(monkeypatch, fresh_db):
    """A SELL chased below its zone still takes the anchor -- direction
    doesn't change the always-fire policy."""
    class _BelowSellZone:
        ask = 4070.2
        bid = 4070.0
        spread_points = 20.0

    ea = _RecordingEA()
    _open(monkeypatch, ea, _BelowSellZone(), direction="SELL",
          low=4084.0, high=4088.0, sl=4093.0)
    assert ea.template["anchors"] == 1


def test_anchor_only_template_fires_at_market_even_with_no_resting_legs(monkeypatch, fresh_db):
    """No resting legs to fall back on is no longer a reason to refuse --
    the anchor itself is the whole trade here, and it must still fire."""
    ea = _RecordingEA()
    _open(monkeypatch, ea, _TickAboveZone(), template="AnchorOnly")
    assert ea.calls == 1
    assert ea.template["anchors"] == 1
    assert ea.template["pendings"] == 0


def test_the_stored_template_is_not_mutated(monkeypatch, fresh_db):
    """The wire copy no longer diverges from the saved template at all (it
    used to lose its anchors outside the zone) -- the saved template must
    still say 1 for the next signal that arrives."""
    _open(monkeypatch, _RecordingEA(), _TickAboveZone())
    assert et.get_ea_template("Grid")["anchors"] == 1
