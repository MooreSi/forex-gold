"""Characterizes the CURRENT behavior of forex_trader.test_signal.database
-- see docs/todo/refactor/test-signal-migration/010-*.md and 020-*.md.

The `fresh_db` fixture is parametrized over both database.py and the new
test_signal_repo.py (020) -- every assertion runs against both backends
unmodified, proving behavior equivalence.
"""
import os
import tempfile

import pytest

from forex_trader.test_signal import database as database_module
from forex_trader.test_signal import test_signal_repo as repo_module


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
    base = {
        "direction": "BUY",
        "entry_low": 2399.0, "entry_high": 2401.0, "entry_mid": 2400.0,
        "stop_loss": 2390.0,
    }
    base.update(overrides)
    return base


# ── Init / auth / config ───────────────────────────────────────────────────────

def test_init_seeds_starting_balance(fresh_db):
    assert fresh_db.get_virtual_balance() == 1000.0


def test_auth_roundtrip(fresh_db):
    assert fresh_db.get_password_hash() is None
    fresh_db.set_password_hash("hash123")
    assert fresh_db.get_password_hash() == "hash123"
    fresh_db.touch_last_access()  # must not raise


def test_config_roundtrip(fresh_db):
    assert fresh_db.get_config("missing", "default") == "default"
    fresh_db.set_config("k", "v1")
    assert fresh_db.get_config("k") == "v1"
    fresh_db.set_config("k", "v2")
    assert fresh_db.get_config("k") == "v2"


# ── Balance ───────────────────────────────────────────────────────────────────

def test_set_virtual_balance_rounds_to_2dp(fresh_db):
    fresh_db.set_virtual_balance(1042.567)
    assert fresh_db.get_virtual_balance() == 1042.57


def test_log_balance_and_get_balance_log(fresh_db):
    fresh_db.log_balance(1050.0, 50.0, "win SIG-0001 BUY", signal_id=1)
    rows = fresh_db.get_balance_log()
    assert len(rows) == 1
    assert rows[0]["balance"] == 1050.0
    assert rows[0]["change_amount"] == 50.0


def test_get_max_drawdown(fresh_db):
    fresh_db.log_balance(1050.0, 50.0, "win", None)   # peak 1050
    fresh_db.log_balance(1020.0, -30.0, "loss", None)  # drawdown 30
    assert fresh_db.get_max_drawdown() == 30.0


def test_get_max_drawdown_empty(fresh_db):
    assert fresh_db.get_max_drawdown() == 0.0


# ── Signal CRUD ───────────────────────────────────────────────────────────────

def test_insert_signal_returns_id_and_ref(fresh_db):
    sig_id, ref = fresh_db.insert_signal(_sig())
    assert sig_id > 0
    assert ref == f"SIG-{sig_id:04d}"
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["signal_ref"] == ref
    assert row["status"] == "pending"
    assert row["outcome"] == "open"


def test_get_signal_by_id_missing_returns_empty_dict(fresh_db):
    # NOTE: unlike reversal_engine/breakout_signal (which return None), this
    # module's _row() helper returns {} for a missing row.
    assert fresh_db.get_signal_by_id(999999) == {}


def test_get_open_signals_only_pending_and_triggered(fresh_db):
    p, _ = fresh_db.insert_signal(_sig())
    t, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_triggered(t, 2400.5)
    c, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_status(c, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)
    assert {s["id"] for s in fresh_db.get_open_signals()} == {p, t}


def test_get_all_signals_respects_limit_newest_first(fresh_db):
    ids = [fresh_db.insert_signal(_sig())[0] for _ in range(3)]
    rows = fresh_db.get_all_signals(limit=2)
    assert len(rows) == 2
    assert rows[0]["id"] == ids[-1]


def test_get_recent_closed_signals(fresh_db):
    sig_id, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_status(sig_id, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)
    rows = fresh_db.get_recent_closed_signals()
    assert len(rows) == 1
    assert rows[0]["id"] == sig_id


def test_update_signal_status_sets_all_fields(fresh_db):
    sig_id, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_status(
        sig_id, "closed", "win", close_price=2410.0, pnl_pts=10.0,
        pnl_dollars=100.0, balance_after=1100.0, learning_note="good trade",
    )
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["status"] == "closed"
    assert row["outcome"] == "win"
    assert row["close_price"] == 2410.0
    assert row["pnl_dollars"] == 100.0
    assert row["balance_after"] == 1100.0
    assert row["learning_note"] == "good trade"


