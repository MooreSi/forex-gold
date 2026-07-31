"""Characterizes the CURRENT behavior of backend.src.services.reversal_engine.database
before it's touched by task 030 (repo/adapter migration) or task 040
(service extraction) -- see
docs/todo/refactor/backend-foundation/020-characterize-reversal-engine-current-behavior.md.

Every test here runs against BOTH the original database.py and the new
reversal_engine_repo.py (030) -- the `fresh_db` fixture is parametrized over
both modules, so the same assertions prove behavior equivalence without
being duplicated or edited per backend. That's the whole point: this file
is the contract task 030 (and eventually 040) is held to.
"""
import os
import tempfile

import pytest

from backend.src.services.reversal_engine import database as database_module
from backend.src.services.reversal_engine import reversal_engine_repo as repo_module


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.remove(path)


@pytest.fixture(params=[database_module, repo_module], ids=["database", "repo"])
def fresh_db(request, db_path):
    module = request.param
    module.init(db_path)
    yield module


# ── Schema / init ────────────────────────────────────────────────────────────

def test_init_creates_all_tables_and_a_starting_balance(fresh_db):
    assert fresh_db.get_virtual_balance() == 1000.0


def test_init_is_idempotent_when_called_again_on_the_same_file(fresh_db, db_path):
    # _create_schema uses CREATE TABLE IF NOT EXISTS -- calling init() twice
    # on the same path must not error or reset state.
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "x1"})
    fresh_db.init(db_path)
    assert fresh_db.get_signal_by_id(sig_id) is not None


# ── Config helpers ────────────────────────────────────────────────────────────

def test_config_roundtrip(fresh_db):
    assert fresh_db.get_config("missing_key", "default") == "default"
    fresh_db.set_config("k", "v1")
    assert fresh_db.get_config("k") == "v1"
    fresh_db.set_config("k", "v2")  # upsert, not duplicate
    assert fresh_db.get_config("k") == "v2"


# ── Signal CRUD ───────────────────────────────────────────────────────────────

def test_create_signal_returns_an_id_and_defaults_created_at(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "sig-1"})
    assert sig_id > 0
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["direction"] == "BUY"
    assert row["status"] == "pending"
    assert row["outcome"] == "open"
    assert row["created_at"] > 0


def test_create_signal_is_insert_or_ignore_on_duplicate_signal_ref(fresh_db):
    id1 = fresh_db.create_signal({"direction": "BUY", "signal_ref": "dup"})
    id2 = fresh_db.create_signal({"direction": "SELL", "signal_ref": "dup"})
    assert id2 == 0  # INSERT OR IGNORE -> no new row, lastrowid not set
    row = fresh_db.get_signal_by_id(id1)
    assert row["direction"] == "BUY"  # original untouched


def test_get_open_signals_only_returns_pending_and_triggered(fresh_db):
    p = fresh_db.create_signal({"direction": "BUY", "signal_ref": "p"})
    t = fresh_db.create_signal({"direction": "BUY", "signal_ref": "t"})
    fresh_db.trigger_signal(t, 2400.0)
    c = fresh_db.create_signal({"direction": "BUY", "signal_ref": "c"})
    fresh_db.close_signal(c, 2400.0, "win", net_pnl_dollars=10.0)
    open_ids = {s["id"] for s in fresh_db.get_open_signals()}
    assert open_ids == {p, t}


def test_get_all_signals_respects_limit_and_orders_newest_first(fresh_db):
    ids = []
    for i in range(3):
        ids.append(fresh_db.create_signal({"direction": "BUY", "signal_ref": f"s{i}"}))
    rows = fresh_db.get_all_signals(limit=2)
    assert len(rows) == 2
    assert rows[0]["id"] == ids[-1]  # newest first (created_at DESC)


