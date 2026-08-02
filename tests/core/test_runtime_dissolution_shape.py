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