def test_update_signal_triggered(fresh_db):
    sig_id, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_triggered(sig_id, 2400.7)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["status"] == "triggered"
    assert row["trigger_price"] == 2400.7


def test_set_signal_sl_moved(fresh_db):
    sig_id, _ = fresh_db.insert_signal(_sig())
    fresh_db.set_signal_sl_moved(sig_id, 2401.5)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["stop_loss"] == 2401.5
    assert row["outcome"] == "tp1_hit"
    assert row["sl_moved_to_be"] == 1


def test_update_conservative_levels(fresh_db):
    sig_id, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_conservative_levels(sig_id, 2395.0, 2403.0)
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["stop_loss"] == 2395.0
    assert row["tp1"] == 2403.0


def test_expire_old_pending_signals(fresh_db):
    old_id, _ = fresh_db.insert_signal(_sig(created_at=0.0))
    fresh_id, _ = fresh_db.insert_signal(_sig())  # created now
    count = fresh_db.expire_old_pending_signals(max_age_hours=24.0)
    assert count == 1
    assert fresh_db.get_signal_by_id(old_id)["status"] == "expired"
    assert fresh_db.get_signal_by_id(fresh_id)["status"] == "pending"


# ── The full lifecycle -- killer test ─────────────────────────────────────────

def test_full_lifecycle_insert_trigger_be_move_close(fresh_db):
    """insert -> trigger -> TP1 BE-move (no partial banking here) -> close at
    TP3. No double-counting risk (no partial-close mechanism), but the
    balance update sequence (get/set/log) must still net out correctly."""
    sig_id, ref = fresh_db.insert_signal(_sig(entry_mid=2400.0, stop_loss=2390.0))
    fresh_db.update_signal_triggered(sig_id, 2400.5)
    fresh_db.set_signal_sl_moved(sig_id, 2401.0)  # TP1 BE-move, no balance change

    assert fresh_db.get_virtual_balance() == 1000.0  # unaffected by BE move

    balance = fresh_db.get_virtual_balance()
    new_balance = round(balance + 96.50, 2)
    fresh_db.set_virtual_balance(new_balance)
    fresh_db.log_balance(new_balance, 96.50, f"win {ref} BUY", sig_id)
    fresh_db.update_signal_status(
        sig_id, "closed", "win", close_price=2440.0, pnl_pts=9.65,
        pnl_dollars=96.50, balance_after=new_balance,
    )

    final = fresh_db.get_signal_by_id(sig_id)
    assert final["status"] == "closed"
    assert final["pnl_dollars"] == 96.50
    assert fresh_db.get_virtual_balance() == 1096.50
    assert final["balance_after"] == 1096.50


# ── ML feature store ──────────────────────────────────────────────────────────

def test_store_and_patch_ml_features(fresh_db):
    sig_id, _ = fresh_db.insert_signal(_sig())
    fresh_db.store_ml_features(sig_id, {"adx_norm": 0.6, "trigger_drift_atr": 0.1})
    feats = fresh_db.get_ml_features_for_signal(sig_id)
    assert feats["adx_norm"] == 0.6

    fresh_db.patch_ml_features(sig_id, {"trigger_drift_atr": 0.42})
    patched = fresh_db.get_ml_features_for_signal(sig_id)
    assert patched["trigger_drift_atr"] == 0.42
    assert patched["adx_norm"] == 0.6  # untouched fields survive the merge


def test_patch_ml_features_no_op_when_nothing_stored(fresh_db):
    sig_id, _ = fresh_db.insert_signal(_sig())
    fresh_db.patch_ml_features(sig_id, {"x": 1})  # must not raise
    assert fresh_db.get_ml_features_for_signal(sig_id) is None


def test_get_ml_training_data_only_closed_valid_outcome_with_features(fresh_db):
    open_sig, _ = fresh_db.insert_signal(_sig())
    fresh_db.store_ml_features(open_sig, {"a": 1})

    closed_no_feats, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_status(closed_no_feats, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)

    closed_with_feats, _ = fresh_db.insert_signal(_sig())
    fresh_db.store_ml_features(closed_with_feats, {"a": 1})
    fresh_db.update_signal_status(closed_with_feats, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)

    ids = {r["signal_id"] for r in fresh_db.get_ml_training_data()}
    assert ids == {closed_with_feats}


def test_get_ml_monitor_data_only_closed_with_ml_prob(fresh_db):
    sig_id, _ = fresh_db.insert_signal(_sig(ml_prob=0.42))
    fresh_db.update_signal_status(sig_id, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)
    rows = fresh_db.get_ml_monitor_data()
    assert len(rows) == 1
    assert rows[0]["signal_id"] == sig_id
    assert "regime" in rows[0]


