"""The daily profit goal: stop new entries for the day once it is secured.

Owner, 2026-09-29: "a slider for either % or $ to secure for the day ... once
that is hit it will stop trading ... compound profit over days/weeks". Answers
the same day: realised P&L only, new entries only (open trades run on), and
every source of trades.

`%` is of the day's OPENING balance (balance now minus today's realised P&L),
so the same percentage is a bigger dollar goal as the account grows. That is
the compounding.

Nothing here reaches a broker: the balance comes from a fake bridge and the
halt is two app_config rows.
"""
import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.services.risk import daily_goal as dg
from backend.src.services.risk import governor as rg

from tests.core.test_giveback_guard import _closes


def _rs(mode="usd", value=100.0, enabled=1):
    return {"daily_goal_enabled": enabled, "daily_goal_mode": mode,
            "daily_goal_value": value}


class _Bridge:
    def __init__(self, balance):
        self.balance = balance
        self.calls = 0

    async def get_account(self):
        self.calls += 1
        return {"balance": self.balance}


# ── when it is reached ───────────────────────────────────────────────────────

def test_a_dollar_goal_is_reached_by_realised_profit(fresh_db):
    _closes([60, 45])
    assert dg.check_daily_goal(_rs("usd", 100.0), balance=None) is not None


def test_a_dollar_goal_just_short_is_not_reached(fresh_db):
    """Negative control for the test above."""
    _closes([60, 39.99])
    assert dg.check_daily_goal(_rs("usd", 100.0), balance=None) is None


def test_a_percent_goal_is_measured_from_the_days_opening_balance(fresh_db):
    """Balance is 10,200 after +200 today, so the day opened at 10,000 and 2%
    of THAT is 200 -- reached. Measured against the current balance (10,200)
    2% would be 204, and it would not be."""
    _closes([200])
    assert dg.check_daily_goal(_rs("pct", 2.0), balance=10_200.0) is not None


def test_a_percent_goal_just_short_is_not_reached(fresh_db):
    _closes([199])
    assert dg.check_daily_goal(_rs("pct", 2.0), balance=10_199.0) is None


def test_the_percent_goal_compounds_with_the_balance(fresh_db):
    """The same 2% asks for more once the account is bigger."""
    _closes([250])
    assert dg.check_daily_goal(_rs("pct", 2.0), balance=10_250.0) is not None
    assert dg.check_daily_goal(_rs("pct", 2.0), balance=20_250.0) is None


def test_a_percent_goal_with_no_balance_is_not_judged(fresh_db):
    """No balance, no percentage. Refusing to guess is safer than halting on a
    made-up number or never halting on one."""
    _closes([1_000])
    assert dg.check_daily_goal(_rs("pct", 1.0), balance=None) is None


def test_the_reason_names_the_goal_and_the_day(fresh_db):
    _closes([150])
    reason = dg.check_daily_goal(_rs("usd", 100.0), balance=None)
    assert "Daily goal" in reason and "150.00" in reason and "100.00" in reason


# ── when it must stay out of the way ─────────────────────────────────────────

def test_off_by_default(fresh_db):
    _closes([10_000])
    rs = db.get_risk_settings()
    assert int(rs["daily_goal_enabled"]) == 0
    assert dg.check_daily_goal(rs, balance=50_000.0) is None


def test_the_defaults_are_one_percent(fresh_db):
    rs = db.get_risk_settings()
    assert rs["daily_goal_mode"] == "pct"
    assert float(rs["daily_goal_value"]) == pytest.approx(1.0)


@pytest.mark.parametrize("value", [0, -5])
def test_a_zero_or_negative_goal_never_halts(fresh_db, value):
    _closes([500])
    assert dg.check_daily_goal(_rs("usd", value), balance=None) is None


def test_yesterdays_profit_does_not_count(fresh_db):
    _closes([500], day_start=rg.rg_day_start_ts() - 86400)
    _closes([20])
    assert dg.check_daily_goal(_rs("usd", 100.0), balance=None) is None


def test_a_resume_restarts_the_window(fresh_db):
    """Otherwise Resume is undone on the very next check: the day is already
    past the goal. After a resume, only profit made since counts."""
    _closes([150])
    assert dg.check_daily_goal(_rs("usd", 100.0), balance=None) is not None
    rg.rearm_risk_guards()
    assert dg.check_daily_goal(_rs("usd", 100.0), balance=None) is None
    _closes([120], at=time.time() + 5)
    assert dg.check_daily_goal(_rs("usd", 100.0), balance=None) is not None


# ── applying it ──────────────────────────────────────────────────────────────

