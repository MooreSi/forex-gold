"""The REF-style ladder was retuned from 33/33/34 to 50/30/20 (task #35 of
the 2026-07-22 Reversal Engine improvements pack): the fork's real trade history
showed a 72% win rate that was still net losing, because the original
33% TP1 leg didn't bank enough to outweigh a full SL loss (0% banked, the
whole sl_dist risked) even at a 72% hit rate -- see reversal_engine_manage.
py's _TP1_FRAC/_MID_FRAC/_FINAL_FRAC constants for the full reasoning.
Front-loading more onto TP1 (the leg actually reached most reliably) is a
judgment call, not a backtested optimum -- these tests pin the exact
fractions and prove the remaining_frac range checks (_POST_TP1_REMAINING/
_POST_MID_REMAINING) still gate the three branches correctly, so a future
retune can't silently reintroduce a gap or overlap between them.
"""
import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

from backend.src.services.reversal_engine import reversal_engine_manage as manage
from backend.src.services.reversal_engine import reversal_engine_repo as db
from backend.src.services.reversal_engine.reversal_engine_service import ReversalEngine
from tests.conftest import remove_db_file


@pytest.fixture
def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    db.close_db()
    remove_db_file(path)


@pytest.fixture
def engine(fresh_db):
    return ReversalEngine(bridge=None)


@pytest.fixture(autouse=True)
def _no_external_side_effects(monkeypatch):
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


def test_fractions_sum_to_one():
    """A retune that doesn't sum to 1.0 would silently leave a sliver of
    the position permanently un-managed (or double-book more than the
    original lot) -- guard it explicitly."""
    total = manage._TP1_FRAC + manage._MID_FRAC + manage._FINAL_FRAC
    assert total == pytest.approx(1.0, abs=1e-9)


def test_remaining_thresholds_match_the_fractions():
    assert manage._POST_TP1_REMAINING == pytest.approx(0.50, abs=1e-9)
    assert manage._POST_MID_REMAINING == pytest.approx(0.20, abs=1e-9)
    assert manage._POST_MID_REMAINING == pytest.approx(manage._FINAL_FRAC, abs=1e-9)


def _build_ref_signal(fresh_db, direction="BUY"):
    return fresh_db.create_signal({
        "signal_ref":     "ladder-1",
        "direction":      direction,
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


def test_full_ladder_books_50_30_20_and_final_close_uses_the_last_20(engine, fresh_db):
    sig_id = _build_ref_signal(fresh_db)

    sig = fresh_db.get_signal_by_id(sig_id)
    asyncio.run(engine._manage_triggered_signal(sig, _tick(4003.5, 4003.8)))  # TP1
    sig = fresh_db.get_signal_by_id(sig_id)
    assert sig["remaining_frac"] == pytest.approx(0.50, abs=1e-6)
    assert sig["partial_pnl_dollars"] > 0

    asyncio.run(engine._manage_triggered_signal(sig, _tick(4020.5, 4020.8)))  # mid (tp4)
    sig = fresh_db.get_signal_by_id(sig_id)
    assert sig["status"] == "triggered"
    assert sig["remaining_frac"] == pytest.approx(0.20, abs=1e-6)

    asyncio.run(engine._manage_triggered_signal(sig, _tick(4060.5, 4060.8)))  # final (tp7)
    sig = fresh_db.get_signal_by_id(sig_id)
    assert sig["status"] == "closed"
    assert sig["outcome"] == "win"


def test_sl_before_tp1_still_risks_the_full_unbanked_position(engine, fresh_db):
    """Sanity baseline the retune must not have touched: a stop hit before
    any partial has been booked still closes 100% of the position as a
    loss (the exact dynamic that motivated front-loading TP1 in the first
    place -- a full loss dwarfs a 33% partial win)."""
    sig_id = _build_ref_signal(fresh_db)
    sig = fresh_db.get_signal_by_id(sig_id)
    asyncio.run(engine._manage_triggered_signal(sig, _tick(3989.8, 3990.0)))
    sig = fresh_db.get_signal_by_id(sig_id)
    assert sig["status"] == "closed"
    assert sig["outcome"] == "loss"
    assert sig["remaining_frac"] == pytest.approx(1.0, abs=1e-6)
