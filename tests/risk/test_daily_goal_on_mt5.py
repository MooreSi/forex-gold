"""The daily goal halts on MT5's realised figure, not the local table.

Owner, 2026-10-07: the day stood at -$41.05 on MT5 (the header's "Today's
Goal" and the Calendar agreed) and the goal was $26.99, yet trading stopped on
"Daily goal secured: +$39.24 today vs a goal of $23.77". The halt summed the
local `vantage_simulated_trades` rows, which held only the day's four small
winners; the losing closes reached MT5 by another route and never had a row.
The goal is profit above the day's starting capital, and only MT5 knows that.

Nothing here reaches a broker: deal history is a canned table, the balance and
positions come from a fake bridge, and the halt is two app_config rows.
"""
import asyncio
import time

from backend.src.db import database as db
from backend.src.services.analytics import formatting as fmt
from backend.src.services.analytics import todays_realised as todays
from backend.src.services.risk import daily_goal as dg
from backend.src.services.risk import governor as rg

from tests.core.test_giveback_guard import _closes


def _rs(mode="pct", value=4.0):
    return {"daily_goal_enabled": 1, "daily_goal_mode": mode,
            "daily_goal_value": value}


class _MT5Bridge:
    """Balance, open positions, and a deal history the sweep may ask for."""

    def __init__(self, balance, positions=0):
        self.balance = balance
        self.positions = positions

    async def get_account(self):
        return {"balance": self.balance}

    async def get_positions(self):
        return [{"ticket": i} for i in range(self.positions)]

    async def get_deal_history(self, days=7):  # pragma: no cover - stubbed below
        raise AssertionError("the sweep reads deals through todays_realised")


def _mt5_says(monkeypatch, value):
    calls = []

    async def fake(engine, since_ts):
        calls.append(since_ts)
        return value

    monkeypatch.setattr(dg._todays, "since", fake)
    return calls


def _sweep(bridge, rs, now=1_000.0):
    asyncio.run(dg.sweep(dg.SweepState(), bridge, rs, now=now))


# ── the owner's day ──────────────────────────────────────────────────────────

def test_local_winners_do_not_secure_a_day_mt5_has_in_loss(fresh_db, monkeypatch):
    """The 2026-10-07 numbers: local +$39.24, MT5 -$41.05, balance $633.45."""
    _closes([2.24, 3.02, 3.73, 30.25])
    _mt5_says(monkeypatch, -41.05)
    _sweep(_MT5Bridge(633.45, positions=0), _rs("pct", 4.0))
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_the_goal_is_priced_from_the_mt5_opening_balance(fresh_db, monkeypatch):
    """Opening balance = 633.45 + 41.05 = 674.50; 4% of it is $26.98. A day
    at +$26.98 on MT5 has reached it, whatever the local table says."""
    _closes([-500])
    _mt5_says(monkeypatch, 26.98)
    _sweep(_MT5Bridge(674.50 + 26.98, positions=0), _rs("pct", 4.0))
    assert rg.halt_reason().startswith("Daily goal secured")
    assert "+$26.98" in rg.halt_reason()


def test_just_short_on_mt5_is_not_reached(fresh_db, monkeypatch):
    """Negative control for the test above."""
    _closes([500])
    _mt5_says(monkeypatch, 26.96)
    _sweep(_MT5Bridge(674.50 + 26.96, positions=0), _rs("pct", 4.0))
    assert not rg.is_trading_paused()


def test_mt5_is_asked_from_the_goals_own_window(fresh_db, monkeypatch):
    """A Resume restarts the window; MT5's figure must restart with it."""
    calls = _mt5_says(monkeypatch, 0.0)
    rg.rearm_risk_guards()
    _sweep(_MT5Bridge(1_000.0), _rs("usd", 10.0))
    assert calls and calls[0] == dg._window_start()
    assert calls[0] > rg.rg_day_start_ts()


def test_no_answer_from_mt5_decides_nothing(fresh_db, monkeypatch):
    """No figure, no halt -- and the local table is not a stand-in for it."""
    _closes([500])
    _mt5_says(monkeypatch, None)
    _sweep(_MT5Bridge(10_000.0), _rs("usd", 100.0))
    assert not rg.is_trading_paused()


def test_a_secured_halt_mt5_disagrees_with_lifts_when_flat(fresh_db, monkeypatch):
    """The halt already written on the local figure must not stand for the
    rest of the day once MT5 shows the day under the goal."""
    with db.db():
        db.set_app_config("trade_pause_until", str(rg.rg_day_start_ts() + 86400.0))
        db.set_app_config("risk_halt_reason",
                          "Daily goal secured: +$39.24 today vs a goal of $23.77")
    _mt5_says(monkeypatch, -41.05)
    _sweep(_MT5Bridge(633.45, positions=0), _rs("pct", 4.0))
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_a_secured_halt_mt5_agrees_with_stands(fresh_db, monkeypatch):
    """Negative control: a real secured day stays secured."""
    with db.db():
        db.set_app_config("trade_pause_until", str(rg.rg_day_start_ts() + 86400.0))
        db.set_app_config("risk_halt_reason",
                          "Daily goal secured: +$30.00 today vs a goal of $26.98")
    _mt5_says(monkeypatch, 30.0)
    _sweep(_MT5Bridge(674.50 + 30.0, positions=0), _rs("pct", 4.0))
    assert rg.halt_reason().startswith("Daily goal secured")


def test_a_secured_halt_is_not_lifted_without_an_mt5_figure(fresh_db, monkeypatch):
    with db.db():
        db.set_app_config("trade_pause_until", str(rg.rg_day_start_ts() + 86400.0))
        db.set_app_config("risk_halt_reason",
                          "Daily goal secured: +$39.24 today vs a goal of $23.77")
    _mt5_says(monkeypatch, None)
    _sweep(_MT5Bridge(633.45, positions=0), _rs("pct", 4.0))
    assert rg.is_trading_paused()


# ── todays_realised.realised_since: the window over MT5's rows ───────────────

def _row(epoch, pnl):
    """A closed-trade row; MT5 stamps closes in broker time (UTC+3) as-if-UTC."""
    return {"close_ts": epoch + fmt.BROKER_OFFSET, "pnl": pnl}


def test_realised_since_sums_closes_at_or_after_the_window():
    t0 = time.time() - 3_600
    table = {"rows": [_row(t0 - 1, 100.0), _row(t0, -41.05), _row(t0 + 60, 10.0)]}
    assert todays.realised_since(table, t0) == -31.05


def test_realised_since_is_none_when_mt5_could_not_answer():
    assert todays.realised_since({"rows": [], "error": "bridge down"}, 0.0) is None
    assert todays.realised_since({}, 0.0) is None


def test_realised_since_with_no_closes_is_zero():
    assert todays.realised_since({"rows": [], "error": None}, 0.0) == 0.0
