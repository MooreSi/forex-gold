"""Auto template selection -- the backtested regime->template mapping, the
stand-down outcome, and the guarantee that the deterministic floor still
answers when the AI layer is unavailable.

No AI is called here and no order is placed: this is the layer that has to
behave correctly precisely when the API does not.
"""
import os
import tempfile

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_auto_template as auto


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    os.remove(path)


# ── the mapping itself ───────────────────────────────────────────────────

def test_every_channel_regime_cell_resolves():
    for ch in ("GOLD DIGGERS INSTITUTIONAL", "Gold Diggers VIP", "Reversal Engine"):
        for reg in auto.REGIMES:
            assert auto.baseline_for(ch, reg), f"{ch}/{reg} has no answer"


def test_vip_stands_down_in_ranging():
    """The one cell with no positive configuration in 31 days of backtest --
    best available was -0.084R, so it must not fall back to a least-bad
    template."""
    assert auto.is_stand_down(auto.baseline_for("Gold Diggers VIP", "ranging"))
    assert not auto.is_stand_down(auto.baseline_for("Gold Diggers VIP", "trending"))


def test_institutional_always_uses_a_limit_template():
    """Institutional's market entries measured -0.03R against +0.26R for
    resting limit legs -- no regime may put it back on a market fill."""
    for reg in auto.REGIMES:
        assert "Limit" in auto.baseline_for("GOLD DIGGERS INSTITUTIONAL", reg)


def test_decorated_source_names_fold_to_the_right_channel():
    """Sources arrive decorated ("Telegram Auto (X)", "instant:X") and must
    still hit their own cell rather than the generic default."""
    a = auto.baseline_for("GOLD DIGGERS INSTITUTIONAL", "ranging")
    for variant in ("Telegram Auto (GOLD DIGGERS INSTITUTIONAL)",
                    "instant:GOLD DIGGERS INSTITUTIONAL",
                    "gold diggers institutional"):
        assert auto.baseline_for(variant, "ranging") == a
    assert auto.is_stand_down(auto.baseline_for("Telegram Auto (Gold Diggers VIP)", "ranging"))


def test_unknown_channel_gets_conservative_default_not_a_crash():
    for reg in auto.REGIMES:
        assert auto.baseline_for("Some New Channel", reg)


def test_auto_templates_excludes_stand_down():
    """The AI's vocabulary must not contain stand_down as a *template*; it is
    offered separately so a bad parse can't turn into a fake template name."""
    v = auto.auto_templates()
    assert auto.STAND_DOWN not in v
    assert all(t.startswith("template:") for t in v)


# ── regime detection wrapper ─────────────────────────────────────────────

def test_regime_defaults_to_ranging_without_enough_candles():
    """No data must not read as 'trending' -- the tighter cell is the safe
    assumption."""
    assert auto.regime_from_candles(None) == "ranging"
    assert auto.regime_from_candles([]) == "ranging"
    assert auto.regime_from_candles([{"high": 1, "low": 1, "close": 1, "open": 1}] * 5) == "ranging"


def test_regime_classifies_a_real_series():
    candles = [{"open": 100 + i, "high": 100 + i + 1.0, "low": 100 + i - 1.0,
                "close": 100 + i + 0.5} for i in range(40)]
    assert auto.regime_from_candles(candles) in auto.REGIMES


# ── the deterministic floor ──────────────────────────────────────────────

def test_apply_baselines_writes_recs_and_reports_changes(fresh_db):
    srcs = ["GOLD DIGGERS INSTITUTIONAL", "Gold Diggers VIP"]
    changed = auto.apply_baselines("trending", srcs)
    assert set(changed) == set(srcs)
    for s in srcs:
        assert db.get_channel_strategy_rec(s)["strategy"] == auto.baseline_for(s, "trending")


def test_apply_baselines_is_idempotent(fresh_db):
    srcs = ["Reversal Engine"]
    auto.apply_baselines("trending", srcs)
    assert auto.apply_baselines("trending", srcs) == {}, "no-op cycle must not rewrite"


def test_unforced_pass_does_not_revert_an_ai_override(fresh_db):
    """Regression for the two layers fighting. The detector runs every 60s
    and the AI at most every 15 min, so an unforced pass inside the same
    regime must leave the AI's pick alone -- observed live reverting an AI
    stand_down back to the baseline template 60 seconds after it was set."""
    src = "GOLD DIGGERS INSTITUTIONAL"
    auto.apply_baselines("trending", [src])                 # baseline
    db.set_channel_strategy_rec(src, auto.STAND_DOWN, "AI: no edge right now", 0.9)

    assert auto.apply_baselines("trending", [src], force=False) == {}
    assert db.get_channel_strategy_rec(src)["strategy"] == auto.STAND_DOWN

    # ...but a genuine regime change is allowed to reassert the baseline.
    changed = auto.apply_baselines("ranging", [src], force=True)
    assert src in changed
    assert db.get_channel_strategy_rec(src)["strategy"] == auto.baseline_for(src, "ranging")


def test_unforced_pass_still_seeds_a_source_with_no_rec(fresh_db):
    """force=False must not mean 'do nothing' -- a channel that has never
    been evaluated still needs its floor set."""
    changed = auto.apply_baselines("trending", ["Reversal Engine"], force=False)
    assert "Reversal Engine" in changed


def test_unforced_pass_replaces_a_junk_rec(fresh_db):
    """A stale built-in strategy left over from before Auto was enabled is
    not a valid auto pick and should be replaced even on an unforced pass."""
    src = "Reversal Engine"
    db.set_channel_strategy_rec(src, "reversal_runner", "pre-auto leftover", 0.5)
    changed = auto.apply_baselines("trending", [src], force=False)
    assert src in changed
    assert db.get_channel_strategy_rec(src)["strategy"] == auto.baseline_for(src, "trending")


def test_apply_baselines_switches_on_regime_change(fresh_db):
    srcs = ["Reversal Engine"]
    auto.apply_baselines("trending", srcs)
    changed = auto.apply_baselines("ranging", srcs)
    assert "Reversal Engine" in changed
    before, after = changed["Reversal Engine"]
    assert before != after
    assert db.get_channel_strategy_rec("Reversal Engine")["strategy"] == after


def test_apply_baselines_records_stand_down(fresh_db):
    auto.apply_baselines("ranging", ["Gold Diggers VIP"])
    assert auto.is_stand_down(db.get_channel_strategy_rec("Gold Diggers VIP")["strategy"])


def test_describe_cell_explains_stand_down_and_picks():
    assert "stand down" in auto.describe_cell("Gold Diggers VIP", "ranging").lower()
    assert "Auto Limit" in auto.describe_cell("GOLD DIGGERS INSTITUTIONAL", "trending")
