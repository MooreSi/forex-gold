"""A secured daily goal stays secured for the rest of the trading-clock day.

Owner, 2026-10-10: "todays goal - this shouldn't reset". On 9 October the goal
was secured at 05:46 (+$29.57 vs $22.11, halt until midnight). At 07:30 the
halt was lifted the way Resume lifts it, which also restarted the goal's
window, so the morning's +$29.57 no longer counted and nothing stopped the
day again. It finished at -$39.28 on MT5.

Now:
- Resume (every surface: dashboard, Telegram, the bot panel) leaves the
  goal's own halt in place while the goal is switched on, and says so.
- The goal counts the whole day, so a Resume or a restart (a fresh
  `SweepState`) cannot restart its count.
- The deliberate override is the goal's own setting: switched off, or raised
  above the day, and the next sweep lifts its halt.
- Another guard's halt is still lifted by Resume exactly as before.

Nothing here reaches a broker: MT5's figures are canned, the balance and
positions come from a fake bridge, and the halt is app_config rows.
"""
from __future__ import annotations

import asyncio

from backend.src.db import database as db
from backend.src.services.risk import daily_goal as dg
from backend.src.services.risk import governor as rg
from backend.src.services.risk import manual_pause
from backend.src.services.risk import trading_status


def _rs(value=10.0, enabled=1):
    return {"daily_goal_enabled": enabled, "daily_goal_mode": "usd",
            "daily_goal_value": value}


def _settings(**rs):
    db.update_risk_settings(rs)
    db._rs_cache = None
    return {**db.get_risk_settings(), **rs}


class _Bridge:
    def __init__(self, balance=1_000.0, positions=0):
        self.balance = balance
        self.positions = positions

    async def get_account(self):
        return {"balance": self.balance}

    async def get_positions(self):
        return [{"ticket": i} for i in range(self.positions)]

    async def get_deal_history(self, days=7):  # pragma: no cover - stubbed below
        raise AssertionError("the sweep reads deals through todays_realised")


def _mt5(monkeypatch, day_figure):
    """MT5's realised: the whole day's from the day's start, nothing since any
    later moment (a Resume). Asked from a Resume, the goal would see $0."""
    async def fake(engine, since_ts):
        return day_figure if since_ts <= dg._goal_day_start() else 0.0

    monkeypatch.setattr(dg._todays, "since", fake)


def _sweep(rs, bridge=None, now=1_000.0):
    """A fresh SweepState every time is what a restart is."""
    asyncio.run(dg.sweep(dg.SweepState(), bridge or _Bridge(), rs, now=now))


def _secure(monkeypatch, rs, figure=29.57):
    _mt5(monkeypatch, figure)
    _sweep(rs)
    assert rg.halt_reason().startswith("Daily goal secured"), rg.halt_reason()


# ── Resume ───────────────────────────────────────────────────────────────────

def test_resume_leaves_a_secured_goal_in_place(fresh_db, monkeypatch):
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    kept = manual_pause.resume()
    assert rg.is_trading_paused()
    assert rg.halt_reason().startswith("Daily goal secured")
    assert kept == manual_pause.GOAL_KEPT


def test_resume_leaves_a_goal_hold_in_place(fresh_db, monkeypatch):
    """Reached with trades open: the hold is the goal's too."""
    rs = _settings(**_rs())
    _mt5(monkeypatch, 29.57)
    _sweep(rs, _Bridge(positions=2))
    assert rg.halt_reason().startswith("Daily goal reached"), rg.halt_reason()
    manual_pause.resume()
    assert rg.is_trading_paused()


def test_resume_does_not_restart_the_goals_count(fresh_db, monkeypatch):
    """The owner's 9 October: after the Resume, the morning still counts."""
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    manual_pause.resume()
    # Even had the halt been cleared some other way, the next sweep must
    # count the whole day and secure it again.
    with db.db():
        db.set_app_config("trade_pause_until", "0")
        db.set_app_config("risk_halt_reason", "")
    _sweep(rs, now=2_000.0)
    assert rg.halt_reason().startswith("Daily goal secured"), rg.halt_reason()


def test_a_restart_finds_the_goal_still_secured(fresh_db, monkeypatch):
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    _sweep(rs, now=50_000.0)
    assert rg.is_trading_paused()
    assert rg.halt_reason().startswith("Daily goal secured")


def test_resume_still_lifts_another_guards_halt(fresh_db):
    _settings(**_rs())
    with db.db():
        db.set_app_config("trade_pause_until", str(rg.calendar_day_start_ts() + 86400))
        db.set_app_config("risk_halt_reason", "Give-back guard: handed back 50%")
    assert manual_pause.resume() is None
    assert not rg.is_trading_paused()


def test_resume_lifts_a_goal_halt_once_the_goal_is_switched_off(fresh_db, monkeypatch):
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    _settings(daily_goal_enabled=0)
    assert manual_pause.resume() is None
    assert not rg.is_trading_paused()


# ── the deliberate override ──────────────────────────────────────────────────

def test_switching_the_goal_off_lifts_its_halt(fresh_db, monkeypatch):
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    off = _settings(daily_goal_enabled=0)
    _sweep(off, now=2_000.0)
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_switching_it_off_never_lifts_another_guards_halt(fresh_db):
    off = _settings(**_rs(enabled=0))
    with db.db():
        db.set_app_config("trade_pause_until", str(rg.calendar_day_start_ts() + 86400))
        db.set_app_config("risk_halt_reason", "Give-back guard: handed back 50%")
    _sweep(off)
    assert rg.halt_reason().startswith("Give-back")


def test_switching_it_off_without_a_balance_lifts_nothing(fresh_db, monkeypatch):
    """Lifting re-runs the daily-loss limit, which needs the balance."""
    class _NoAcct(_Bridge):
        async def get_account(self):
            return None

    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    off = _settings(daily_goal_enabled=0)
    _sweep(off, _NoAcct(), now=2_000.0)
    assert rg.is_trading_paused()


def test_raising_the_goal_above_the_day_lifts_its_halt(fresh_db, monkeypatch):
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    _sweep(_settings(**_rs(value=50.0)), now=2_000.0)
    assert not rg.is_trading_paused(), rg.halt_reason()


# ── the badge says what lifts it ─────────────────────────────────────────────

def test_the_badge_offers_no_resume_for_a_secured_goal(fresh_db, monkeypatch):
    monkeypatch.setattr(trading_status, "_paired_vps_view", lambda: None)
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    b = trading_status.badge()
    assert b["state"] == "halted"
    assert b["can_resume"] is False
    assert "Risk settings" in b["detail"]
