"""Expert Tunables: the Tier-A constants become configurable (M7).

CONFIG_AUDIT.md found ~135 hardcoded behaviour constants across the
services and sorted them into three tiers. Tier A is the short list a
trader might genuinely want to move: the minimum R:R to open at all, the
directional cap, the instant-entry stop bounds and follow-up timeout, how
long a queued signal survives, how stale a signal may be, the duplicate
window, and the broker-close miss threshold.

This exposes exactly those, through one declarative catalogue rendered
generically -- the same shape strategy params already use: defaults in
code, override in the DB merged over them, cache invalidated on
demo/live switch, unknown keys dropped.

The single most important property, asserted first and repeatedly below:
**every default is byte-identical to the constant it replaces**, so
installing this changes nothing about how the app trades until somebody
moves a dial. A tunables page that silently retunes the system on upgrade
would be far worse than the hardcoded constants it replaces.

Every value here is clamped to a declared safe range, because several of
them gate order placement and a fat-fingered 0 in the R:R floor would
open trades this system currently refuses.

No order is placed by anything in this file.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import expert_params as ep


# ── the catalogue itself ─────────────────────────────────────────────────

def test_every_param_is_fully_declared():
    """A half-declared param cannot be rendered generically, which is the
    whole point of a catalogue."""
    assert ep.EXPERT_PARAMS, "catalogue is empty"
    seen = set()
    for param in ep.EXPERT_PARAMS:
        assert param.key not in seen, f"duplicate key {param.key}"
        seen.add(param.key)
        assert param.label, f"{param.key}: needs a label"
        assert param.domain, f"{param.key}: needs a domain to group under"
        assert len(param.desc) > 20, f"{param.key}: desc must explain the effect"
        assert param.min <= param.default <= param.max, (
            f"{param.key}: default {param.default} outside safe range "
            f"[{param.min}, {param.max}]"
        )
        assert param.min < param.max, f"{param.key}: empty range"


# The values these replace, read straight out of the modules that used to
# own them. This is the regression that matters: if a default drifts, the
# app starts trading differently on upgrade with no user action.
EXPECTED_DEFAULTS = {
    "min_tp1_rr":              0.75,   # governor._MIN_RR
    "rg_min_tp1_rr":           1.00,   # governor.RG_MIN_TP1_RR
    "rg_max_stop_atr":         1.5,    # governor.RG_MAX_STOP_ATR
    "max_unprotected_trades":  2,      # governor._MAX_UNPROTECTED
    "ime_sl_min_pts":          8.0,    # instant_entry, lower clamp
    "ime_sl_max_pts":          25.0,   # instant_entry, upper clamp
    "ime_sl_atr_mult":         1.2,    # instant_entry, ATR multiplier
    "ime_followup_timeout_s":  180,    # instant_followup._IME_TIMEOUT_SEC
    "pending_signal_expiry_s": 120,    # pending_activation._EXPIRY
    "max_signal_age_s":        240,    # scan_staleness._MAX_SIGNAL_AGE_SECS
    "duplicate_window_s":      900,    # scan_parse_classify._RECENT_DUP_WINDOW
    "placeholder_no_fill_expiry_s": 86400,
    "mt5_sync_miss_threshold": 2,      # runtime.MT5_SYNC_MISS_THRESHOLD
}


@pytest.mark.parametrize("key,expected", sorted(EXPECTED_DEFAULTS.items()))
def test_defaults_are_byte_identical_to_the_constants_they_replace(key, expected):
    param = ep.spec(key)
    assert param is not None, f"{key} missing from the catalogue"
    assert param.default == expected, (
        f"{key} default drifted: {param.default} != {expected}. Installing "
        f"Expert Tunables must not change how the app trades."
    )


def test_the_catalogue_covers_every_tier_a_value_the_audit_listed():
    assert set(EXPECTED_DEFAULTS) <= {p.key for p in ep.EXPERT_PARAMS}


# ── reading values ───────────────────────────────────────────────────────

def test_a_clean_install_reads_the_defaults(fresh_db):
    for key, expected in EXPECTED_DEFAULTS.items():
        assert ep.get(key) == expected


def test_an_unknown_key_is_a_programming_error_not_a_silent_zero(fresh_db):
    with pytest.raises(KeyError):
        ep.get("no_such_param")


# ── writing values ───────────────────────────────────────────────────────

def test_a_saved_override_is_read_back(fresh_db):
    ep.set_params({"max_signal_age_s": 300})
    assert ep.get("max_signal_age_s") == 300
    # untouched params keep their defaults
    assert ep.get("min_tp1_rr") == 0.75


def test_unknown_keys_are_dropped_rather_than_stored(fresh_db):
    ep.set_params({"max_signal_age_s": 300, "typo_key": 5})
    assert "typo_key" not in ep.all_values()


def test_values_are_clamped_to_the_declared_safe_range(fresh_db):
    """Several of these gate order placement. A 0 in the R:R floor would
    open trades the system currently refuses, so the clamp is a safety
    control, not input tidying."""
    spec = ep.spec("min_tp1_rr")
    ep.set_params({"min_tp1_rr": -5})
    assert ep.get("min_tp1_rr") == spec.min
    ep.set_params({"min_tp1_rr": 10_000})
    assert ep.get("min_tp1_rr") == spec.max


def test_integer_params_stay_integers(fresh_db):
    """A miss threshold of 2.5 cycles is meaningless, and float creep into
    a range() or a comparison against a counter is a real bug source."""
    ep.set_params({"mt5_sync_miss_threshold": 3.7})
    value = ep.get("mt5_sync_miss_threshold")
    assert isinstance(value, int) and value == 3


def test_reset_restores_the_default(fresh_db):
    ep.set_params({"max_signal_age_s": 300})
    assert ep.get("max_signal_age_s") == 300
    ep.reset("max_signal_age_s")
    assert ep.get("max_signal_age_s") == 240


def test_reset_all_restores_every_default(fresh_db):
    ep.set_params({"max_signal_age_s": 300, "min_tp1_rr": 1.5})
    ep.reset_all()
    for key, expected in EXPECTED_DEFAULTS.items():
        assert ep.get(key) == expected


# ── the cache ────────────────────────────────────────────────────────────

def test_the_cache_is_invalidated_on_a_demo_live_switch(fresh_db):
    """Same defect get_risk_settings had: a cache keyed only on time keeps
    serving the other environment's values for its whole TTL after the
    user switches account. These values gate order placement."""
    from backend.src.db import database as db_module
    ep.set_params({"max_signal_age_s": 300})
    assert ep.get("max_signal_age_s") == 300
    assert ep._cache_clear in db_module._cache_invalidators or any(
        getattr(fn, "__self__", None) is ep._cache
        or fn is ep._cache_clear
        for fn in db_module._cache_invalidators
    ), "expert params must register a cache invalidator"


def test_a_write_is_visible_immediately_not_after_the_ttl(fresh_db):
    ep.get("max_signal_age_s")            # prime the cache
    ep.set_params({"max_signal_age_s": 300})
    assert ep.get("max_signal_age_s") == 300


# ── the snapshot used for node sync ──────────────────────────────────────

def test_the_snapshot_round_trips(fresh_db):
    ep.set_params({"max_signal_age_s": 300, "min_tp1_rr": 1.25})
    snapshot = ep.snapshot()
    ep.reset_all()
    assert ep.get("max_signal_age_s") == 240
    ep.apply_snapshot(snapshot)
    assert ep.get("max_signal_age_s") == 300
    assert ep.get("min_tp1_rr") == 1.25
