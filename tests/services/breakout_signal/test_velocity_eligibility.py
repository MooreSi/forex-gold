"""Fast-route policy parity; the inherited engine only records candidates."""
from tests.breakout_signal.test_velocity_break_guards import lab, _run, _NOW


def test_expired_cycle_cannot_feed_velocity(lab):
    lab["cached"]["observed_at"] = _NOW - 121
    assert _run(lab).processed == []


def test_unknown_cycle_age_cannot_feed_velocity(lab):
    lab["cached"].pop("observed_at", None)
    assert _run(lab).processed == []


def test_wide_spread_cannot_feed_velocity(lab):
    lab["params"]["max_spread_pts"] = .5
    assert _run(lab).processed == []


def test_generation_role_change_stops_velocity(lab, monkeypatch):
    from backend.src.services.cluster import node_roles
    monkeypatch.setattr(node_roles, "engines_generate_here", lambda: False)
    assert _run(lab).processed == []


def test_disabled_trading_market_stops_velocity(lab, monkeypatch):
    from backend.src.db import database
    monkeypatch.setattr(database, "is_session_allowed", lambda: (False, "london"))
    assert _run(lab).processed == []


def test_atr_scaled_break_floor_also_applies_to_velocity(lab):
    lab["params"]["min_break_atr_frac"] = 1
    assert _run(lab).processed == []


def test_fresh_cache_timestamp_cannot_hide_old_market_bars(lab):
    lab["cached"]["source_closed_at"] = _NOW - 301
    lab["tick"].timestamp = _NOW + 10800
    assert _run(lab).processed == []
