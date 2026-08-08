"""_manage_triggered_signal() used to only ever run one ladder model,
keyed on a `sig["strategy"] == "gd2_unicorn"` check that could never
actually be true: reversal_engine_service._run_cycle overwrites
sig_data["strategy"] with whatever real STRATEGY_* the "Reversal Engine"
channel resolves to (never the literal "gd2_unicorn" signal_generator.
build_signal originally tagged it with), so every GD2 signal fell through
to the REF-ladder branch, where its missing tp4/tp7 fields resolved via
`or` fallbacks to tp3 (as the mid target) and tp1 (as the "final" target)
-- closing the whole runner the moment price cleared tp1 again, instead of
riding it to the real 6R target.

Covers the fix: GD2 detection via source_channel (immune to whatever
strategy override the channel is configured with), and a genuinely
distinct virtual-management model for STRATEGY_CONSERVATIVE, which
discards the signal's own SL/TP levels in favour of fixed distances from
the fill price (matching core/models.py's STRATEGY_DESCRIPTIONS exactly).

ml_engine.record_outcome/core.database.close_bus_entry/sync.ledger.
push_trade_closed are monkeypatched out -- record_outcome persists to the
REAL app's joblib model files (DATA_DIR), which must never be touched by
a test run, especially not this fork's just-imported 267-example model.
"""
import os
import tempfile
from types import SimpleNamespace

import pytest

from backend.src.services.reversal_engine import reversal_engine_repo as db
from backend.src.services.reversal_engine.reversal_engine_service import ReversalEngine


@pytest.fixture
def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    db.close_db()
    os.remove(path)


@pytest.fixture
def engine(fresh_db):
    return ReversalEngine(bridge=None)


@pytest.fixture(autouse=True)
def _no_external_side_effects(monkeypatch):
    """Prevent close-path side effects from touching real app state."""
    monkeypatch.setattr(
        "backend.src.services.reversal_engine.ml_engine.record_outcome",
        lambda signal_id, outcome: None,
    )
    monkeypatch.setattr(
        "backend.src.db.database.close_bus_entry",
        lambda engine, signal_id: None,
    )
    monkeypatch.setattr(
        "backend.src.services.cluster.sync.ledger.push_trade_closed",
        lambda trade: None,
    )


def _tick(bid: float, ask: float):
    mid = (bid + ask) / 2
    return SimpleNamespace(bid=bid, ask=ask, mid=mid, spread=round(ask - bid, 4))


def _refetch(fresh_db, sig_id):
    return fresh_db.get_signal_by_id(sig_id)


# ── GD2 detection (source_channel, not the dead "gd2_unicorn" strategy tag) ──

def test_gd2_signal_rides_full_ladder_to_its_real_6r_target(engine, fresh_db):
    """The core regression: a GD2 signal must bank tp1 -> tp2 -> tp3
    (its own 1R/2R/6R structure) instead of closing everything the moment
    price re-clears tp1 after the tp1 partial."""
    sig_id = fresh_db.create_signal({
        "signal_ref":     "gd2-1",
        "direction":      "BUY",
        "entry_low":      3999.0,
        "entry_high":     4001.0,
        "trigger_price":  4000.0,
        "stop_loss":      3990.0,
        "tp1":            4010.0,   # 1R
        "tp2":            4020.0,   # 2R
        "tp3":            4060.0,   # 6R runner
        "status":         "triggered",
        "outcome":        "open",
        # Overwritten by _active_strategy() in real usage -- deliberately NOT
        # "gd2_unicorn" here, proving GD2 routing no longer depends on it.
        "strategy":       "scale_out",
        "source_channel": "GOLD DIGGERS 2.0 ⚡️",
        "remaining_frac": 1.0,
        "partial_pnl_dollars": 0.0,
    })

    # TP1 (1R) hit -> _TP1_FRAC (50%) banked, signal stays open.
    sig = _refetch(fresh_db, sig_id)
    import asyncio
    asyncio.run(engine._manage_triggered_signal(sig, _tick(4011.0, 4011.2)))
    sig = _refetch(fresh_db, sig_id)
    assert sig["status"] == "triggered"
    assert sig["remaining_frac"] == pytest.approx(0.50, abs=1e-6)
    assert sig["max_tp_hit"] == 1

    # Price clears tp1 again (4025) but hasn't reached tp3 (4060) -- must
    # bank the tp2 partial, NOT close the whole remaining position.
    asyncio.run(engine._manage_triggered_signal(sig, _tick(4024.8, 4025.2)))
    sig = _refetch(fresh_db, sig_id)
    assert sig["status"] == "triggered", (
        "GD2 signal closed early instead of riding to its real 6R target -- "
        "the tp7-falls-back-to-tp1 bug is back"
    )
    assert sig["remaining_frac"] == pytest.approx(0.20, abs=1e-6)
    assert sig["max_tp_hit"] == 2

    # Price finally reaches the real 6R runner target -> full close, win.
    asyncio.run(engine._manage_triggered_signal(sig, _tick(4064.8, 4065.2)))
    sig = _refetch(fresh_db, sig_id)
    assert sig["status"] == "closed"
    assert sig["outcome"] == "win"


