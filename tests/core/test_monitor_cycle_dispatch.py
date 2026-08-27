"""One pass of the position-monitor cycle.

`monitor_cycle.py` was 68.7% covered, and everything missing sat in
`run_monitor_cycle` -- the strategy dispatch table and the exception handlers
around each sub-check.

Those handlers are the point. This cycle is what watches every open position:
stop-loss reconciliation, profit-close targets, EA handoff, then a per-strategy
handler. If any one sub-check could take the pass down, every position after it
in the list goes unwatched until the next tick -- so "a failure here costs one
check, never the cycle" is a safety property, not tidiness.

Nothing reaches a broker. Every collaborator arrives on `MonitorCtx`, the
strategy handlers are replaced with recorders, and the module-level `_impl`
functions the cycle calls directly are patched out.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.src.db import database as db
from backend.src.services.positions import monitor_cycle as mc
from backend.src.utils.models import (
    STRATEGY_BE_RUNNER,
    STRATEGY_CONSERVATIVE_TRIAL,
    STRATEGY_NO_SL_SCALE,
    STRATEGY_ORB_FIXED,
    STRATEGY_SCALE_OUT,
    STRATEGY_TRAIL_STOP,
)


@pytest.fixture
def quiet(monkeypatch):
    """Neutralise everything the cycle calls that is not under test.

    Returns the recorder every strategy handler writes into.
    """
    seen = []

    def _rec(name):
        async def _fn(trade, tick, *a, **k):
            seen.append((name, trade.get("trade_id")))
        return _fn

    async def _false(*a, **k):
        return False

    async def _none(*a, **k):
        return None

    for attr, name in (
        ("_handle_orb_fixed_impl", "orb"),
        ("_handle_dynamic_position_management_impl", "dpm"),
        ("_handle_be_runner_impl", "be_runner"),
        ("_handle_trail_stop_impl", "trail"),
        ("_handle_no_sl_scale_impl", "no_sl_scale"),
        ("_handle_conservative_trial_impl", "conservative"),
        ("_handle_scale_out_impl", "scale_out"),
    ):
        if hasattr(mc, attr):
            monkeypatch.setattr(mc, attr, _rec(name))

    # Sub-checks that would otherwise short-circuit the dispatch.
    monkeypatch.setattr(mc, "_check_sl_impl", lambda t, tick: None)
    monkeypatch.setattr(mc, "_check_profit_close_target_impl", _false)
    monkeypatch.setattr(mc, "_reclaim_ea_managed_trade_impl", _false)
    for attr in ("_check_equity_protect_impl", "_check_basket_harvest_impl",
                 "_reconcile_orphaned_trades_impl", "_repair_template_placeholders_impl",
                 "_profit_sweep_impl", "_run_dpm_calibration_impl",
                 "_check_pending_signals_impl", "_ime_timeout_watchdog_impl",
                 "_revalidate_pending_impl", "_reconcile_sl_hit_impl"):
        if hasattr(mc, attr):
            monkeypatch.setattr(mc, attr, _none)
    return seen


def _trade(tid="t1", strategy=STRATEGY_SCALE_OUT, managed_by=None):
    return {"trade_id": tid, "strategy": strategy, "managed_by": managed_by,
            "direction": "BUY", "entry_price": 2400.0, "lot_size": 0.1,
            "remaining_lots": 0.1, "mt5_ticket": 111}


def _ctx(trades, *, tick=True, paused=False):
    async def get_tick():
        return types.SimpleNamespace(bid=2400.0, ask=2400.2) if tick else None

    async def _noop(*a, **k):
        return None

    async def get_candles(*a, **k):
        return []

    return mc.MonitorCtx(
        bridge=object(), cfg={}, tp_trigger_cache={}, dpm_cache={},
        scale_out_last_fail={}, pending_activation_retry_after={},
        get_dpm_candles=lambda: [], set_dpm_candles=lambda v: None,
        get_tick=get_tick, get_open_trades=lambda: trades,
        get_candles=get_candles,
        is_trading_paused=lambda: paused,
        background_open_commentary=_noop, close_full_after_tps=_noop,
        make_close_trade_ctx=lambda: object(),
        sync_closed_mt5_positions=_noop,
        close_trade=_noop,
    )


# ── Strategy dispatch ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("strategy,expected", [
    (STRATEGY_ORB_FIXED, "orb"),
    (STRATEGY_BE_RUNNER, "be_runner"),
    (STRATEGY_TRAIL_STOP, "trail"),
    (STRATEGY_NO_SL_SCALE, "no_sl_scale"),
    (STRATEGY_CONSERVATIVE_TRIAL, "conservative"),
])
def test_each_strategy_reaches_its_own_handler(fresh_db, quiet, strategy, expected):
    db.update_risk_settings({"dpm_enabled": 0})

    asyncio.run(mc.run_monitor_cycle(_ctx([_trade(strategy=strategy)])))

    assert quiet == [(expected, "t1")]


def test_dpm_takes_over_every_strategy_except_orb(fresh_db, quiet):
    """DPM's global toggle sweeps everything up -- except ORB/IVB, whose whole
    point is "exactly the setup the report computed, nothing recalculated"."""
    db.update_risk_settings({"dpm_enabled": 1})

    asyncio.run(mc.run_monitor_cycle(_ctx([_trade("orb", strategy=STRATEGY_ORB_FIXED),
                                           _trade("be", strategy=STRATEGY_BE_RUNNER)])))

    assert quiet == [("orb", "orb"), ("dpm", "be")]


def test_an_ea_managed_trade_skips_the_handlers_while_the_ea_holds_it(fresh_db, quiet, monkeypatch):
    """Its SL/TP ladder is being managed tick-by-tick inside MT5; running the
    Python handler too would double-manage it."""
    db.update_risk_settings({"dpm_enabled": 0})

    async def _reclaimed(trade, strategy):
        return True          # the EA still owns it
    monkeypatch.setattr(mc, "_reclaim_ea_managed_trade_impl", _reclaimed)

    asyncio.run(mc.run_monitor_cycle(_ctx([_trade("ea", managed_by="ea")])))

    assert quiet == []


def test_an_ea_trade_is_reclaimed_when_the_ea_has_gone_silent(fresh_db, quiet):
    """Python must always be able to take management back rather than trusting
    the EA blindly forever -- a crashed EA would otherwise leave the trade with
    nobody watching it."""
    db.update_risk_settings({"dpm_enabled": 0})

    asyncio.run(mc.run_monitor_cycle(
        _ctx([_trade("ea", strategy=STRATEGY_BE_RUNNER, managed_by="ea")])))

    assert quiet == [("be_runner", "ea")], "the handler must run once reclaimed"


# ── The cycle survives its own failures ───────────────────────────────────────

def test_no_tick_ends_the_pass_without_touching_any_trade(fresh_db, quiet):
    asyncio.run(mc.run_monitor_cycle(_ctx([_trade()], tick=False)))
    assert quiet == []


def test_one_exploding_handler_does_not_stop_the_other_trades(fresh_db, monkeypatch, quiet):
    """The safety property: every position after the bad one still gets checked."""
    db.update_risk_settings({"dpm_enabled": 0})
    handled = []

    async def _boom(trade, tick, *a, **k):
        if trade["trade_id"] == "bad":
            raise RuntimeError("handler exploded")
        handled.append(trade["trade_id"])

    monkeypatch.setattr(mc, "_handle_be_runner_impl", _boom)

    asyncio.run(mc.run_monitor_cycle(_ctx([
        _trade("bad", strategy=STRATEGY_BE_RUNNER),
        _trade("good", strategy=STRATEGY_BE_RUNNER)])))

    assert handled == ["good"]


@pytest.mark.parametrize("failing", [
    "_check_equity_protect_impl",
    "_check_basket_harvest_impl",
    "_repair_template_placeholders_impl",
    "_profit_sweep_impl",
])
def test_a_failing_sub_check_costs_that_check_and_not_the_cycle(fresh_db, quiet, monkeypatch, failing):
    """Each periodic sweep is wrapped on purpose. Equity Protect blowing up must
    not stop the per-trade handlers from running."""
    if not hasattr(mc, failing):
        pytest.skip(f"{failing} is not part of this cycle")
    db.update_risk_settings({"dpm_enabled": 0})

    async def _boom(*a, **k):
        raise RuntimeError(f"{failing} exploded")

    monkeypatch.setattr(mc, failing, _boom)
    ctx = _ctx([_trade(strategy=STRATEGY_BE_RUNNER)])
    ctx.state.sync_cycle = 10_000        # force the periodic branches to run
    ctx.state.profit_cycle = 10_000
    ctx.state.cal_cycle = 10_000

    asyncio.run(mc.run_monitor_cycle(ctx))   # must not raise

    assert quiet == [("be_runner", "t1")], "the per-trade work still has to happen"


def test_a_failing_mt5_sync_does_not_stop_the_cycle(fresh_db, quiet):
    db.update_risk_settings({"dpm_enabled": 0})

    async def _boom():
        raise RuntimeError("sync exploded")

    ctx = _ctx([_trade(strategy=STRATEGY_BE_RUNNER)])
    ctx.sync_closed_mt5_positions = _boom
    ctx.state.sync_cycle = 10_000

    asyncio.run(mc.run_monitor_cycle(ctx))

    assert quiet == [("be_runner", "t1")]


def test_the_cycle_reports_whether_to_poll_fast(fresh_db, quiet):
    """Open trades mean the caller should come back in 1s rather than 5."""
    db.update_risk_settings({"dpm_enabled": 0})

    busy = asyncio.run(mc.run_monitor_cycle(_ctx([_trade()])))
    idle = asyncio.run(mc.run_monitor_cycle(_ctx([])))

    assert busy is True
    assert idle is False
