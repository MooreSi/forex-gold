"""Characterizes the CURRENT behavior of forex_trader.breakout_signal.database
-- see docs/todo/refactor/breakout-signal-migration/010-*.md and 020-*.md.

The `fresh_db` fixture is parametrized over both database.py and the new
breakout_signal_repo.py (020) -- every assertion runs against both backends
unmodified, proving behavior equivalence (including the known close_signal
double-counting bug, preserved faithfully in both -- see the killer test's
docstring below).
"""
import os
import tempfile

import pytest

from forex_trader.breakout_signal import database as database_module
from forex_trader.breakout_signal import breakout_signal_repo as repo_module


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


def _sig(**overrides) -> dict:
    """Minimal valid create_signal() payload -- direction/breakout_type/
    entry_mid/stop_loss are required (no .get default in create_signal)."""
    base = {
        "direction": "BUY",
        "breakout_type": "go",
        "entry_mid": 2400.0,
        "stop_loss": 2390.0,
        "signal_ref": "BO-TEST",
    }
    base.update(overrides)
    return base


# ── Init / config ─────────────────────────────────────────────────────────────

def test_init_seeds_starting_balance(fresh_db):
    assert fresh_db.get_virtual_balance() == 1000.0


def test_init_is_idempotent(fresh_db, db_path):
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.init(db_path)
    assert fresh_db.get_signal_by_id(sig_id) is not None


def test_config_roundtrip(fresh_db):
    assert fresh_db.get_config("missing", "default") == "default"
    fresh_db.set_config("k", "v1")
    assert fresh_db.get_config("k") == "v1"
    fresh_db.set_config("k", "v2")
    assert fresh_db.get_config("k") == "v2"


# ── Signal CRUD ───────────────────────────────────────────────────────────────

