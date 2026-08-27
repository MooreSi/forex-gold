"""The sub-second TP-ladder polling loop.

`tp_ladder_loop.py` was 44.4% covered -- 35 of its 63 statements never executed
-- in `services/positions`, which is still below its coverage floor after the
2026-08-25 merge.

This loop is the sole owner of TP-crossing detection for the ladder strategies
when DPM is off, and the module's own docstring records what it was written to
fix: gold TP levels sometimes sit ~1pt apart, and a spike-and-reverse can cross
several tiers between two samples of the slower monitor loop. Measured cost of
missing them: roughly $1,847 over two days on a 50-trade sample.

So the things worth pinning are the ownership rules -- who this loop manages,
who it must leave alone, and that one bad trade cannot take the loop down.

Nothing reaches a broker. Every collaborator arrives on TPLadderCtx, the
per-strategy handlers are replaced with recorders, and `is_running` returns
False after one pass so the loop runs exactly one cycle instead of forever.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.src.db import database as db
from backend.src.services.positions import tp_ladder_loop as loop
from backend.src.utils.models import (
    STRATEGY_ADAPTIVE_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER_2,
    STRATEGY_LIMIT_RUNNER,
    STRATEGY_REVERSAL_RUNNER,
    STRATEGY_SIGNAL_CLIMBER,
)

LADDER = (STRATEGY_SIGNAL_CLIMBER, STRATEGY_REVERSAL_RUNNER,
          STRATEGY_ADAPTIVE_RUNNER_2, STRATEGY_LIMIT_RUNNER,
          STRATEGY_ADAPTIVE_RUNNER)


@pytest.fixture
def handlers(monkeypatch):
    """Replace the five per-strategy handlers with recorders."""
    seen = []

    def _rec(name):
        async def _fn(trade, tick, bridge, cache, close_full_after_tps=None):
            seen.append((name, trade.get("trade_id")))
        return _fn

    for attr, name in (
        ("_handle_signal_climber_impl", "climber"),
        ("_handle_reversal_runner_impl", "reversal"),
        ("_handle_adaptive_runner_2_impl", "adaptive2"),
        ("_handle_limit_runner_impl", "limit"),
        ("_handle_adaptive_runner_impl", "adaptive"),
    ):
        monkeypatch.setattr(loop, attr, _rec(name))
    return seen


@pytest.fixture
def no_ea(monkeypatch):
    """Default: no EA in play, so nothing is skipped for handoff."""
    from backend.src.services.broker import ea_bridge as ea_mod
    monkeypatch.setattr(ea_mod, "get_instance", lambda: None)


def _ctx(trades, *, tick=("bid", 2400.0), cycles=1):
    """A context whose is_running goes False after `cycles` passes."""
    state = {"n": 0}

    def is_running():
        state["n"] += 1
        return state["n"] <= cycles

    async def get_fresh_tick():
        return types.SimpleNamespace(bid=2400.0, ask=2400.2) if tick else None

    return loop.TPLadderCtx(
        is_running=is_running,
        poll_interval=0.0,
        ladder_strategies=LADDER,
        bridge=object(),
        tp_trigger_cache={},
        get_fresh_tick=get_fresh_tick,
        get_open_trades=lambda: trades,
    )


def _trade(tid="t1", strategy=STRATEGY_SIGNAL_CLIMBER, managed_by=None):
    return {"trade_id": tid, "strategy": strategy, "managed_by": managed_by}


# ── Who owns the trade ────────────────────────────────────────────────────────

def test_the_loop_stands_down_entirely_when_dpm_is_on(fresh_db, handlers, no_ea):
    """DPM takes priority, and the monitor loop no-ops for these strategies when
    DPM is off -- so the two loops never race. If this loop also ran under DPM,
    both would manage the same trade."""
    db.update_risk_settings({"dpm_enabled": 1})

    asyncio.run(loop.tp_ladder_fast_loop(_ctx([_trade()])))

    assert handlers == [], "the ladder loop must not run while DPM owns the trade"


def test_a_non_ladder_strategy_is_left_alone(fresh_db, handlers, no_ea):
    db.update_risk_settings({"dpm_enabled": 0})

    asyncio.run(loop.tp_ladder_fast_loop(_ctx([_trade(strategy="scale_out")])))

    assert handlers == []


def test_no_tick_means_nothing_is_handled(fresh_db, handlers, no_ea):
    """Acting on a stale or absent price is how a tier gets banked at the wrong
    level."""
    db.update_risk_settings({"dpm_enabled": 0})

    asyncio.run(loop.tp_ladder_fast_loop(_ctx([_trade()], tick=None)))

    assert handlers == []


def test_an_ea_owned_trade_is_skipped_while_the_ea_is_healthy(fresh_db, handlers, monkeypatch):
    """The handoff rule. Without it this loop double-manages a trade already
    being handled natively inside MT5."""
    from backend.src.services.broker import ea_bridge as ea_mod
    monkeypatch.setattr(ea_mod, "get_instance",
                        lambda: types.SimpleNamespace(is_ea_healthy=lambda: True))
    db.update_risk_settings({"dpm_enabled": 0})

    asyncio.run(loop.tp_ladder_fast_loop(
        _ctx([_trade("ea-one", managed_by="ea"), _trade("py-one")])))

    assert handlers == [("climber", "py-one")], "only the Python-managed trade"


def test_an_ea_owned_trade_is_reclaimed_when_the_ea_is_unhealthy(fresh_db, handlers, monkeypatch):
    """An EA that has gone quiet must not leave its trades unmanaged."""
    from backend.src.services.broker import ea_bridge as ea_mod
    monkeypatch.setattr(ea_mod, "get_instance",
                        lambda: types.SimpleNamespace(is_ea_healthy=lambda: False))
    db.update_risk_settings({"dpm_enabled": 0})

    asyncio.run(loop.tp_ladder_fast_loop(_ctx([_trade("ea-one", managed_by="ea")])))

    assert handlers == [("climber", "ea-one")]


# ── Routing ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("strategy,expected", [
    (STRATEGY_SIGNAL_CLIMBER, "climber"),
    (STRATEGY_REVERSAL_RUNNER, "reversal"),
    (STRATEGY_ADAPTIVE_RUNNER_2, "adaptive2"),
    (STRATEGY_LIMIT_RUNNER, "limit"),
    (STRATEGY_ADAPTIVE_RUNNER, "adaptive"),
])
def test_each_strategy_reaches_its_own_handler(fresh_db, handlers, no_ea, strategy, expected):
    """A misrouted strategy banks the wrong tiers on a live trade."""
    db.update_risk_settings({"dpm_enabled": 0})

    asyncio.run(loop.tp_ladder_fast_loop(_ctx([_trade("t1", strategy=strategy)])))

    assert handlers == [(expected, "t1")]


# ── Resilience ────────────────────────────────────────────────────────────────

def test_one_failing_trade_does_not_stop_the_others(fresh_db, monkeypatch, no_ea):
    """This loop is the sole owner of TP detection for these strategies. If one
    bad trade aborted the pass, every other ladder would stop being watched."""
    db.update_risk_settings({"dpm_enabled": 0})
    handled = []

    async def _boom(trade, tick, bridge, cache, close_full_after_tps=None):
        if trade["trade_id"] == "bad":
            raise RuntimeError("handler exploded")
        handled.append(trade["trade_id"])

    monkeypatch.setattr(loop, "_handle_signal_climber_impl", _boom)

    asyncio.run(loop.tp_ladder_fast_loop(_ctx([_trade("bad"), _trade("good")])))

    assert handled == ["good"]


def test_a_failure_reading_settings_does_not_kill_the_loop(fresh_db, handlers, no_ea, monkeypatch):
    """The whole cycle is wrapped: a transient DB error must cost one pass, not
    the loop."""
    def _boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(db, "get_risk_settings", _boom)
    ctx = _ctx([_trade()], cycles=2)

    asyncio.run(loop.tp_ladder_fast_loop(ctx))   # must return, not raise

    assert handlers == []


def test_the_loop_stops_when_is_running_goes_false(fresh_db, handlers, no_ea):
    """is_running is a callable precisely so shutdown is noticed while awaiting."""
    db.update_risk_settings({"dpm_enabled": 0})
    ctx = _ctx([_trade()], cycles=3)

    asyncio.run(loop.tp_ladder_fast_loop(ctx))

    assert len(handlers) == 3, "three cycles, then it should have stopped"