def test_applying_writes_the_pause_every_daily_halt_uses(fresh_db):
    _closes([150])
    assert dg.apply_daily_goal(_rs("usd", 100.0), balance=None) is True
    assert rg.is_trading_paused()
    until = float(db.get_app_config("trade_pause_until"))
    assert until == pytest.approx(dg._goal_day_start() + 86400.0)
    assert "Daily goal" in rg.halt_reason()


def test_applying_below_the_goal_writes_nothing(fresh_db):
    _closes([50])
    assert dg.apply_daily_goal(_rs("usd", 100.0), balance=None) is False
    assert not rg.is_trading_paused()


def test_applying_does_not_overwrite_an_existing_halt(fresh_db):
    """The halt already in force is the one the operator must see."""
    _closes([150])
    with db.db():
        db.set_app_config("trade_pause_until", str(time.time() + 3600))
        db.set_app_config("risk_halt_reason", "Daily loss limit hit")
    assert dg.apply_daily_goal(_rs("usd", 100.0), balance=None) is False
    assert rg.halt_reason() == "Daily loss limit hit"


# ── the monitor-cycle sweep ──────────────────────────────────────────────────

def test_the_sweep_reads_the_balance_only_for_a_percent_goal(fresh_db):
    _closes([150])
    bridge = _Bridge(10_150.0)
    state = dg.SweepState()
    asyncio.run(dg.sweep(state, bridge, _rs("usd", 100.0), now=1_000.0))
    assert bridge.calls == 0 and rg.is_trading_paused()


def test_the_sweep_halts_on_a_percent_goal(fresh_db):
    _closes([150])
    bridge = _Bridge(10_150.0)
    asyncio.run(dg.sweep(dg.SweepState(), bridge, _rs("pct", 1.0), now=1_000.0))
    assert bridge.calls == 1 and rg.is_trading_paused()


def test_the_sweep_is_throttled(fresh_db):
    _closes([10])
    bridge = _Bridge(10_010.0)
    state = dg.SweepState()
    asyncio.run(dg.sweep(state, bridge, _rs("pct", 1.0), now=1_000.0))
    asyncio.run(dg.sweep(state, bridge, _rs("pct", 1.0), now=1_002.0))
    assert bridge.calls == 1
    asyncio.run(dg.sweep(state, bridge, _rs("pct", 1.0), now=1_000.0 + dg.SWEEP_EVERY_S))
    assert bridge.calls == 2


def test_the_sweep_does_nothing_when_switched_off(fresh_db):
    _closes([10_000])
    bridge = _Bridge(10_000.0)
    asyncio.run(dg.sweep(dg.SweepState(), bridge, _rs(enabled=0), now=1_000.0))
    assert bridge.calls == 0 and not rg.is_trading_paused()


def test_the_sweep_never_raises(fresh_db):
    class _Broken:
        async def get_account(self):
            raise RuntimeError("bridge down")
    _closes([10])
    asyncio.run(dg.sweep(dg.SweepState(), _Broken(), _rs("pct", 1.0), now=1_000.0))
    assert not rg.is_trading_paused()


# ── the monitor cycle drives it ──────────────────────────────────────────────

def test_the_monitor_cycle_sweeps_the_goal_with_nothing_open(fresh_db, monkeypatch):
    """The usual moment the goal is reached is the close of the LAST open
    trade, so the sweep must run when nothing is open."""
    import types
    from backend.src.services.positions import monitor_cycle as mc

    async def _none(*a, **k):
        return None
    for attr in ("_check_equity_protect_impl", "_check_basket_harvest_impl",
                 "_reconcile_orphaned_trades_impl",
                 "_repair_template_placeholders_impl",
                 "_profit_sweep_impl", "_run_dpm_calibration_impl",
                 "_ime_timeout_watchdog_impl", "_revalidate_pending_impl",
                 "_try_activate_pending_signals_impl"):
        if hasattr(mc, attr):
            monkeypatch.setattr(mc, attr, _none)
    seen = []

    async def _sweep(state, bridge, rs, now=None):
        seen.append((state, bridge, rs.get("daily_goal_enabled")))
    monkeypatch.setattr(dg, "sweep", _sweep)

    async def get_tick():
        return types.SimpleNamespace(bid=2400.0, ask=2400.2)
    bridge = object()
    ctx = mc.MonitorCtx(
        bridge=bridge, cfg={}, tp_trigger_cache={}, dpm_cache={},
        scale_out_last_fail={}, pending_activation_retry_after={},
        get_dpm_candles=lambda: [], set_dpm_candles=lambda v: None,
        get_tick=get_tick, get_open_trades=lambda: [],
        get_candles=_none, is_trading_paused=lambda: False,
        background_open_commentary=_none, close_full_after_tps=_none,
        make_close_trade_ctx=lambda: object(),
        sync_closed_mt5_positions=_none, close_trade=_none,
    )
    asyncio.run(mc.run_monitor_cycle(ctx))
    assert len(seen) == 1
    state, got_bridge, _ = seen[0]
    assert got_bridge is bridge
    assert state is ctx.state.daily_goal, "the throttle must outlive one cycle"