# ── Live execution / MT5 reconciliation ───────────────────────────────────────

def test_update_live_exec_result(fresh_db):
    sig_id, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_live_exec_result(sig_id, 555, "vsig-1", "success")
    row = fresh_db.get_signal_by_id(sig_id)
    assert row["mt5_ticket"] == 555
    assert row["vantage_signal_id"] == "vsig-1"
    assert row["live_exec_status"] == "success"


def test_update_signal_pnl_from_mt5_corrects_balance_by_delta(fresh_db):
    """Confirmed: already correction-based here (correction = mt5_profit -
    virtual_pnl), unlike breakout_signal's pre-fix close_signal bug."""
    sig_id, _ = fresh_db.insert_signal(_sig())
    fresh_db.set_virtual_balance(1100.0)
    fresh_db.update_signal_status(sig_id, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)
    fresh_db.update_signal_pnl_from_mt5(sig_id, 80.0, "win")  # real was only +80, not +100
    assert fresh_db.get_virtual_balance() == 1080.0  # corrected down by 20, not re-applied


def test_update_signal_pnl_from_mt5_on_missing_signal_is_no_op(fresh_db):
    fresh_db.update_signal_pnl_from_mt5(999999, 10.0, "win")  # must not raise


# ── Stats / performance ────────────────────────────────────────────────────────

def test_get_stats(fresh_db):
    s1, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_status(s1, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)
    s2, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_status(s2, "closed", "loss", 2395.0, -5.0, -50.0, 1050.0)
    stats = fresh_db.get_stats()
    assert stats["total"] == 2
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate"] == 50.0


def test_get_consecutive_losses(fresh_db):
    s1, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_status(s1, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)
    s2, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_status(s2, "closed", "loss", 2395.0, -5.0, -50.0, 1050.0)
    s3, _ = fresh_db.insert_signal(_sig())
    fresh_db.update_signal_status(s3, "closed", "loss", 2395.0, -5.0, -50.0, 1000.0)
    assert fresh_db.get_consecutive_losses() == 2


def test_get_perf_by_session(fresh_db):
    s1, _ = fresh_db.insert_signal(_sig(session="london"))
    fresh_db.update_signal_status(s1, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)
    s2, _ = fresh_db.insert_signal(_sig(session="ny"))
    fresh_db.update_signal_status(s2, "closed", "win", 2430.0, 30.0, 300.0, 1400.0)
    rows = fresh_db.get_perf_by_session()
    assert rows[0]["session"] == "ny"  # higher total_pnl first


def test_get_perf_by_level_type(fresh_db):
    s1, _ = fresh_db.insert_signal(_sig(key_level_type="swing_high"))
    fresh_db.update_signal_status(s1, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)
    rows = fresh_db.get_perf_by_level_type()
    assert rows[0]["key_level_type"] == "swing_high"


def test_get_perf_by_bias(fresh_db):
    s1, _ = fresh_db.insert_signal(_sig(htf_bias="bullish"))
    fresh_db.update_signal_status(s1, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)
    rows = fresh_db.get_perf_by_bias()
    assert rows[0]["htf_bias"] == "bullish"


def test_get_perf_by_regime(fresh_db):
    sig_id, _ = fresh_db.insert_signal(_sig())
    fresh_db.store_ml_features(sig_id, {"adx_norm": 0.7})  # -> "trending"
    fresh_db.update_signal_status(sig_id, "closed", "win", 2410.0, 10.0, 100.0, 1100.0)
    rows = fresh_db.get_perf_by_regime()
    assert len(rows) == 1
    assert rows[0]["regime"] == "trending"
    assert rows[0]["wins"] == 1


# ── Analysis log ──────────────────────────────────────────────────────────────

def test_log_analysis_and_get_analysis_log_roundtrip_json_fields(fresh_db):
    fresh_db.log_analysis({
        "session": "london", "htf_bias": "bullish", "price": 2410.0,
        "atr_m15": 8.0, "adx": 30.0,
        "key_levels": [{"price": 2400, "type": "swing_high"}],
        "candidate": {"direction": "BUY"},
        "result": "signal_created", "claude_decision": "approved",
        "suppressed_reason": "",
    })
    rows = fresh_db.get_analysis_log()
    assert len(rows) == 1
    assert rows[0]["key_levels_json"] == [{"price": 2400, "type": "swing_high"}]
    assert rows[0]["candidate_json"] == {"direction": "BUY"}
