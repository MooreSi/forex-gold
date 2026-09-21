"""The pending watcher's momentum check has to be handed a candle to read.

**No test in this file places, closes or modifies any MT5 order.** The
monitor cycle is driven with a `MonitorCtx` whose every collaborator is a
local function; the pending watcher itself is replaced by a recorder.

WHY THIS EXISTS (owner, 2026-09-21)
-----------------------------------
`pending_activation` defers a queued signal whose direction disagrees with
the last completed M5 candle -- the "momentum mismatch" branch. It is the
only gate in the release path that makes a BUY and a SELL mutually exclusive
on the same tick, and on 2026-09-21 a resume from the daily-loss halt
released two buys and a sell inside 700ms.

It did not fire because it had nothing to read. The check is written
`if dpm_candles and ...`, and the cache behind it is filled by this module
only when `dpm_enabled`. That setting is 0 on this account, so the list is
permanently empty and the branch has never executed -- a gate that has never
been red, in the exact sense rules/40-testing warns about.

`pending_momentum_gate_enabled` is what feeds it without turning Dynamic
Profit Management on. The load-bearing part is the SEPARATE LIST: the shared
`dpm_candles` cache is also read by the Auto strategy picker
(`resolution.regime_from_candles`), the trailing ADX and the resting-order
sweep, so filling it here would make one toggle change four behaviours. The
last two tests are what pin that, and they matter more than the first.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.src.db import database as db
from backend.src.services.positions import monitor_cycle as mc


def _ctx(*, fetched, set_calls, candles=None):
    # get/set share one cell, exactly as runtime._set_dpm_candles does. A
    # fake whose setter did not change what the getter returns would report
    # a second candle fetch that the real runtime never makes.
    cache = {"candles": list(candles) if candles is not None else []}

    def _set(v):
        set_calls.append(v)
        cache["candles"] = v

    async def get_tick():
        return types.SimpleNamespace(bid=2400.0, ask=2400.2)

    async def _noop(*a, **k):
        return None

    async def get_candles(*a, **k):
        fetched.append(a)
        return [{"open": 2400.0, "close": 2401.0}]

    return mc.MonitorCtx(
        bridge=object(), cfg={}, tp_trigger_cache={}, dpm_cache={},
        scale_out_last_fail={}, pending_activation_retry_after={},
        get_dpm_candles=lambda: cache["candles"],
        set_dpm_candles=_set,
        get_tick=get_tick, get_open_trades=lambda: [],
        get_candles=get_candles, is_trading_paused=lambda: False,
        background_open_commentary=_noop, close_full_after_tps=_noop,
        make_close_trade_ctx=lambda: object(),
        sync_closed_mt5_positions=_noop, close_trade=_noop,
    )


@pytest.fixture
def watcher_saw(monkeypatch):
    """Whatever the pending watcher is handed as its candle list."""
    async def _none(*a, **k):
        return None
    for attr in ("_check_equity_protect_impl", "_check_basket_harvest_impl",
                 "_reconcile_orphaned_trades_impl",
                 "_repair_template_placeholders_impl",
                 "_profit_sweep_impl", "_run_dpm_calibration_impl",
                 "_ime_timeout_watchdog_impl", "_revalidate_pending_impl"):
        if hasattr(mc, attr):
            monkeypatch.setattr(mc, attr, _none)
    seen = []

    async def _watch(tick, rs, bridge, retry_after, dpm_candles, **kw):
        seen.append(dpm_candles)
        return False
    monkeypatch.setattr(mc, "_try_activate_pending_signals_impl", _watch)
    return seen


def _run(ctx):
    asyncio.run(mc.run_monitor_cycle(ctx))


class TestTheGateIsFedWhenItIsOn:

    def test_the_watcher_gets_a_candle_with_dpm_off_and_the_gate_on(
        self, fresh_db, watcher_saw,
    ):
        """The configuration this account is actually in, and the one the
        momentum check has never run in."""
        db.update_risk_settings({"dpm_enabled": 0, "auto_execute_signals": 1,
                                 "pending_momentum_gate_enabled": 1})
        fetched, set_calls = [], []

        _run(_ctx(fetched=fetched, set_calls=set_calls))

        assert watcher_saw == [[{"open": 2400.0, "close": 2401.0}]]

    def test_the_watcher_gets_nothing_when_both_are_off(
        self, fresh_db, watcher_saw,
    ):
        """Negative control. Without this, a fetch that happened for some
        unrelated reason would make the test above pass on its own."""
        db.update_risk_settings({"dpm_enabled": 0, "auto_execute_signals": 1,
                                 "pending_momentum_gate_enabled": 0})
        fetched, set_calls = [], []

        _run(_ctx(fetched=fetched, set_calls=set_calls))

        assert watcher_saw == [[]]
        assert fetched == []

    def test_no_fetch_when_the_watcher_is_not_going_to_run(
        self, fresh_db, watcher_saw,
    ):
        """The gate serves one reader. With auto-execution off there is
        nobody to serve, so it must not cost a bridge call per cycle."""
        db.update_risk_settings({"dpm_enabled": 0, "auto_execute_signals": 0,
                                 "pending_momentum_gate_enabled": 1})
        fetched, set_calls = [], []

        _run(_ctx(fetched=fetched, set_calls=set_calls))

        assert fetched == []


class TestItDoesNotDisturbTheSharedCache:
    """One toggle, one behaviour. `dpm_candles` on the runtime is read by the
    Auto strategy picker, the trailing ADX and the resting-order sweep."""

    def test_turning_the_gate_on_does_not_write_to_the_shared_cache(
        self, fresh_db, watcher_saw,
    ):
        db.update_risk_settings({"dpm_enabled": 0, "auto_execute_signals": 1,
                                 "pending_momentum_gate_enabled": 1})
        fetched, set_calls = [], []

        _run(_ctx(fetched=fetched, set_calls=set_calls))

        assert set_calls == []

    def test_with_dpm_on_the_shared_cache_is_still_refreshed(
        self, fresh_db, watcher_saw,
    ):
        """The other half: the gate must not have taken the DPM refresh over.
        Without this, moving the fetch would silently freeze the cache again
        -- the 2026-08-27 bug this file's neighbour was written for."""
        db.update_risk_settings({"dpm_enabled": 1, "auto_execute_signals": 1,
                                 "pending_momentum_gate_enabled": 0})
        fetched, set_calls = [], []

        _run(_ctx(fetched=fetched, set_calls=set_calls))

        assert set_calls == [[{"open": 2400.0, "close": 2401.0}]]

    def test_the_gate_does_not_cause_a_second_fetch_when_dpm_is_already_on(
        self, fresh_db, watcher_saw,
    ):
        """Both on is a legitimate configuration and must not double the
        bridge calls on a path that runs once a second."""
        db.update_risk_settings({"dpm_enabled": 1, "auto_execute_signals": 1,
                                 "pending_momentum_gate_enabled": 1})
        fetched, set_calls = [], []

        _run(_ctx(fetched=fetched, set_calls=set_calls))

        assert len(fetched) == 1
