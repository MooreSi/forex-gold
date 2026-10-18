"""Entry contracts are pure and retain exact policy/quote observations."""
import pytest
from backend.src.services.market import entry_contract as c


def test_quote_prices_must_be_finite_positive_and_ordered():
    assert c.quote_refusal(100, 100.3) is None
    for bid, ask in [(0, 1), (101, 100), (float("nan"), 100), (100, float("inf"))]:
        assert c.quote_refusal(bid, ask)


def test_signal_age_checks_are_explicit_and_do_not_confuse_resting_waits():
    assert c.age_refusal(100, 101, 240) is None
    assert c.age_refusal(100, 341, 240) is not None
    assert c.age_refusal(200, 100, 240) is not None


def test_immutable_policy_hash_changes_with_the_actual_exits():
    original = {"strategy": "template:test", "template": {"sl": 5, "rr": 2}}
    first = c.snapshot("trend_pa", "fill", original, {"entry": 100}, {"risk_atr": 1}, 10)
    original["template"]["sl"] = 7
    second = c.snapshot("trend_pa", "fill", original, {"entry": 100}, {"risk_atr": 1}, 11)
    assert first["policy_hash"] != second["policy_hash"]
    assert first["policy"]["template"]["sl"] == 5


def test_snapshot_rejects_invented_nonfinite_features():
    with pytest.raises(ValueError):
        c.snapshot("breakout", "fill", {}, {}, {"atr": float("nan")}, 10)


def test_timed_broker_quotes_cannot_enter_stale(monkeypatch):
    from types import SimpleNamespace
    from backend.src.services.market import bar_clock
    # Known broker offset; missing adapter timestamps remain explicitly unknown.
    monkeypatch.setattr(bar_clock, "broker_offset_s", lambda ts, now: 10800)
    assert c.broker_quote_refusal(SimpleNamespace(timestamp=10900), 101) is None
    assert c.broker_quote_refusal(SimpleNamespace(timestamp=10900), 161)
    assert c.broker_quote_refusal(SimpleNamespace(timestamp=float("nan")), 101)


def test_policy_resolves_global_fallback_and_marks_auto_unresolved(monkeypatch):
    from backend.src.services.risk import schedule
    from backend.src.db import database
    from backend.src import config
    monkeypatch.setattr(schedule, "effective_channel_strategy", lambda source: None)
    monkeypatch.setattr(database, "get_risk_settings", lambda: {"trade_strategy": "conservative"})
    monkeypatch.setattr(config, "get", lambda key, default=None: "demo")
    assert c.selected_policy("Breakout Engine")["strategy"] == "conservative"
    assert c.selected_policy("Breakout Engine", "auto")["status"] == "unresolved_auto"


def test_equal_width_feature_schemas_are_not_assumed_identical():
    a = c.snapshot("breakout", "creation", {}, {"feature_names": ["adx", "atr"]}, [1, 2], 10)
    b = c.snapshot("breakout", "creation", {}, {"feature_names": ["atr", "adx"]}, [1, 2], 10)
    assert a["schema_hash"] != b["schema_hash"]
