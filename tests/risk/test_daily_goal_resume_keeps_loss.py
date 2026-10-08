"""A Resume restarts the daily goal's count of profit, never its debt.

Owner, 2026-10-07: the day was -$41.05 on MT5 by 09:22, trading was resumed,
and +$36.30 came after. The header read "$26.99 / -$4.75", yet trading stopped
on "Daily goal secured: +$36.30 today vs a goal of $25.34": the goal's window
started at the Resume and the morning's loss fell out of it. "It still needs
to cover the loss and achieve $26.99."

So the goal counts realised P&L since the window opened PLUS any loss made
earlier in the broker day; profit made before a Resume is still not counted
(resuming past a reached goal asks for a fresh goal on top). The % goal is of
the day's real opening balance: live balance minus the WHOLE day's realised.

Nothing here reaches a broker: MT5's figures are canned per window start, the
balance and positions come from a fake bridge, and the halt is app_config rows.
"""
import asyncio

from backend.src.db import database as db
from backend.src.services.risk import daily_goal as dg
from backend.src.services.risk import governor as rg


def _rs(mode="pct", value=4.0):
    return {"daily_goal_enabled": 1, "daily_goal_mode": mode,
            "daily_goal_value": value}


class _Bridge:
    def __init__(self, balance, positions=0):
        self.balance = balance
        self.positions = positions

    async def get_account(self):
        return {"balance": self.balance}

    async def get_positions(self):
        return [{"ticket": i} for i in range(self.positions)]

    async def get_deal_history(self, days=7):  # pragma: no cover - stubbed below
        raise AssertionError("the sweep reads deals through todays_realised")


def _mt5(monkeypatch, before_resume, since_resume):
    """MT5's realised from the day's start, and from the Resume."""
    day_start = dg._goal_day_start()

    async def fake(engine, since_ts):
        if since_ts <= day_start:
            return round(before_resume + since_resume, 2)
        return since_resume

    monkeypatch.setattr(dg._todays, "since", fake)


def _sweep(bridge, rs):
    asyncio.run(dg.sweep(dg.SweepState(), bridge, rs, now=1_000.0))


# Opening balance 674.75; 4% of it is $26.99, the header's goal.
OPEN = 674.75


def test_a_loss_before_resume_still_has_to_be_covered(fresh_db, monkeypatch):
    """The owner's day: -41.05, Resume, +36.30. Day at -4.75: not reached."""
    rg.rearm_risk_guards()
    _mt5(monkeypatch, -41.05, 36.30)
    _sweep(_Bridge(OPEN - 4.75), _rs("pct", 4.0))
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_the_halt_written_on_profit_since_resume_lifts(fresh_db, monkeypatch):
    """The halt standing in the owner's database lifts once flat."""
    rg.rearm_risk_guards()
    with db.db():
        db.set_app_config("trade_pause_until", str(rg.rg_day_start_ts() + 86400.0))
        db.set_app_config("risk_halt_reason",
                          "Daily goal secured: +$36.30 today vs a goal of $25.34 "
                          "(4% of the day's opening balance)")
    _mt5(monkeypatch, -41.05, 36.30)
    _sweep(_Bridge(OPEN - 4.75, positions=0), _rs("pct", 4.0))
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_covering_the_loss_and_the_goal_secures_it(fresh_db, monkeypatch):
    """-41.05, Resume, +68.04: the day is +26.99, the goal is $26.99."""
    rg.rearm_risk_guards()
    _mt5(monkeypatch, -41.05, 68.04)
    _sweep(_Bridge(OPEN + 26.99), _rs("pct", 4.0))
    reason = rg.halt_reason()
    assert reason.startswith("Daily goal secured"), reason
    assert "+$26.99 today vs a goal of $26.99" in reason


def test_one_cent_short_after_covering_the_loss_is_not_reached(fresh_db, monkeypatch):
    rg.rearm_risk_guards()
    _mt5(monkeypatch, -41.05, 68.03)
    _sweep(_Bridge(OPEN + 26.98), _rs("pct", 4.0))
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_usd_goal_counts_the_loss_too(fresh_db, monkeypatch):
    rg.rearm_risk_guards()
    _mt5(monkeypatch, -20.0, 25.0)
    _sweep(_Bridge(1_000.0), _rs("usd", 10.0))
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_profit_before_a_resume_is_not_counted(fresh_db, monkeypatch):
    """Resuming past a reached goal asks for a fresh one: unchanged."""
    rg.rearm_risk_guards()
    _mt5(monkeypatch, 30.0, 9.0)
    _sweep(_Bridge(1_000.0), _rs("usd", 10.0))
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_a_fresh_goal_after_resume_is_reached_on_its_own(fresh_db, monkeypatch):
    rg.rearm_risk_guards()
    _mt5(monkeypatch, 30.0, 10.0)
    _sweep(_Bridge(1_000.0), _rs("usd", 10.0))
    assert rg.halt_reason().startswith("Daily goal secured"), rg.halt_reason()
