"""Characterizes the testable surface of BreakoutEngine before task 030
splits it -- see docs/todo/refactor/breakout-signal-migration/010-*.md.

Scope note (same as reversal_engine's 020): the async orchestration loops
(_run_cycle, _velocity_loop, _outcome_loop, _execute_live,
_run_batch_analysis) are heavily externally coupled (MT5 bridge,
self._main_engine, forex_trader.core.database, ai_provider/Claude) --
left uncovered by design. Covered here: _compute_cost_pts (pure) and
_close_and_learn's P&L math, run against a real isolated DB.
"""
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.src.services.breakout_signal import breakout_signal_repo as db
from backend.src.services.breakout_signal import database as _legacy_bo_db
from backend.src.services.breakout_signal.breakout_signal_service import BreakoutEngine


@pytest.fixture
def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    # _close_and_learn -> bo_ml.record_outcome reaches into the legacy
    # backend.src.services.breakout_signal.database module directly (not yet
    # migrated to breakout_signal_repo) for its ml_features/signal lookups.
    # That module's _DB_PATH is process-wide global state, not reset between
    # test files -- without initializing it here too, this test only passes
    # by accident of whatever _DB_PATH some earlier-collected test file
    # happened to leave behind (confirmed: reproduces "no such table:
    # bo_signals" when nothing else in the run initializes it first).
    fd2, legacy_path = tempfile.mkstemp(suffix=".db")
    os.close(fd2)
    _legacy_bo_db.init(legacy_path)
    yield db
    os.remove(path)
    os.remove(legacy_path)


@pytest.fixture
def engine(fresh_db):
    return BreakoutEngine(bridge=None)


def _sig(**overrides) -> dict:
    base = {
        "direction": "BUY", "breakout_type": "go",
        "entry_mid": 2400.0, "stop_loss": 2390.0, "signal_ref": "BO-TEST",
    }
    base.update(overrides)
    return base


# ── _compute_cost_pts ─────────────────────────────────────────────────────────

def test_compute_cost_pts_falls_back_when_fee_settings_unavailable(engine):
    # core.database isn't importable/configured in this isolated test, so
    # the except-branch fallback (0.35) is what actually runs -- this locks
    # in that fallback value.
    cost = engine._compute_cost_pts(0.30)
    assert cost == 0.35


# ── _close_and_learn -- the money math, run against a real isolated DB ───────

def test_close_and_learn_no_partial_matches_close_signal_direct(engine, fresh_db):
    """No partial booked (remaining_frac=1.0): net P&L is just the leg,
    cost-adjusted. This is the case where breakout_signal's close_signal
    balance-delta behavior is NOT buggy (nothing was double-counted,
    because nothing was pre-banked)."""
    sig_id = fresh_db.create_signal(_sig(entry_mid=2400.0))
    fresh_db.trigger_signal(sig_id, 2401.0)
    engine._close_and_learn(
        sig_id, close_price=2410.0, outcome="win", note="test",
        entry=2400.0, direction="BUY", lot=0.10, cost_pts=0.35,
    )
    row = fresh_db.get_signal_by_id(sig_id)
    # raw 10pts - 0.35 cost = 9.65pts * 0.10 lot * 100 = $96.50
    assert row["net_pnl_dollars"] == 96.50
    assert row["status"] == "closed"
    assert fresh_db.get_virtual_balance() == 1096.50


def test_close_and_learn_with_prior_partial_double_counts_the_partial(engine, fresh_db):
    """Documents the real bug found while characterizing this module (see
    test_database_characterization.py's full-lifecycle test): when a
    partial was already booked, _close_and_learn computes net_dol as
    partial_booked + leg_net_dol, and close_signal applies that FULL value
    as the balance delta -- even though partial_booked was already applied
    to the balance when book_partial_close ran. The balance ends up higher
    than the economically correct result.
    """
    sig_id = fresh_db.create_signal(_sig(entry_mid=2400.0))
    fresh_db.trigger_signal(sig_id, 2401.0)
    fresh_db.book_partial_close(sig_id, 15.0, 0.33, "TP1")
    balance_after_partial = fresh_db.get_virtual_balance()
    assert balance_after_partial == 1015.0

    engine._close_and_learn(
        sig_id, close_price=2410.0, outcome="win", note="test",
        entry=2400.0, direction="BUY", lot=0.10, cost_pts=0.35,
    )
    row = fresh_db.get_signal_by_id(sig_id)
    # leg: remaining_frac=0.67, raw 10pts - 0.35 cost = 9.65 * 0.10 * 0.67 * 100 = $64.66
    # net_dol stored = partial_booked (15.0) + leg (64.66) = $79.66 (economically correct total)
    assert row["net_pnl_dollars"] == 79.66
    # but the balance got the partial(15) applied TWICE: once at book_partial_close,
    # once again inside close_signal's balance_delta=net_pnl_dollars=79.66
    # 1015 (after partial) + 79.66 = 1094.66, NOT 1000 + 79.66 = 1079.66
    assert fresh_db.get_virtual_balance() == 1094.66
    economically_correct_balance = 1000.0 + row["net_pnl_dollars"]
    assert fresh_db.get_virtual_balance() != economically_correct_balance
    assert fresh_db.get_virtual_balance() - economically_correct_balance == 15.0  # = partial_booked, double-counted
