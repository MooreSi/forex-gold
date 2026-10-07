"""The Market Order and Limit Order dialogs: strategy choice, and a pause.

Owner, 2026-10-07:

* "a market or limit order can bypass any paused trading". Manual limit
  orders already did (owner, 2026-10-05); the Market Order dialog was still
  refused by `open_trade`'s pause and circuit-breaker checks. Only the dialog
  (`source_name="manual_market"`) is exempt: Set & Forget, ORB and every
  automated route still stop.
* "an option to select a strategy/ea or leave it blank and just use the take
  profit figure which would be a 100% take profit". Blank is the single-target
  management `orb_fixed` already runs in Python and the EA: one full close at
  TP1, no partials, no breakeven, no trailing, never DPM. It needs a take
  profit; without one there is nothing to close at.
* The Limit Order dialog takes the same choice instead of eight TP levels.

Nothing here reaches a broker. The market path runs against a fake bridge
whose `place_order` records; the EA is a recorder whose `place_pending_order`
belongs to this file; Telegram is stubbed.
"""
from __future__ import annotations

import asyncio
import time
import types
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime
from backend.src.services.broker import ea_bridge as ea_bridge_mod
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.trading import manual_limit_order as mlo
from backend.src.services.trading import manual_market_order as mmo
from backend.src.utils.models import (
    STRATEGY_LIMIT_RUNNER, STRATEGY_ORB_FIXED, STRATEGY_SCALE_OUT,
)
from tests._fakes import _FakeBridge


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    async def _send(*a, **k):
        return None
    monkeypatch.setattr(telegram_alerts, "send_message", _send)


class _MarketBridge(_FakeBridge):
    """The shared fake (tests/_fakes.py), plus what the market path calls:
    a fresh tick, a recorded `place_order`, an account and candles."""

    def __init__(self):
        super().__init__(tick=_tick())
        self.place_order_calls = []

    async def get_fresh_tick(self):
        return self._tick

    async def place_order(self, direction, lots, sl, tp, comment=""):
        self.place_order_calls.append({"direction": direction, "lots": lots,
                                       "sl": sl, "tp": tp})
        return {"ticket": 999, "fill_price": 2400.0}

    async def get_account(self):
        return {"balance": 0}

    async def get_candles(self, timeframe="M5", count=200):
        return []


def _tick():
    return SimpleNamespace(bid=2399.8, ask=2400.2, spread_points=4.0)


