"""The header badge says so when the daily loss limit is refusing orders.

Owner, 2026-11-11: "its still saying VPS: Trading Active when its actually
blocked, this needs updating".

That morning the VPS closed two losses (-$31.29) against a -$39.29 limit (4%
of $982.32) with `daily_limit_counts_open_risk` on. Every Reversal Engine
order after that risked $11.48 to $15.00, so `position_guards` refused 17 of
them between 03:30 and 08:42 UTC, each with "Open risk counts toward the daily
loss limit ... Order refused." The closed-only halt
(`apply_daily_loss_halt_on_close`) never fired, because -$31.29 had not passed
-$39.29, so nothing paused and the badge read "Trading Active" all morning.

The badge cannot know the next order's risk. It knows the last refused one:
the guard records it, and the badge reports the hold while that same order
would still be refused by the same sum. It lifts by itself when the trading
day ends, when a profitable close or a closed trade frees enough room, or when
the switch or the limit changes.

Nothing here places an order or reaches a broker. The bridge is a fake with a
canned balance.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.services.risk import governor
from backend.src.services.risk import open_risk_hold
from backend.src.services.risk import position_guards as pg
from backend.src.services.risk import trading_status as ts
from tests._fakes import _FakeBridge

BALANCE = 951.03         # 982.32 at the day's start, less -31.29 closed
DAY_BASE = 982.32
LOT = 0.03                # $15 at a 5-point stop


class _Bridge(_FakeBridge):
    def __init__(self, tick, balance=BALANCE):
        super().__init__(tick=tick)
        self.balance = balance

    async def get_account(self):
        return {"balance": self.balance}


def _tick(bid=4120.78):
    return SimpleNamespace(bid=bid, ask=bid + 0.21, spread_points=21.0)


def _settings(**kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    with db.db() as conn:
        conn.execute(f"UPDATE vantage_risk_settings SET {sets} WHERE id=1", tuple(kw.values()))
    db._rs_cache = None
    db._rs_cache_ts = 0.0


def _closed(trade_id, net):
    close_time = max(time.time() - 60, governor.calendar_day_start_ts() + 1)
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "Reversal Engine", "BUY", 4125.0, 4125.0, 4120.0, LOT,
             "active", time.time()))
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,direction,"
            "entry_low,entry_high,entry_price,lot_size,remaining_lots,stop_loss,status,"
            "open_time,close_time,net_pnl,strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", 1000 + len(trade_id), "BUY", 4125.0, 4125.0,
             4125.0, LOT, LOT, 4120.0, "closed", time.time() - 600, close_time, net,
             "scalp"))


def _refuse_one(lots=LOT):
    """One order through the real guard, which must refuse it."""
    bridge = _Bridge(_tick())
    tick = bridge._tick
    with pytest.raises(ValueError, match="daily loss limit") as exc:
        asyncio.run(pg.check(bridge, signal_id="new", direction="BUY",
                             stop_loss=tick.ask - 5.0, lot_size=lots,
                             strategy="scalp", tick=tick, rs=db.get_risk_settings()))
    return str(exc.value)


@pytest.fixture
def morning(fresh_db, monkeypatch):
    """11 Nov after the second loss: -$31.29 closed, the open-risk switch on."""
    _settings(daily_limit_counts_open_risk=1, risk_governor_enabled=0,
              max_daily_loss_pct=4.0)
    _closed("c1", -15.57)
    _closed("c2", -15.72)
    # The wall clock decides these two; they have their own tests.
    monkeypatch.setattr(ts, "_session_state", lambda: (True, "london"))
    monkeypatch.setattr(ts, "_news_state", lambda: {"paused": False})


class TestTheBadgeReportsTheHold:

    def test_a_loss_inside_the_limit_with_no_refusal_is_still_active(self, morning):
        assert ts.badge()["state"] == "ok"

    def test_after_a_refusal_the_badge_says_entries_are_blocked(self, morning):
        _refuse_one()

        out = ts.badge()

        assert out["state"] == "daily_loss"
        assert out["label"] == "Daily Loss Limit: Entries Blocked"
        assert out["can_resume"] is False

    def test_the_detail_names_the_room_left_and_the_refused_order(self, morning):
        _refuse_one()

        detail = ts.badge()["detail"]

        # 39.29 - 31.29 = 8.00 of room; the refused order risked 15.00.
        assert "$8.00" in detail
        assert "$15.00" in detail
        assert "-$39.29" in detail
        assert "-31.29" in detail


class TestItLiftsByItself:

    def test_a_refusal_from_an_earlier_trading_day_is_not_reported(self, morning):
        _refuse_one()
        rec = json.loads(db.get_app_config(open_risk_hold.CONFIG_KEY))
        rec["day_start"] -= 86400.0
        db.set_app_config(open_risk_hold.CONFIG_KEY, json.dumps(rec))

        assert ts.badge()["state"] == "ok"

    def test_a_profitable_close_that_frees_the_room_lifts_it(self, morning):
        _refuse_one()
        _closed("w1", +20.0)          # -11.29 closed: 28.00 of room > 15.00

        assert ts.badge()["state"] == "ok"

    def test_switching_open_risk_off_lifts_it(self, morning):
        _refuse_one()
        _settings(daily_limit_counts_open_risk=0)

        assert ts.badge()["state"] == "ok"

    def test_raising_the_limit_enough_lifts_it(self, morning):
        _refuse_one()
        _settings(max_daily_loss_pct=6.0)   # -58.94: 27.65 of room > 15.00

        assert ts.badge()["state"] == "ok"


class TestItsPlaceInTheOrder:

    def test_a_halt_outranks_it(self, morning, monkeypatch):
        _refuse_one()
        monkeypatch.setattr(ts, "_trade_pause_until", lambda: time.time() + 3600)

        assert ts.badge()["state"] == "halted"

    def test_a_closed_session_outranks_it(self, morning, monkeypatch):
        _refuse_one()
        monkeypatch.setattr(ts, "_session_state", lambda: (False, "london"))

        assert ts.badge()["state"] == "session_closed"

    def test_it_outranks_a_news_blackout(self, morning, monkeypatch):
        """A blackout lifts in minutes; this lasts until the day ends."""
        _refuse_one()
        monkeypatch.setattr(ts, "_news_state", lambda: {
            "paused": True, "label": "News", "detail": "CPI", "resume_ts": None})

        assert ts.badge()["state"] == "daily_loss"


class TestRecordingNeverChangesTheRefusal:

    def test_a_failing_record_still_refuses_with_the_same_reason(self, morning, monkeypatch):
        expected = _refuse_one()

        def _boom(**_kw):
            raise RuntimeError("disk full")
        monkeypatch.setattr(open_risk_hold, "remember", _boom)

        assert _refuse_one() == expected

    def test_an_allowed_order_records_nothing(self, morning):
        bridge = _Bridge(_tick())
        tick = bridge._tick
        asyncio.run(pg.check(bridge, signal_id="new", direction="BUY",
                             stop_loss=tick.ask - 1.0, lot_size=0.01,   # $1
                             strategy="scalp", tick=tick, rs=db.get_risk_settings()))

        assert db.get_app_config(open_risk_hold.CONFIG_KEY) in (None, "")


def test_on_a_paired_mac_the_vps_hold_is_labelled_as_the_vps(monkeypatch):
    remote = {"state": "daily_loss", "label": "Daily Loss Limit: Entries Blocked",
              "detail": "Orders that risk more than $8.00 are refused.",
              "until": None, "resume_ts": None, "can_resume": False}

    out = ts._as_the_vps_reports_it({"connected": True, "badge": remote})

    assert out["state"] == "daily_loss"
    assert out["label"] == "VPS: Daily Loss Limit: Entries Blocked"
    assert out["can_resume_on_vps"] is False