def test_non_gd2_signal_with_strategy_named_scale_out_uses_ref_ladder(engine, fresh_db):
    """Sanity check the dispatch doesn't mis-route a normal REF-style
    signal -- default source_channel, non-conservative strategy -- into
    either the GD2 or conservative models."""
    sig_id = fresh_db.create_signal({
        "signal_ref":     "ref-1",
        "direction":      "BUY",
        "entry_low":      3999.0,
        "entry_high":     4001.0,
        "trigger_price":  4000.0,
        "stop_loss":      3990.0,
        "tp1": 4003.0, "tp4": 4020.0, "tp7": 4060.0,
        "status": "triggered", "outcome": "open",
        "strategy": "scale_out",
        "source_channel": "Gold Diggers VIP",
        "remaining_frac": 1.0,
        "partial_pnl_dollars": 0.0,
    })
    sig = _refetch(fresh_db, sig_id)
    import asyncio
    asyncio.run(engine._manage_triggered_signal(sig, _tick(4003.5, 4003.8)))
    sig = _refetch(fresh_db, sig_id)
    # 50% (REF ladder's _TP1_FRAC, not conservative's 80%) banked at tp1 --
    # proves the generic REF ladder ran, not _manage_conservative_signal.
    assert sig["remaining_frac"] == pytest.approx(0.50, abs=1e-6)


# ── STRATEGY_CONSERVATIVE: fixed SL/TP1 from fill, ignore signal levels ──────

def test_conservative_signal_uses_fixed_distances_not_signal_levels(engine, fresh_db):
    """Signal's own sl/tp1 are set far away (would never trigger) --
    only the fixed 5pt SL / 3pt TP1 from the fill price should govern."""
    sig_id = fresh_db.create_signal({
        "signal_ref":     "cons-1",
        "direction":      "BUY",
        "entry_low":      3999.0,
        "entry_high":     4001.0,
        "trigger_price":  4000.0,
        "stop_loss":      3900.0,   # deliberately unreachable in this test
        "tp1":            4500.0,   # deliberately unreachable in this test
        "status": "triggered", "outcome": "open",
        "strategy": "conservative",
        "source_channel": "Gold Diggers VIP",
        "remaining_frac": 1.0,
        "partial_pnl_dollars": 0.0,
    })
    sig = _refetch(fresh_db, sig_id)
    import asyncio
    # Price is +3.5pts from fill -- past the fixed TP1 (4003) but nowhere
    # near the signal's own (ignored) tp1 of 4500.
    asyncio.run(engine._manage_triggered_signal(sig, _tick(4003.5, 4003.8)))
    sig = _refetch(fresh_db, sig_id)
    assert sig["status"] == "triggered"
    assert sig["remaining_frac"] == pytest.approx(0.20, abs=1e-6)   # 80% booked
    assert sig["stop_loss"] == pytest.approx(4000.0, abs=1e-6)       # BE at fill price
    assert sig["sl_moved_to_be"] == 1


def test_conservative_runner_trails_and_never_loosens(engine, fresh_db):
    sig_id = fresh_db.create_signal({
        "signal_ref":     "cons-2",
        "direction":      "BUY",
        "entry_low":      3999.0,
        "entry_high":     4001.0,
        "trigger_price":  4000.0,
        "stop_loss":      3900.0,
        "tp1":            4500.0,
        "status": "triggered", "outcome": "open",
        "strategy": "conservative",
        "source_channel": "Gold Diggers VIP",
        "remaining_frac": 1.0,
        "partial_pnl_dollars": 0.0,
    })
    import asyncio
    sig = _refetch(fresh_db, sig_id)
    asyncio.run(engine._manage_triggered_signal(sig, _tick(4003.5, 4003.8)))  # TP1 -> BE

    # Price runs to 4010 -- trail should advance to 4010-3=4007.
    sig = _refetch(fresh_db, sig_id)
    asyncio.run(engine._manage_triggered_signal(sig, _tick(4009.8, 4010.2)))
    sig = _refetch(fresh_db, sig_id)
    assert sig["status"] == "triggered"
    assert sig["stop_loss"] == pytest.approx(4007.0, abs=1e-6)

    # Price pulls back to 4006 -- below the 4007 trail: closes, and must
    # NOT have loosened the trail back down first.
    asyncio.run(engine._manage_triggered_signal(sig, _tick(4005.8, 4006.2)))
    sig = _refetch(fresh_db, sig_id)
    assert sig["status"] == "closed"
    assert sig["outcome"] == "win"
    assert sig["net_pnl_dollars"] == pytest.approx(39.6, abs=0.05)