def test_trigger_signal_sets_status_and_trigger_price(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.trigger_signal(sig_id, 2410.5)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["status"] == "triggered"
    assert row["trigger_price"] == 2410.5
    assert row["trigger_time"] > 0


def test_expire_signal_sets_status_and_outcome_expired(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.expire_signal(sig_id, "max_age_exceeded")
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["status"] == "expired"
    assert row["outcome"] == "expired"


# ── Balance / close_signal ────────────────────────────────────────────────────

def test_close_signal_updates_status_outcome_and_balance(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.close_signal(sig_id, 2450.0, "win", pnl_pts=50.0, net_pnl_dollars=25.0)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["status"] == "closed"
    assert row["outcome"] == "win"
    assert row["close_price"] == 2450.0
    assert row["net_pnl_dollars"] == 25.0
    assert fresh_db.get_virtual_balance() == 1025.0
    assert row["balance_after"] == 1025.0


def test_close_signal_balance_delta_defaults_to_net_pnl_dollars(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.close_signal(sig_id, 2450.0, "loss", net_pnl_dollars=-15.0)
    assert fresh_db.get_virtual_balance() == 985.0


def test_close_signal_with_explicit_balance_delta_uses_that_not_net_pnl(fresh_db):
    # This is the ladder-close case: net_pnl_dollars is the TOTAL realized
    # result (partials already booked + this leg), but balance_delta is only
    # the remaining leg -- the partials were already added to the balance by
    # book_partial_close. Confirms close_signal doesn't double-count them.
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.book_partial_close(sig_id, leg_net_dollars=10.0, frac_closed=0.33, tp_idx=1)
    bal_after_partial = fresh_db.get_virtual_balance()
    assert bal_after_partial == 1010.0
    fresh_db.close_signal(sig_id, 2450.0, "win", net_pnl_dollars=25.0, balance_delta=15.0)
    assert fresh_db.get_virtual_balance() == 1025.0  # 1010 + 15, not 1000 + 25


def test_move_sl_to_be_defaults_to_entry_midpoint(fresh_db):
    sig_id = fresh_db.create_signal({
        "direction": "BUY", "signal_ref": "s", "entry_low": 2400.0, "entry_high": 2404.0,
    })
    fresh_db.move_sl_to_be(sig_id)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["stop_loss"] == 2402.0
    assert row["sl_moved_to_be"] == 1


def test_move_sl_to_be_with_explicit_price(fresh_db):
    sig_id = fresh_db.create_signal({
        "direction": "BUY", "signal_ref": "s", "entry_low": 2400.0, "entry_high": 2404.0,
    })
    fresh_db.move_sl_to_be(sig_id, be_price=2403.1)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["stop_loss"] == 2403.1


def test_set_stop_loss_trails_the_stop(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.set_stop_loss(sig_id, 2415.678)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["stop_loss"] == 2415.68  # rounded to 2dp


def test_book_partial_close_reduces_remaining_frac_and_banks_pnl(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    remaining = fresh_db.book_partial_close(sig_id, leg_net_dollars=12.5, frac_closed=0.33, tp_idx=1)
    assert remaining == 0.67
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["partial_pnl_dollars"] == 12.5
    assert row["remaining_frac"] == 0.67
    assert row["max_tp_hit"] == 1
    assert fresh_db.get_virtual_balance() == 1012.5


def test_book_partial_close_twice_accumulates_and_tracks_max_tp_hit(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.book_partial_close(sig_id, leg_net_dollars=10.0, frac_closed=0.33, tp_idx=1)
    remaining = fresh_db.book_partial_close(sig_id, leg_net_dollars=8.0, frac_closed=0.33, tp_idx=4)
    assert remaining == 0.34
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["partial_pnl_dollars"] == 18.0
    assert row["max_tp_hit"] == 4  # max(1, 4), never regresses
    assert fresh_db.get_virtual_balance() == 1018.0


def test_book_partial_close_on_missing_signal_is_a_no_op(fresh_db):
    remaining = fresh_db.book_partial_close(999999, leg_net_dollars=10.0, frac_closed=0.33, tp_idx=1)
    assert remaining == 0.0
    assert fresh_db.get_virtual_balance() == 1000.0  # unchanged


def test_reconcile_balance_with_trades_corrects_drift(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.close_signal(sig_id, 2450.0, "win", net_pnl_dollars=25.0)
    fresh_db.set_config("virtual_balance", "999999")  # simulate drift
    corrected = fresh_db.reconcile_balance_with_trades()
    assert corrected == 1025.0
    assert fresh_db.get_virtual_balance() == 1025.0


def test_get_max_drawdown_tracks_peak_to_trough(fresh_db):
    s1 = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s1"})
    fresh_db.close_signal(s1, 2450.0, "win", net_pnl_dollars=50.0)   # balance 1050 (peak)
    s2 = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s2"})
    fresh_db.close_signal(s2, 2400.0, "loss", net_pnl_dollars=-80.0)  # balance 970
    assert fresh_db.get_max_drawdown() == pytest.approx(80.0)


# ── The full lifecycle -- the killer test ─────────────────────────────────────

def test_full_lifecycle_create_trigger_partial_close_matches_hand_calculated_balance(fresh_db):
    """create -> trigger -> TP1 partial (33%) -> close remainder (67%).
    This is the exact sequence engine.py's _check_outcomes runs for a
    winning ladder trade. Hand-calculated expected balance: start at 1000,
    +12.00 partial, +24.00 remainder leg = 1036.00.
    """
    sig_id = fresh_db.create_signal({
        "direction": "BUY", "signal_ref": "lifecycle-1",
        "entry_low": 2400.0, "entry_high": 2404.0,
        "stop_loss": 2390.0, "tp1": 2410.0, "tp7": 2440.0,
    })
    fresh_db.trigger_signal(sig_id, 2402.0)

    remaining = fresh_db.book_partial_close(sig_id, leg_net_dollars=12.00, frac_closed=0.33, tp_idx=1)
    assert remaining == 0.67
    assert fresh_db.get_virtual_balance() == 1012.00

    fresh_db.close_signal(
        sig_id, 2440.0, "win", pnl_pts=38.0,
        net_pnl_dollars=36.00,      # total realized: 12 (partial) + 24 (remainder)
        pnl_dollars=36.00,
        balance_delta=24.00,        # only the remainder leg still needs adding
    )

    final = fresh_db.get_signal_by_id(sig_id)
    assert final["status"] == "closed"
    assert final["outcome"] == "win"
    assert final["net_pnl_dollars"] == 36.00
    assert fresh_db.get_virtual_balance() == 1036.00
    assert final["balance_after"] == 1036.00


# ── ML fields ─────────────────────────────────────────────────────────────────

def test_ml_feature_and_prob_storage(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.store_ml_features(sig_id, [1.0, 2.0, 3.0])
    fresh_db.store_ml_prob(sig_id, 0.62)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["ml_prob"] == 0.62
    assert "1.0" in row["ml_features_json"]


def test_store_ml_prob_at_fill_is_separate_from_creation_time_ml_prob(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.store_ml_prob(sig_id, 0.40)
    fresh_db.store_ml_prob_at_fill(sig_id, 0.55, "bullish")
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["ml_prob"] == 0.40           # creation-time value untouched
    assert row["ml_prob_at_fill"] == 0.55   # fresh fill-time value
    assert row["htf_bias_at_fill"] == "bullish"


def test_get_ml_training_data_only_returns_closed_signals_with_features(fresh_db):
    open_sig = fresh_db.create_signal({"direction": "BUY", "signal_ref": "open"})
    fresh_db.store_ml_features(open_sig, [1.0])

    closed_no_feats = fresh_db.create_signal({"direction": "BUY", "signal_ref": "closed_no_feats"})
    fresh_db.close_signal(closed_no_feats, 2400.0, "win", net_pnl_dollars=5.0)

    closed_with_feats = fresh_db.create_signal({"direction": "BUY", "signal_ref": "closed_with_feats"})
    fresh_db.store_ml_features(closed_with_feats, [1.0, 2.0])
    fresh_db.close_signal(closed_with_feats, 2400.0, "win", net_pnl_dollars=5.0)

    training_ids = {r["id"] for r in fresh_db.get_ml_training_data()}
    assert training_ids == {closed_with_feats}


# ── Correlation ───────────────────────────────────────────────────────────────

def test_update_correlation_marks_confirmed(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.update_correlation(sig_id, ref_signal_id="ref-1", time_delta_s=-120.5, distance_pts=1.2)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["correlated_ref_signal_id"] == "ref-1"
    assert row["correlation_confirmed"] == 1
    assert row["correlation_time_delta_s"] == -120.5


def test_log_near_miss_is_idempotent_per_pair(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.log_near_miss(sig_id, "ref-1", "BUY", 400.0, 5.0, "time")
    fresh_db.log_near_miss(sig_id, "ref-1", "BUY", 400.0, 5.0, "time")  # duplicate, should not insert again
    misses = fresh_db.get_near_misses()
    assert len(misses) == 1


def test_count_today_signals_and_correlated(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    assert fresh_db.count_today_signals() == 1
    assert fresh_db.count_today_correlated() == 0
    fresh_db.update_correlation(sig_id, ref_signal_id="ref-1", time_delta_s=0, distance_pts=0)
    assert fresh_db.count_today_correlated() == 1


def test_upsert_daily_correlation_inserts_then_updates(fresh_db):
    fresh_db.upsert_daily_correlation("2026-07-19", re_signals_sent=1)
    fresh_db.upsert_daily_correlation("2026-07-19", re_signals_sent=2, re_correlated=1)
    rows = fresh_db.get_correlation_history(days=5)
    assert len(rows) == 1  # updated in place, not duplicated
    assert rows[0]["re_signals_sent"] == 2
    assert rows[0]["re_correlated"] == 1


# ── Live execution tracking ───────────────────────────────────────────────────

def test_update_live_exec(fresh_db):
    sig_id = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_db.update_live_exec(sig_id, mt5_ticket=555, vantage_sig_id="v-1", status="executed")
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["mt5_ticket"] == 555
    assert row["vantage_signal_id"] == "v-1"
    assert row["live_exec_status"] == "executed"


# ── Stats / performance breakdowns ────────────────────────────────────────────

def test_get_stats_computes_win_rate_and_totals(fresh_db):
    s1 = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s1"})
    fresh_db.close_signal(s1, 2400.0, "win", net_pnl_dollars=10.0)
    s2 = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s2"})
    fresh_db.close_signal(s2, 2400.0, "loss", net_pnl_dollars=-5.0)
    stats = fresh_db.get_stats()
    assert stats["total"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate"] == 50.0
    assert stats["total_pnl"] == 5.0


def test_get_stats_on_empty_db_returns_zeroed_dict(fresh_db):
    stats = fresh_db.get_stats()
    assert stats["total"] == 0
    assert stats["win_rate"] == 0.0


def test_get_recent_win_rate_defaults_to_half_when_no_closed_trades(fresh_db):
    assert fresh_db.get_recent_win_rate() == 0.5


def test_get_perf_by_session_groups_and_sorts_by_total_pnl(fresh_db):
    s1 = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s1", "session": "london"})
    fresh_db.close_signal(s1, 2400.0, "win", net_pnl_dollars=10.0)
    s2 = fresh_db.create_signal({"direction": "BUY", "signal_ref": "s2", "session": "ny"})
    fresh_db.close_signal(s2, 2400.0, "win", net_pnl_dollars=30.0)
    rows = fresh_db.get_perf_by_session()
    assert rows[0]["session"] == "ny"  # higher total_pnl first
    assert rows[0]["total_pnl"] == 30.0


# ── Level tracking ────────────────────────────────────────────────────────────

def test_upsert_level_inserts_new_then_increments_strength_on_close_match(fresh_db):
    fresh_db.upsert_level("swing_high", 2450.0, "SELL")
    levels = fresh_db.get_active_levels()
    assert len(levels) == 1
    assert levels[0]["strength"] == 1

    fresh_db.upsert_level("swing_high", 2450.4, "SELL")  # within 1pt tolerance
    levels = fresh_db.get_active_levels()
    assert len(levels) == 1  # matched existing, not a new row
    assert levels[0]["strength"] == 2


def test_upsert_level_far_price_creates_a_new_row(fresh_db):
    fresh_db.upsert_level("swing_high", 2450.0, "SELL")
    fresh_db.upsert_level("swing_high", 2470.0, "SELL")  # outside 1pt tolerance
    assert len(fresh_db.get_active_levels()) == 2


def test_deactivate_old_levels_skips_ref_sourced_levels(fresh_db):
    fresh_db.upsert_level("swing_high", 2450.0, "SELL", source="engine")
    fresh_db.upsert_level("round_10", 2500.0, "BUY", source="ref")
    # Force every row to look old enough to deactivate using only the public
    # API: a large negative older_than_hours pushes the cutoff far into the
    # future, so every row's updated_at (set moments ago) falls before it.
    fresh_db.deactivate_old_levels(older_than_hours=-999999)
    remaining = {r["level_type"] for r in fresh_db.get_active_levels()}
    assert remaining == {"round_10"}  # ref-sourced survives, engine-sourced doesn't


# ── Analysis log ──────────────────────────────────────────────────────────────

def test_log_analysis_and_get_analysis_log_roundtrip_levels_json(fresh_db):
    fresh_db.log_analysis({
        "session": "london", "htf_bias": "bullish", "price": 2410.0,
        "atr": 8.5, "adx": 22.0, "levels": [{"p": 2400, "t": "swing_high"}],
        "result": "signal", "reason": None,
    })
    rows = fresh_db.get_analysis_log()
    assert len(rows) == 1
    assert rows[0]["levels"] == [{"p": 2400, "t": "swing_high"}]


# ── Daily research ────────────────────────────────────────────────────────────

def test_store_daily_research_upserts_per_date(fresh_db):
    fresh_db.store_daily_research(
        "2026-07-19", discipline_score=0.7, aggression_score=0.3,
        summary="first pass", notable_trades="", entry_logic_notes="",
        risk_mgmt_notes="", n_messages=5, n_images_analyzed=1, raw_json="{}",
    )
    fresh_db.store_daily_research(
        "2026-07-19", discipline_score=0.8, aggression_score=0.2,
        summary="revised", notable_trades="", entry_logic_notes="",
        risk_mgmt_notes="", n_messages=6, n_images_analyzed=1, raw_json="{}",
    )
    latest = fresh_db.get_latest_daily_research()
    assert latest["summary"] == "revised"  # overwritten, not duplicated
    assert latest["discipline_score"] == 0.8