def test_create_signal_returns_id_with_defaults(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    assert sig_id > 0
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["direction"] == "BUY"
    assert row["status"] == "pending"
    assert row["outcome"] == "open"
    assert row["lot_size"] == 0.10
    assert row["strategy"] == "conservative"


def test_get_open_signals_only_pending_and_triggered(fresh_db):
    p = fresh_db.create_signal(_sig(signal_ref="p"))
    t = fresh_db.create_signal(_sig(signal_ref="t"))
    fresh_db.trigger_signal(t, 2401.0)
    c = fresh_db.create_signal(_sig(signal_ref="c"))
    fresh_db.close_signal(c, 2410.0, "win")
    assert {s["id"] for s in fresh_db.get_open_signals()} == {p, t}


def test_get_all_signals_respects_limit_newest_first(fresh_db):
    ids = [fresh_db.create_signal(_sig(signal_ref=f"s{i}")) for i in range(3)]
    rows = fresh_db.get_all_signals(limit=2)
    assert len(rows) == 2
    assert rows[0]["id"] == ids[-1]


def test_trigger_signal_sets_status_and_price(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.trigger_signal(sig_id, 2405.5)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["status"] == "triggered"
    assert row["trigger_price"] == 2405.5


def test_expire_signal(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.expire_signal(sig_id, "too old")
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["status"] == "expired"
    assert row["outcome"] == "expired"
    assert row["learning_note"] == "too old"


# ── close_signal / balance ─────────────────────────────────────────────────────

def test_close_signal_computes_pnl_from_price_when_no_net_given(fresh_db):
    # BUY entry 2400, lot 0.10 (default), close @ 2410 -> +10pts -> $100
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.close_signal(sig_id, 2410.0, "win")
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["pnl_pts"] == 10.0
    assert row["pnl_dollars"] == 100.0
    assert fresh_db.get_virtual_balance() == 1100.0
    assert row["balance_after"] == 1100.0


def test_close_signal_sell_direction_math(fresh_db):
    sig_id = fresh_db.create_signal(_sig(direction="SELL", entry_mid=2400.0))
    fresh_db.close_signal(sig_id, 2390.0, "win")  # price dropped 10pts, SELL wins
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["pnl_pts"] == 10.0
    assert row["pnl_dollars"] == 100.0


def test_close_signal_net_pnl_dollars_overrides_gross_and_drives_balance(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.close_signal(sig_id, 2410.0, "win", net_pnl_dollars=42.0, net_pnl_pts=4.2)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["pnl_dollars"] == 42.0   # overridden, not the raw 100.0
    assert row["net_pnl_dollars"] == 42.0
    assert row["pnl_pts"] == 4.2
    assert fresh_db.get_virtual_balance() == 1042.0  # balance follows net, not gross


def test_close_signal_on_missing_signal_is_a_no_op(fresh_db):
    fresh_db.close_signal(999999, 2400.0, "win")  # must not raise
    assert fresh_db.get_virtual_balance() == 1000.0


def test_move_sl_to_be_defaults_to_entry_mid(fresh_db):
    sig_id = fresh_db.create_signal(_sig(entry_mid=2403.5))
    fresh_db.move_sl_to_be(sig_id)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["stop_loss"] == 2403.5
    assert row["sl_moved_to_be"] == 1


def test_move_sl_to_be_explicit_price(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.move_sl_to_be(sig_id, be_price=2401.2)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["stop_loss"] == 2401.2


def test_book_partial_close_reduces_remaining_and_banks_pnl(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    remaining = fresh_db.book_partial_close(sig_id, 15.0, 0.33, "TP1 partial")
    assert remaining == 0.67
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["partial_pnl_dollars"] == 15.0
    assert row["remaining_frac"] == 0.67
    assert fresh_db.get_virtual_balance() == 1015.0


def test_book_partial_close_twice_accumulates(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.book_partial_close(sig_id, 10.0, 0.33, "TP1")
    remaining = fresh_db.book_partial_close(sig_id, 8.0, 0.33, "TP2")
    assert remaining == 0.34
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["partial_pnl_dollars"] == 18.0
    assert fresh_db.get_virtual_balance() == 1018.0


def test_book_partial_close_missing_signal_is_no_op(fresh_db):
    remaining = fresh_db.book_partial_close(999999, 10.0, 0.33, "x")
    assert remaining == 0.0
    assert fresh_db.get_virtual_balance() == 1000.0


def test_reconcile_balance_with_trades_corrects_drift(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.close_signal(sig_id, 2410.0, "win")  # +100
    fresh_db.set_config("virtual_balance", "0")  # simulate drift
    correction = fresh_db.reconcile_balance_with_trades()
    assert correction == 1100.0
    assert fresh_db.get_virtual_balance() == 1100.0


def test_reconcile_returns_none_when_already_correct(fresh_db):
    assert fresh_db.reconcile_balance_with_trades() is None


def test_get_max_drawdown(fresh_db):
    s1 = fresh_db.create_signal(_sig(signal_ref="s1"))
    fresh_db.close_signal(s1, 2450.0, "win")   # balance up (peak)
    s2 = fresh_db.create_signal(_sig(signal_ref="s2", direction="SELL"))
    fresh_db.close_signal(s2, 2460.0, "loss")  # balance down from peak
    assert fresh_db.get_max_drawdown() >= 0.0


# ── The full lifecycle -- killer test ─────────────────────────────────────────

def test_full_lifecycle_create_trigger_two_partials_close(fresh_db):
    """create -> trigger -> TP1 partial (33%) -> TP2 partial (33%) -> close
    remainder (34%).

    IMPORTANT -- this pins down a real bug in current behavior, not the
    intended design. Unlike reversal_engine's close_signal (which takes a
    separate `balance_delta` for only the still-unbanked leg),
    breakout_signal's close_signal applies the FULL `net_pnl_dollars` value
    as the balance delta. The real caller (engine.py's _close_and_learn)
    passes `net_dol = partial_booked + leg_net_dol` into close_signal --
    since partial_booked was already added to the balance by the earlier
    book_partial_close() calls, this DOUBLE-COUNTS the partial profit on
    every trade that closes after at least one partial. Confirmed by hand
    -calculation below: 1000 + 12 (TP1) + 11 (TP2) = 1023, then + 32
    (net_pnl_dollars, which re-includes the 23 already banked) = 1055, not
    the "correct" 1032 a naive reading would expect. See 020's task notes
    for the flagged fix (deferred, not part of this characterization pass)."""
    sig_id = fresh_db.create_signal(_sig(entry_mid=2400.0, stop_loss=2390.0))
    fresh_db.trigger_signal(sig_id, 2401.0)

    r1 = fresh_db.book_partial_close(sig_id, 12.0, 0.33, "TP1")
    assert r1 == 0.67
    r2 = fresh_db.book_partial_close(sig_id, 11.0, 0.33, "TP2")
    assert r2 == 0.34
    assert fresh_db.get_virtual_balance() == 1023.0

    fresh_db.close_signal(sig_id, 2440.0, "win", net_pnl_dollars=32.0, net_pnl_pts=3.2)
    final = fresh_db.get_signal_by_id(sig_id)
    assert final["status"] == "closed"
    assert final["net_pnl_dollars"] == 32.0
    assert fresh_db.get_virtual_balance() == 1055.0  # bug-preserving expectation, see docstring
    assert final["balance_after"] == 1055.0


# ── ML fields ─────────────────────────────────────────────────────────────────

def test_ml_features_and_prob_storage(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.store_ml_features(sig_id, [1.0, 2.0])
    fresh_db.store_ml_prob(sig_id, 0.4567)
    row = fresh_db.get_signal_by_id(sig_id)
    assert "1.0" in row["ml_features_json"]
    assert row["ml_prob"] == 0.4567  # rounded to 4dp by store_ml_prob


def test_get_ml_features_for_signal(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    assert fresh_db.get_ml_features_for_signal(sig_id) is None
    fresh_db.store_ml_features(sig_id, [1, 2, 3])
    assert fresh_db.get_ml_features_for_signal(sig_id) is not None


def test_get_ml_training_data_only_closed_with_features_and_valid_outcome(fresh_db):
    open_sig = fresh_db.create_signal(_sig(signal_ref="open"))
    fresh_db.store_ml_features(open_sig, [1.0])

    closed_no_feats = fresh_db.create_signal(_sig(signal_ref="no_feats"))
    fresh_db.close_signal(closed_no_feats, 2410.0, "win")

    closed_with_feats = fresh_db.create_signal(_sig(signal_ref="with_feats"))
    fresh_db.store_ml_features(closed_with_feats, [1.0, 2.0])
    fresh_db.close_signal(closed_with_feats, 2410.0, "win")

    ids = {r["id"] for r in fresh_db.get_ml_training_data()}
    assert ids == {closed_with_feats}


def test_get_ml_monitor_data_only_signals_with_prob(fresh_db):
    a = fresh_db.create_signal(_sig(signal_ref="a"))
    b = fresh_db.create_signal(_sig(signal_ref="b"))
    fresh_db.store_ml_prob(b, 0.5)
    ids = {r["id"] for r in fresh_db.get_ml_monitor_data()}
    assert ids == {b}


# ── Live execution / MT5 reconciliation ───────────────────────────────────────

def test_update_live_exec_result(fresh_db):
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.update_live_exec_result(sig_id, 777, "vsig-1", "success")
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["mt5_ticket"] == 777
    assert row["vantage_signal_id"] == "vsig-1"
    assert row["live_exec_status"] == "success"


def test_update_signal_pnl_from_mt5_when_close_signal_never_called(fresh_db):
    """bal_after is None (close_signal never ran) -> apply full mt5_profit
    as a fresh balance entry."""
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.update_signal_pnl_from_mt5(sig_id, 55.0, "win")
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["pnl_dollars"] == 55.0
    assert row["outcome"] == "win"
    assert fresh_db.get_virtual_balance() == 1055.0


def test_update_signal_pnl_from_mt5_corrects_drift_from_virtual_close(fresh_db):
    """Signal was already closed with a simulated figure; MT5 says the real
    profit was different -- balance gets corrected by the delta."""
    sig_id = fresh_db.create_signal(_sig())
    fresh_db.close_signal(sig_id, 2410.0, "win")  # simulated +100 -> balance 1100
    fresh_db.update_signal_pnl_from_mt5(sig_id, 80.0, "win")  # real was only +80
    assert fresh_db.get_virtual_balance() == 1080.0  # corrected down by 20


def test_update_signal_pnl_from_mt5_on_missing_signal_is_no_op(fresh_db):
    fresh_db.update_signal_pnl_from_mt5(999999, 10.0, "win")  # must not raise


# ── Stats / performance ────────────────────────────────────────────────────────

def test_get_stats(fresh_db):
    s1 = fresh_db.create_signal(_sig(signal_ref="s1"))
    fresh_db.close_signal(s1, 2410.0, "win")
    s2 = fresh_db.create_signal(_sig(signal_ref="s2"))
    fresh_db.close_signal(s2, 2395.0, "loss")
    stats = fresh_db.get_stats()
    assert stats["total"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate"] == 50


def test_get_stats_empty(fresh_db):
    stats = fresh_db.get_stats()
    assert stats["total"] == 0
    assert stats["win_rate"] == 0


def test_get_recent_win_rate_default_and_computed(fresh_db):
    assert fresh_db.get_recent_win_rate() == 0.5
    s1 = fresh_db.create_signal(_sig(signal_ref="s1"))
    fresh_db.close_signal(s1, 2410.0, "win")
    s2 = fresh_db.create_signal(_sig(signal_ref="s2"))
    fresh_db.close_signal(s2, 2395.0, "loss")
    assert fresh_db.get_recent_win_rate() == 0.5


def test_get_consecutive_losses_stops_at_first_win(fresh_db):
    s1 = fresh_db.create_signal(_sig(signal_ref="s1"))
    fresh_db.close_signal(s1, 2410.0, "win")
    s2 = fresh_db.create_signal(_sig(signal_ref="s2"))
    fresh_db.close_signal(s2, 2395.0, "loss")
    s3 = fresh_db.create_signal(_sig(signal_ref="s3"))
    fresh_db.close_signal(s3, 2395.0, "loss")
    assert fresh_db.get_consecutive_losses() == 2  # most recent 2, stops at the win


def test_get_recent_closed_signals(fresh_db):
    s1 = fresh_db.create_signal(_sig(signal_ref="s1"))
    fresh_db.close_signal(s1, 2410.0, "win")
    rows = fresh_db.get_recent_closed_signals()
    assert len(rows) == 1
    assert rows[0]["signal_ref"] == "s1"


def test_get_perf_by_session(fresh_db):
    s1 = fresh_db.create_signal(_sig(signal_ref="s1", session="london"))
    fresh_db.close_signal(s1, 2410.0, "win")
    s2 = fresh_db.create_signal(_sig(signal_ref="s2", session="ny"))
    fresh_db.close_signal(s2, 2430.0, "win")
    rows = fresh_db.get_perf_by_session()
    assert rows[0]["session"] == "ny"  # higher total_pnl first


def test_get_perf_by_breakout_type(fresh_db):
    s1 = fresh_db.create_signal(_sig(signal_ref="s1", breakout_type="go"))
    fresh_db.close_signal(s1, 2410.0, "win")
    s2 = fresh_db.create_signal(_sig(signal_ref="s2", breakout_type="retest"))
    fresh_db.close_signal(s2, 2395.0, "loss")
    types = {r["breakout_type"] for r in fresh_db.get_perf_by_breakout_type()}
    assert types == {"go", "retest"}


def test_get_perf_by_adx_band(fresh_db):
    s1 = fresh_db.create_signal(_sig(signal_ref="s1", adx_at_signal=30.0))
    fresh_db.close_signal(s1, 2410.0, "win")
    rows = fresh_db.get_perf_by_adx_band()
    assert rows[0]["adx_band"] == "25-35 (mild)"


def test_get_perf_by_bias(fresh_db):
    s1 = fresh_db.create_signal(_sig(signal_ref="s1", htf_bias="bullish"))
    fresh_db.close_signal(s1, 2410.0, "win")
    rows = fresh_db.get_perf_by_bias()
    assert rows[0]["htf_bias"] == "bullish"


# ── Analysis log ──────────────────────────────────────────────────────────────

def test_log_analysis_and_get_analysis_log_roundtrip_json_fields(fresh_db):
    fresh_db.log_analysis({
        "session": "london", "htf_bias": "bullish", "h4_bias": "bullish",
        "price": 2410.0, "atr_m15": 8.0, "adx": 30.0,
        "key_levels": [{"price": 2400, "type": "swing_high"}],
        "candidate": {"direction": "BUY"},
        "result": "signal_created", "claude_decision": "approved",
        "suppressed_reason": "",
    })
    rows = fresh_db.get_analysis_log()
    assert len(rows) == 1
    assert rows[0]["key_levels_json"] == [{"price": 2400, "type": "swing_high"}]
    assert rows[0]["candidate_json"] == {"direction": "BUY"}