@pytest.fixture
def engine(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _MarketBridge()
    e._cfg = {}
    return e


def _pause():
    with db.db():
        db.set_app_config("trade_pause_until", str(time.time() + 3600))
        db.set_app_config("risk_halt_reason",
                          "Daily goal secured: +$39.24 today vs a goal of $23.77")


def _market(engine, **kw):
    kw.setdefault("stop_loss", 2390.0)
    kw.setdefault("lot_size", 0.05)
    return asyncio.run(TradingRuntime.open_manual_market_order(engine, "BUY", **kw))


def _strategy_of(trade_id):
    with db.db() as conn:
        return conn.execute("SELECT strategy FROM vantage_simulated_trades "
                            "WHERE trade_id=?", (trade_id,)).fetchone()[0]


# ── the Market Order dialog through a pause ──────────────────────────────────

def test_the_market_order_dialog_places_through_a_pause(engine):
    _pause()
    result = _market(engine, take_profit=2420.0)
    assert result["trade_id"]
    assert len(engine._bridge.place_order_calls) == 1


def test_an_automated_market_order_is_still_refused_while_paused(engine):
    """Negative control: Set & Forget Auto is not the dialog."""
    _pause()
    with pytest.raises(ValueError, match="Trading paused"):
        _market(engine, take_profit=2420.0, source_name="Set & Forget Auto")
    assert engine._bridge.place_order_calls == []


def test_the_market_order_dialog_is_still_stopped_by_a_tripped_breaker(engine, monkeypatch):
    """Scope narrowed to the daily goal's pause pending the owner's answer."""
    monkeypatch.setattr(db, "get_circuit_breaker_state",
                        lambda: {"is_active": True, "remaining_secs": 600,
                                 "consec_losses": 3})
    with pytest.raises(ValueError, match="circuit breaker"):
        _market(engine, take_profit=2420.0)


def test_an_automated_order_is_still_stopped_by_a_tripped_breaker(engine, monkeypatch):
    monkeypatch.setattr(db, "get_circuit_breaker_state",
                        lambda: {"is_active": True, "remaining_secs": 600,
                                 "consec_losses": 3})
    with pytest.raises(ValueError, match="circuit breaker"):
        _market(engine, take_profit=2420.0, source_name="Set & Forget Auto")
    assert engine._bridge.place_order_calls == []


# ── the single-target choice ─────────────────────────────────────────────────

def test_the_single_target_choice_is_orb_fixed_management(engine):
    result = _market(engine, take_profit=2420.0, strategy=mmo.SINGLE_TP_STRATEGY)
    assert mmo.SINGLE_TP_STRATEGY == STRATEGY_ORB_FIXED
    assert _strategy_of(result["trade_id"]) == STRATEGY_ORB_FIXED


def test_the_single_target_choice_needs_a_take_profit(engine):
    with pytest.raises(ValueError, match="take profit"):
        _market(engine, strategy=mmo.SINGLE_TP_STRATEGY)
    assert engine._bridge.place_order_calls == []


def test_a_chosen_strategy_manages_the_trade(engine):
    result = _market(engine, take_profit=2420.0, strategy=STRATEGY_SCALE_OUT)
    assert _strategy_of(result["trade_id"]) == STRATEGY_SCALE_OUT


# ── the Limit Order dialog: the same choice, one target ──────────────────────

def _install_ea(monkeypatch):
    calls = []

    async def place_pending_order(trade_id, direction, price, lot, sl, tps, pcts,
                                  be_at_pos, **kw):
        calls.append(dict(tps=tps, pcts=pcts, be_at_pos=be_at_pos, **kw))
        return {"type": "pending_order_placed", "ticket": 987}

    monkeypatch.setattr(ea_bridge_mod, "get_instance", lambda: types.SimpleNamespace(
        is_ea_healthy=lambda: True, place_pending_order=place_pending_order))
    return calls


def _limit(**over):
    kw = dict(direction="BUY", entry_low=2390.0, entry_high=2400.0,
              stop_loss=2380.0, tp1=2420.0, lot_size=0.05)
    kw.update(over)
    return asyncio.run(mlo.open_manual_limit_order(None, **kw))


def _resting_strategy(trade_id):
    with db.db() as conn:
        return conn.execute("SELECT strategy FROM vantage_pending_orders "
                            "WHERE trade_id=?", (trade_id,)).fetchone()[0]


def test_a_limit_order_left_blank_rests_as_a_single_full_close(fresh_db, monkeypatch):
    calls = _install_ea(monkeypatch)
    out = _limit(strategy=mlo.SINGLE_TP_STRATEGY)
    assert calls[0]["strategy"] == STRATEGY_ORB_FIXED
    assert calls[0]["tps"] == {1: 2420.0}
    assert calls[0]["pcts"] == [1.0]
    # The stamp read back at fill time decides how the position is managed.
    assert _resting_strategy(out["trade_id"]) == STRATEGY_ORB_FIXED


def test_a_limit_order_with_a_strategy_rests_under_it(fresh_db, monkeypatch):
    calls = _install_ea(monkeypatch)
    out = _limit(strategy=STRATEGY_SCALE_OUT)
    assert calls[0]["strategy"] == STRATEGY_SCALE_OUT
    assert _resting_strategy(out["trade_id"]) == STRATEGY_SCALE_OUT


def test_a_limit_order_with_no_strategy_field_is_unchanged(fresh_db, monkeypatch):
    """The API default stays Limit Runner: other callers send no strategy."""
    calls = _install_ea(monkeypatch)
    out = _limit()
    assert calls[0]["strategy"] == STRATEGY_LIMIT_RUNNER
    assert _resting_strategy(out["trade_id"]) == STRATEGY_LIMIT_RUNNER


def _save_template(name, mode="single"):
    from backend.src.services.broker import ea_templates as et
    et.save_ea_template(name, {
        "mode": mode, "anchors": 1, "pendings": 0 if mode == "single" else 2,
        "sl_pips": 50.0, "tp1_pips": 40.0, "tp2_pips": 50.0,
        "tp1_pct": 55.0, "tp2_pct": 45.0,
    })


def test_a_limit_order_can_rest_under_a_single_template(fresh_db, monkeypatch):
    """EA v1.07 runs a single-mode template on a resting order
    (ApplyTemplateToPending), so the dialog offers templates as the market
    order does. The template's own ladder, measured from the resting price,
    replaces the typed take profit -- as it does on the market order."""
    _save_template("GD single")
    calls = _install_ea(monkeypatch)
    out = _limit(strategy="template:GD single")
    sent = calls[0]
    assert sent["strategy"] == "template:GD single"
    assert sent["template"]["name"] == "GD single"
    # 40 and 50 pips above the BUY's resting price (entry_high, 2400).
    assert sent["tps"] == {1: pytest.approx(2404.0), 2: pytest.approx(2405.0)}
    assert _resting_strategy(out["trade_id"]) == "template:GD single"


def test_a_limit_order_refuses_a_grid_template(fresh_db, monkeypatch):
    """A grid stages its own legs; it cannot rest as one limit order."""
    _save_template("GD grid", mode="grid")
    calls = _install_ea(monkeypatch)
    with pytest.raises(ValueError, match="grid"):
        _limit(strategy="template:GD grid")
    assert calls == []


def test_a_limit_order_refuses_a_template_that_does_not_exist(fresh_db, monkeypatch):
    calls = _install_ea(monkeypatch)
    with pytest.raises(ValueError, match="template"):
        _limit(strategy="template:Nope")
    assert calls == []


def test_a_limit_order_refuses_an_unknown_strategy(fresh_db, monkeypatch):
    calls = _install_ea(monkeypatch)
    with pytest.raises(ValueError, match="Unknown strategy"):
        _limit(strategy="made_up")
    assert calls == []
