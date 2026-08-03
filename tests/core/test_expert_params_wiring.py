"""The Tier-A constants actually read the catalogue (M7).

A tunables page nothing consumes is worse than no page: it presents a
control that appears to work and changes nothing. These tests drive the
real code paths and assert that moving a dial moves the behaviour.

Each test does the same two things, deliberately:

  1. with defaults, behaviour is EXACTLY what it was when the value was a
     hardcoded constant -- the upgrade-safety property; and
  2. after set_params, behaviour changes -- the it-is-actually-wired
     property.

Only (1) or only (2) would let a real bug through. Together they pin the
value AND the connection.

No real or demo MT5 order is placed, closed or modified: the pre-trade
filter is a pure decision function, and the sync/staleness paths are
driven against a temp database.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import expert_params as ep
from backend.src.services.risk import governor


@pytest.fixture(autouse=True)
def _clean_params(fresh_db):
    """Every test starts from the built-in defaults."""
    ep.reset_all()
    yield
    ep.reset_all()


# ── Filter 1: minimum TP1 R:R ────────────────────────────────────────────

def _rr_check(tp1, stop_loss=2390.0, price=2400.0):
    """check_pre_trade_filters returns an error string, or None to allow."""
    return governor.check_pre_trade_filters(
        "BUY", price, price, stop_loss, tp1, actual_price=price, source_name="x",
    )


def test_the_default_rr_floor_is_still_0_75():
    # SL 10pts away. TP1 at 0.8R (8pts) passes, 0.7R (7pts) is blocked --
    # bracketing the 0.75 default exactly as the constant did.
    assert _rr_check(tp1=2408.0) is None
    blocked = _rr_check(tp1=2407.0)
    assert blocked is not None and "R:R filter blocked" in blocked


def test_raising_the_rr_floor_blocks_a_trade_the_default_allowed():
    assert _rr_check(tp1=2408.0) is None          # allowed at 0.75
    ep.set_params({"min_tp1_rr": 1.5})
    blocked = _rr_check(tp1=2408.0)
    assert blocked is not None, "raising the R:R floor must block thinner setups"
    assert "1.50" in blocked, "the message should quote the configured floor"


def test_lowering_the_rr_floor_admits_a_trade_the_default_blocked():
    assert _rr_check(tp1=2407.0) is not None      # blocked at 0.75
    ep.set_params({"min_tp1_rr": 0.5})
    assert _rr_check(tp1=2407.0) is None


# ── Filter 2: directional cap ────────────────────────────────────────────

def _cap_check(monkeypatch, open_count):
    from backend.src.services.risk import repo as risk_repo
    monkeypatch.setattr(risk_repo, "count_unprotected_same_direction",
                        lambda direction: open_count)
    # tp1=None skips filter 1 entirely, isolating the cap.
    return governor.check_pre_trade_filters(
        "BUY", 2400.0, 2400.0, 2390.0, None, actual_price=2400.0, source_name="x",
    )


def test_the_default_directional_cap_is_still_2(monkeypatch):
    assert _cap_check(monkeypatch, 1) is None
    blocked = _cap_check(monkeypatch, 2)
    assert blocked is not None and "Directional cap blocked" in blocked


def test_raising_the_directional_cap_admits_a_third_trade(monkeypatch):
    assert _cap_check(monkeypatch, 2) is not None
    ep.set_params({"max_unprotected_trades": 4})
    assert _cap_check(monkeypatch, 2) is None
    assert _cap_check(monkeypatch, 4) is not None


# ── Signal staleness ─────────────────────────────────────────────────────

def test_the_default_signal_age_cutoff_is_still_240s():
    from backend.src.services.signals import scan_staleness
    assert scan_staleness.max_signal_age_secs() == 240


def test_the_signal_age_cutoff_follows_the_catalogue():
    from backend.src.services.signals import scan_staleness
    ep.set_params({"max_signal_age_s": 600})
    assert scan_staleness.max_signal_age_secs() == 600


# ── Queued signal expiry ─────────────────────────────────────────────────

def test_the_default_pending_expiry_is_still_120s():
    from backend.src.services.signals import pending_activation
    assert pending_activation.expiry_secs() == 120


def test_the_pending_expiry_follows_the_catalogue():
    from backend.src.services.signals import pending_activation
    ep.set_params({"pending_signal_expiry_s": 45})
    assert pending_activation.expiry_secs() == 45


# ── Duplicate suppression ────────────────────────────────────────────────

def test_the_default_duplicate_window_is_still_15_minutes():
    from backend.src.services.signals import scan_parse_classify
    assert scan_parse_classify.recent_dup_window() == 900


def test_the_duplicate_window_follows_the_catalogue():
    from backend.src.services.signals import scan_parse_classify
    ep.set_params({"duplicate_window_s": 60})
    assert scan_parse_classify.recent_dup_window() == 60


# ── IME follow-up timeout ────────────────────────────────────────────────

def test_the_default_ime_timeout_is_still_180s():
    from backend.src.services.trading import instant_followup
    assert instant_followup.ime_timeout_secs() == 180


def test_the_ime_timeout_follows_the_catalogue():
    from backend.src.services.trading import instant_followup
    ep.set_params({"ime_followup_timeout_s": 300})
    assert instant_followup.ime_timeout_secs() == 300


# ── IME provisional stop bounds ──────────────────────────────────────────

def test_the_default_ime_stop_bounds_are_still_8_to_25_at_1_2_atr():
    from backend.src.services.trading import instant_entry
    assert instant_entry.ime_sl_bounds() == (8.0, 25.0, 1.2)


def test_the_ime_stop_bounds_follow_the_catalogue():
    from backend.src.services.trading import instant_entry
    ep.set_params({"ime_sl_min_pts": 5.0, "ime_sl_max_pts": 40.0,
                   "ime_sl_atr_mult": 2.0})
    assert instant_entry.ime_sl_bounds() == (5.0, 40.0, 2.0)


# ── Broker-close miss threshold ──────────────────────────────────────────

def test_the_default_miss_threshold_is_still_2():
    from backend.src.runtime import TradingRuntime
    engine = TradingRuntime.__new__(TradingRuntime)
    engine._bridge = None
    engine._mt5_sync_missing_streak = {}
    assert engine._make_position_sync_ctx().miss_threshold == 2


def test_the_miss_threshold_follows_the_catalogue():
    from backend.src.runtime import TradingRuntime
    ep.set_params({"mt5_sync_miss_threshold": 5})
    engine = TradingRuntime.__new__(TradingRuntime)
    engine._bridge = None
    engine._mt5_sync_missing_streak = {}
    assert engine._make_position_sync_ctx().miss_threshold == 5
