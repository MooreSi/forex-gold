"""The permanent pin of the dissolved runtime shape (M4).

The god object is being taken apart in batches. Each batch deletes a named
set of wrapper methods whose logic already lives in a service, and each
batch adds its set here. A wrapper that comes back -- reintroduced by hand,
or resurrected by a bad merge -- fails this file immediately, which is the
whole point: the last refactor's wrappers grew back because nothing pinned
their absence.

Deleting a wrapper is only legitimate when its behaviour is covered
elsewhere. For every name below, the service function it used to delegate
to still has its own surface test; where a characterization test drove the
wrapper specifically, that test moved to the service in the same commit.
"""
from __future__ import annotations

import pytest

from backend.src.runtime import SimulationEngine

# B3: wrappers with no production caller and no internal caller -- reachable
# only from old characterization tests, or from nothing at all.
B3_REMOVED = [
    # Referenced by nothing anywhere in the repo before deletion.
    "_compute_be_cost_pts",
    "_push_ai_recovered_created",
    # Risk governor -- services/risk/governor.py owns these.
    "_price_in_entry_range",
    "_rg_day_start_ts",
    "_rg_size_and_check",
    "_rg_check_halt",
    # DPM bookkeeping -- services/dpm/bookkeeping.py.
    "_record_dpm_entry",
    "_update_dpm_peak",
    "_set_dpm_milestone",
    # TP tracking / ladder -- services/positions/.
    "_check_tp_hits",
    "_get_remaining_lots",
    "_log_tp_wait_diagnostic",
    "_run_tp_ladder",
    "_tp_safety_net_check_trade",
    "_close_all_ladder_legs",
    # ORB report -- services/analytics/orb_report.py + trading/orb_execute.py.
    "_orb_auto_execute",
    "_get_orb_target_multiple",
    "_backtest_orb_target_multiple",
    # Misc single-service delegations.
    "_analyse_unrecognised_message",
    "_backfill_max_tp_hit_corrected",
    "_cmd_switch_env",
    "activate_signal",
    "get_all_trades",
    "update_sim_balance",
]


# B5: wrappers called from exactly one internal hub (_monitor_loop or
# _tp_ladder_fast_loop) and nowhere else. The hub now calls the service
# function directly with the bindings the wrapper used to supply. Every
# strategy docstring these carried is already present, verbatim, on the
# service function -- checked before deletion, not assumed.
B5_REMOVED = [
    "_check_sl",
    "_handle_scale_out",
    "_handle_be_runner",
    "_handle_trail_stop",
    "_handle_protected_scale",
    "_handle_conservative",
    "_handle_conservative_trial",
    "_handle_orb_fixed",
    "_handle_scalp_runner",
    "_handle_no_sl_scale",
    "_handle_signal_climber",
    "_handle_reversal_runner",
    "_handle_adaptive_runner",
    "_handle_adaptive_runner_2",
    "_handle_limit_runner",
    "_handle_dynamic_position_management",
    "_run_dpm_calibration",
    "_try_activate_pending_signals",
    "_profit_sweep",
    "_ime_timeout_watchdog",
]


# B6: the remaining wrappers that are CALLED (not injected as callbacks)
# from exactly one internal hub. The five that _scan_messages injects as
# callbacks -- _try_ai_signal_fallback, _find_and_apply_instant_followup,
# _get_trading_balance, _check_pre_trade_filters, _queue_unrecognised -- are
# deliberately NOT here: they exist to bind state into a callback, and B9
# turns them into fields on the scan context. Replacing them with inline
# lambdas now would be churn B9 immediately rewrites.
#
# _sync_closed_mt5_positions' wrappers are also absent by design: every one
# of them is either on the facade (get_tick, get_mt5_account,
# partial_close_trade, record_close, _sync_profit) or part of the
# demo-gated close context (_schedule_profit_sync). The close path is not
# reshaped by M4.
B6_REMOVED = [
    "_process_instant_entry",
    "_apply_sl_adjustment",
    "_last_closed_tp",
]


# B8: two PUBLIC facade methods with no caller anywhere -- not in the
# frontend, not in a controller, not in a service, not in a test. Found by
# auditing facade_allowlist.json entry by entry while locking it to the
# curated set: an allowlist is a contract, and a contract nobody signs is
# just dead weight the ratchet was protecting. The service functions they
# delegated to stay put -- sim_account.get_sim_account is called directly
# by bot_readonly, which is how a caller that actually exists reaches it.
B8_REMOVED = [
    "get_sim_account",
    "reset_simulation",
]


@pytest.mark.parametrize("name", B8_REMOVED)
def test_batch8_uncalled_facade_methods_removed(name):
    assert not hasattr(SimulationEngine, name), (
        f"{name} was a public facade method with zero callers, deleted in "
        f"M4 B8. Call the service function directly if something needs it."
    )


@pytest.mark.parametrize("name", B6_REMOVED)
def test_batch6_scan_hub_wrappers_removed(name):
    assert not hasattr(SimulationEngine, name), (
        f"{name} was inlined into its hub in M4 B6."
    )


def test_the_close_context_builder_is_untouched():
    """The demo gate: M4 must not reshape the close path in any batch."""
    assert hasattr(SimulationEngine, "_make_close_trade_ctx")
    assert hasattr(SimulationEngine, "close_trade")
    assert hasattr(SimulationEngine, "record_close")
    assert hasattr(SimulationEngine, "_schedule_profit_sync")


@pytest.mark.parametrize("name", B5_REMOVED)
def test_batch5_hub_only_wrappers_removed(name):
    assert not hasattr(SimulationEngine, name), (
        f"{name} was inlined into its hub loop in M4 B5 -- the loop calls the "
        f"service directly now."
    )


def test_the_hub_loops_survive_their_inlining():
    """Negative control: inlining must not eat the loops themselves."""
    for hub in ("_monitor_loop", "_tp_ladder_fast_loop"):
        assert hasattr(SimulationEngine, hub), hub


@pytest.mark.parametrize("name", B3_REMOVED)
def test_batch3_wrappers_removed(name):
    assert not hasattr(SimulationEngine, name), (
        f"{name} was deleted in M4 B3 -- its logic lives in a service and is "
        f"tested there. If something needs it again, call the service."
    )


def test_the_survivors_are_still_here():
    """Negative control: the dissolution must not take the facade with it."""
    for survivor in ("startup", "shutdown", "close_trade", "open_trade",
                     "get_tick", "get_open_trades", "_make_close_trade_ctx"):
        assert hasattr(SimulationEngine, survivor), survivor
