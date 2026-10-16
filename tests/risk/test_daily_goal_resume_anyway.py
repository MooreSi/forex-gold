""""Resume anyway (ignore today's goal)": the deliberate override, from the
status dialog (owner, 2026-10-16).

The 2026-10-10 rule stands: plain Resume leaves a reached goal's halt in
place (tests/risk/test_daily_goal_survives_resume.py). This is a second,
explicitly worded button beside it that lifts the goal's halt and keeps the
goal from halting again for the rest of the trading-clock day only. The next
day the goal counts and halts as before.

Also: while overridden the goal no longer "stands", so the breakeven sweep
(`positions/goal_breakeven`) does not pull the stops of the trades taken after
the override to breakeven. Stops it already moved are not loosened.

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
    async def fake(engine, since_ts):
        return day_figure

    monkeypatch.setattr(dg._todays, "since", fake)


def _sweep(rs, bridge=None, now=1_000.0):
    asyncio.run(dg.sweep(dg.SweepState(), bridge or _Bridge(), rs, now=now))


def _secure(monkeypatch, rs, figure=29.57):
    _mt5(monkeypatch, figure)
    _sweep(rs)
    assert rg.halt_reason().startswith("Daily goal secured"), rg.halt_reason()


# ── the override lifts the goal's halt ───────────────────────────────────────

def test_resume_past_goal_lifts_a_secured_goal(fresh_db, monkeypatch):
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    assert manual_pause.resume_past_goal() is None
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_resume_past_goal_lifts_a_goal_hold(fresh_db, monkeypatch):
    rs = _settings(**_rs())
    _mt5(monkeypatch, 29.57)
    _sweep(rs, _Bridge(positions=2))
    assert rg.halt_reason().startswith("Daily goal reached"), rg.halt_reason()
    manual_pause.resume_past_goal()
    assert not rg.is_trading_paused()


def test_the_next_sweep_does_not_halt_again_today(fresh_db, monkeypatch):
    """Without the override flag the sweep would re-secure within 5 s."""
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    manual_pause.resume_past_goal()
    _mt5(monkeypatch, 60.0)
    _sweep(rs, now=2_000.0)
    _sweep(rs, now=50_000.0)
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_a_hold_is_not_rewritten_either(fresh_db, monkeypatch):
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    manual_pause.resume_past_goal()
    _sweep(rs, _Bridge(positions=3), now=2_000.0)
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_the_override_ends_with_the_day(fresh_db, monkeypatch):
    """Tomorrow the goal is back: the override is written to end at the
    goal's day end, and once past it the sweep halts again."""
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    manual_pause.resume_past_goal()
    day_end = rg.calendar_day_start_ts() + 86400.0
    assert float(db.get_app_config(dg.OVERRIDE_KEY)) == day_end
    # Move "today" to tomorrow: the stored override is now in the past.
    monkeypatch.setattr(rg, "calendar_day_start_ts", lambda: day_end)
    monkeypatch.setattr(dg.time, "time", lambda: day_end + 60.0)
    _sweep(rs, now=day_end + 60.0)
    assert rg.halt_reason().startswith("Daily goal secured"), rg.halt_reason()


def test_plain_resume_still_keeps_the_goal(fresh_db, monkeypatch):
    """The 2026-10-10 rule is not weakened by the new button existing."""
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    assert manual_pause.resume() == manual_pause.GOAL_KEPT
    assert rg.is_trading_paused()
    assert db.get_app_config(dg.OVERRIDE_KEY) in (None, "", "0")


def test_resume_past_goal_on_another_guards_halt_is_a_plain_resume(fresh_db):
    """No goal halt in force: lifts it as Resume does, and sets no override,
    so a goal reached later today still halts."""
    _settings(**_rs())
    with db.db():
        db.set_app_config("trade_pause_until", str(rg.calendar_day_start_ts() + 86400))
        db.set_app_config("risk_halt_reason", "Give-back guard: handed back 50%")
    assert manual_pause.resume_past_goal() is None
    assert not rg.is_trading_paused()
    assert db.get_app_config(dg.OVERRIDE_KEY) in (None, "", "0")


def test_the_goal_no_longer_stands_once_overridden(fresh_db, monkeypatch):
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    assert dg.goal_standing(rs, 1_000.0, 29.57, 29.57)
    manual_pause.resume_past_goal()
    assert not dg.goal_standing(rs, 1_000.0, 29.57, 29.57)


# ── the badge and resume_all ─────────────────────────────────────────────────

def test_the_badge_offers_resume_past_goal_only_for_the_goals_halt(fresh_db, monkeypatch):
    monkeypatch.setattr(trading_status, "_paired_vps_view", lambda: None)
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    b = trading_status.badge()
    assert b["can_resume"] is False
    assert b["can_resume_past_goal"] is True

    manual_pause.resume_past_goal()
    assert trading_status.badge().get("can_resume_past_goal") is not True


def test_the_badge_does_not_offer_it_for_another_guards_halt(fresh_db, monkeypatch):
    monkeypatch.setattr(trading_status, "_paired_vps_view", lambda: None)
    _settings(**_rs())
    with db.db():
        db.set_app_config("trade_pause_until", str(rg.calendar_day_start_ts() + 86400))
        db.set_app_config("risk_halt_reason", "Give-back guard: handed back 50%")
    b = trading_status.badge()
    assert b["can_resume"] is True
    assert b.get("can_resume_past_goal") is not True


def test_resume_all_past_goal_clears_the_goal(fresh_db, monkeypatch):
    monkeypatch.setattr(trading_status, "_paired_vps_view", lambda: None)
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    out = trading_status.resume_all(past_goal=True)
    assert "manual_pause" in out["cleared"]
    assert "kept" not in out
    assert not rg.is_trading_paused()


def test_resume_all_without_the_flag_keeps_the_goal(fresh_db, monkeypatch):
    monkeypatch.setattr(trading_status, "_paired_vps_view", lambda: None)
    rs = _settings(**_rs())
    _secure(monkeypatch, rs)
    out = trading_status.resume_all()
    assert out["kept"] == manual_pause.GOAL_KEPT
    assert rg.is_trading_paused()