# ── the dashboard's "Today's Goal" figure ────────────────────────────────────

def test_progress_is_none_when_the_goal_is_off(fresh_db):
    _closes([60])
    assert dg.progress(_rs("usd", 100.0, enabled=0), balance=None) is None


def test_progress_reports_a_dollar_goal_and_todays_realised(fresh_db):
    _closes([60, -10])
    assert dg.progress(_rs("usd", 100.0), balance=None) == {
        "goal_usd": 100.0, "achieved_usd": 50.0}


def test_progress_turns_a_percent_goal_into_dollars(fresh_db):
    _closes([200])
    got = dg.progress(_rs("pct", 2.0), balance=10_200.0)
    assert got == {"goal_usd": 200.0, "achieved_usd": 200.0}


def test_progress_is_none_when_a_percent_goal_cannot_be_priced(fresh_db):
    """No balance: showing a guessed dollar goal would be worse than none."""
    _closes([200])
    assert dg.progress(_rs("pct", 2.0), balance=None) is None


def test_progress_uses_the_broker_figure_when_given_not_the_local_table(fresh_db):
    """The local table is empty here, as on the day the Calendar said +$2.51
    and the goal said $0.00. A supplied MT5 figure wins."""
    got = dg.progress(_rs("usd", 33.03), balance=None, realised=2.51)
    assert got == {"goal_usd": 33.03, "achieved_usd": 2.51}


def test_a_percent_goal_opens_from_the_broker_realised(fresh_db):
    got = dg.progress(_rs("pct", 2.0), balance=10_200.0, realised=200.0)
    assert got == {"goal_usd": 200.0, "achieved_usd": 200.0}


# ── header_progress: the header poll's version, on MT5's figure ──────────────

def _stub_realised(monkeypatch, rs, value=None, boom=False):
    calls = []
    monkeypatch.setattr(dg._risk, "get", lambda: rs)

    async def fake(engine, day):
        calls.append(day)
        if boom:
            raise RuntimeError("bridge down")
        return value

    monkeypatch.setattr(dg._todays, "for_today", fake)
    return calls


def test_header_progress_does_not_ask_mt5_when_the_goal_is_off(monkeypatch):
    calls = _stub_realised(monkeypatch, _rs("usd", 100.0, enabled=0), 5.0)
    got = asyncio.run(dg.header_progress(None, None, "d"))
    assert got is None
    assert calls == []


def test_header_progress_uses_the_mt5_figure(monkeypatch):
    _stub_realised(monkeypatch, _rs("usd", 33.03), 2.51)
    got = asyncio.run(dg.header_progress(None, None, "d"))
    assert got == {"goal_usd": 33.03, "achieved_usd": 2.51}


def test_header_progress_is_none_when_mt5_cannot_answer(monkeypatch):
    _stub_realised(monkeypatch, _rs("usd", 33.03), None)
    assert asyncio.run(dg.header_progress(None, None, "d")) is None


def test_header_progress_never_raises(monkeypatch):
    _stub_realised(monkeypatch, _rs("usd", 33.03), boom=True)
    assert asyncio.run(dg.header_progress(None, None, "d")) is None


# ── open trades: the goal is secured only once they have all closed ──────────
#
# Owner, 2026-09-30: the VPS halted on "+$34.31 vs $32.33" with trades still
# open; they closed at a loss and the day finished under the goal, halted.
# Reached-with-trades-open is now a HOLD (no new entries) that becomes the
# day's halt only when flat and still at the goal, and lifts when the closes
# took the day back under it. Nothing here reaches a broker.

class _PosBridge(_Bridge):
    def __init__(self, balance, positions):
        super().__init__(balance)
        self.positions = positions

    async def get_positions(self):
        return None if self.positions is None else [{"ticket": i} for i in range(self.positions)]


def _sweep(bridge, rs, now=1_000.0):
    asyncio.run(dg.sweep(dg.SweepState(), bridge, rs, now=now))


def test_reached_with_trades_open_holds_new_entries_but_is_not_secured(fresh_db):
    _closes([150])
    _sweep(_PosBridge(10_150.0, 2), _rs("usd", 100.0))
    assert rg.is_trading_paused(), "no new entries while the goal is only on paper"
    assert "waiting for 2 open trades" in rg.halt_reason()
    assert not rg.halt_reason().startswith("Daily goal secured")


def test_reached_with_nothing_open_is_secured_for_the_day(fresh_db):
    _closes([150])
    _sweep(_PosBridge(10_150.0, 0), _rs("usd", 100.0))
    assert rg.halt_reason().startswith("Daily goal secured")
    assert float(db.get_app_config("trade_pause_until")) == pytest.approx(
        dg._goal_day_start() + 86400.0)


def test_the_hold_becomes_the_days_halt_when_the_last_trade_closes_above(fresh_db):
    _closes([150])
    _sweep(_PosBridge(10_150.0, 1), _rs("usd", 100.0))
    _closes([-20], at=time.time() + 1)
    _sweep(_PosBridge(10_130.0, 0), _rs("usd", 100.0), now=2_000.0)
    assert rg.is_trading_paused()
    assert rg.halt_reason().startswith("Daily goal secured")


def test_the_hold_lifts_when_the_closes_took_the_day_under_the_goal(fresh_db):
    """The owner's case: reached on paper, lost it back, keeps trading."""
    _closes([150])
    _sweep(_PosBridge(10_150.0, 1), _rs("usd", 100.0))
    _closes([-80], at=time.time() + 1)
    _sweep(_PosBridge(10_070.0, 0), _rs("usd", 100.0), now=2_000.0)
    assert not rg.is_trading_paused()


def test_the_hold_stays_while_trades_are_still_open_under_the_goal(fresh_db):
    """Under the goal again, but not flat yet: wait for the rest."""
    _closes([150])
    _sweep(_PosBridge(10_150.0, 2), _rs("usd", 100.0))
    _closes([-80], at=time.time() + 1)
    _sweep(_PosBridge(10_070.0, 1), _rs("usd", 100.0), now=2_000.0)
    assert rg.is_trading_paused()


def test_an_unknown_position_count_never_secures_or_lifts(fresh_db):
    """MT5 could not say what is open: hold, do not guess either way."""
    _closes([150])
    _sweep(_PosBridge(10_150.0, None), _rs("usd", 100.0))
    assert rg.is_trading_paused()
    assert not rg.halt_reason().startswith("Daily goal secured")
    _closes([-80], at=time.time() + 1)
    _sweep(_PosBridge(10_070.0, None), _rs("usd", 100.0), now=2_000.0)
    assert rg.is_trading_paused()


def test_lifting_the_hold_runs_the_daily_loss_limit_it_was_masking(fresh_db):
    """While the hold is in force the close-time guards see "already paused"
    and write nothing. Lifting must not skip a limit the closes breached."""
    _closes([150])
    _sweep(_PosBridge(10_150.0, 1), _rs("usd", 100.0))
    _closes([-500], at=time.time() + 1)
    rs = {**db.get_risk_settings(), **_rs("usd", 100.0), "max_daily_loss_pct": 3.0}
    _sweep(_PosBridge(9_650.0, 0), rs, now=2_000.0)
    assert rg.is_trading_paused()
    assert rg.halt_reason().startswith("Daily loss")


def test_lifting_needs_the_balance(fresh_db):
    """No balance, no daily-loss check, so no lift: fail closed."""
    class _NoAcct(_PosBridge):
        async def get_account(self):
            return None
    _closes([150])
    _sweep(_PosBridge(10_150.0, 1), _rs("usd", 100.0))
    _closes([-80], at=time.time() + 1)
    _sweep(_NoAcct(0.0, 0), _rs("usd", 100.0), now=2_000.0)
    assert rg.is_trading_paused()


def test_a_hold_never_lifts_another_guards_halt(fresh_db):
    _closes([150])
    _sweep(_PosBridge(10_150.0, 1), _rs("usd", 100.0))
    with db.db():
        db.set_app_config("risk_halt_reason", "Give-back guard: handed back 50%")
    _closes([-80], at=time.time() + 1)
    _sweep(_PosBridge(10_070.0, 0), _rs("usd", 100.0), now=2_000.0)
    assert rg.is_trading_paused()
    assert rg.halt_reason().startswith("Give-back")


def test_a_manual_resume_during_the_hold_is_respected(fresh_db):
    from backend.src.services.risk import manual_pause
    _closes([150])
    _sweep(_PosBridge(10_150.0, 1), _rs("usd", 100.0))
    manual_pause.resume()
    _sweep(_PosBridge(10_150.0, 1), _rs("usd", 100.0), now=2_000.0)
    assert not rg.is_trading_paused(), "the window restarted at the Resume"
