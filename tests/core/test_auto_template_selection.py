"""Which channels are on Auto, and what regime they are being judged against.

`core_auto_template.py` decides which EA template each channel should be
running given the live market regime. `auto_enabled_sources` was almost
entirely untested (22 of its 32 statements), and it is what keeps a
recommendation fresh -- a channel missing from this list stops being
reconsidered.

`regime_from_candles` and `is_valid_auto_choice` both carry deliberate
defensive defaults that the module documents at length, and neither had its
default exercised. `is_valid_auto_choice` in particular exists because of a
live incident: when the built-in strategies stopped being selectable on
2026-08-17, GOLD DIGGERS INSTITUTIONAL kept trading `limit_runner` for nearly
nine hours -- six trades at the global 0.1 lot rather than its template's
0.05 -- because a stored recommendation was never revalidated.

Pure logic over injected settings; nothing reaches a broker.
"""
from __future__ import annotations

import pytest

from backend.src.db import database as db
from backend.src.services.positions import core_auto_template as at
from backend.src.services.risk import schedule as sched


# ── regime_from_candles ───────────────────────────────────────────────────────

def test_no_candles_defaults_to_the_defensive_regime():
    """"ranging" rather than "trending": with no data the tighter cell is the
    safer assumption."""
    assert at.regime_from_candles(None) == "ranging"
    assert at.regime_from_candles([]) == "ranging"


def test_too_few_candles_to_classify_defaults_the_same_way():
    assert at.regime_from_candles([{"close": 2400.0}] * 19) == "ranging"


def test_a_failing_detector_defaults_the_same_way(monkeypatch):
    """One place decides the regime so the resolution gate, the auto-manage
    loop and the AI prompt cannot disagree -- including when it breaks."""
    import backend.src.services.dpm.engine as dpm
    monkeypatch.setattr(dpm, "detect_regime",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("bad series")))

    candles = [{"open": 2400, "high": 2401, "low": 2399, "close": 2400}] * 30
    assert at.regime_from_candles(candles) == "ranging"


# ── is_valid_auto_choice ──────────────────────────────────────────────────────

def test_an_empty_choice_is_not_valid():
    assert at.is_valid_auto_choice(None) is False
    assert at.is_valid_auto_choice("") is False
    assert at.is_valid_auto_choice("   ") is False


def test_standing_down_is_a_real_outcome_not_a_stale_value():
    """It means "trade nothing here today", which is a decision."""
    choice = next(c for c in ("stand_down", "STAND_DOWN") if at.is_stand_down(c))
    assert at.is_valid_auto_choice(choice) is True


def test_a_template_that_still_exists_is_valid(monkeypatch):
    monkeypatch.setattr(at, "auto_templates", lambda: ["Asian Reversal - ATR", "GD VIP - Grid"])
    assert at.is_valid_auto_choice("Asian Reversal - ATR") is True


def test_a_choice_outside_todays_vocabulary_is_refused(monkeypatch):
    """The 2026-08-17 incident: stored recommendations outlive the rules that
    produced them, so consumers check rather than trust."""
    monkeypatch.setattr(at, "auto_templates", lambda: ["Asian Reversal - ATR"])
    assert at.is_valid_auto_choice("limit_runner") is False


def test_surrounding_whitespace_does_not_change_the_answer(monkeypatch):
    monkeypatch.setattr(at, "auto_templates", lambda: ["Asian Reversal - ATR"])
    assert at.is_valid_auto_choice("  Asian Reversal - ATR  ") is True


# ── auto_enabled_sources ──────────────────────────────────────────────────────

def _channels(monkeypatch, configs, overrides):
    monkeypatch.setattr(db, "get_all_channel_parser_configs",
                        lambda: configs, raising=False)
    monkeypatch.setattr(db, "get_channel_strategy_override",
                        lambda name: overrides.get(name), raising=False)


def _schedule(monkeypatch, schedule):
    monkeypatch.setattr(sched, "get_trading_schedule", lambda: schedule, raising=False)


def test_nothing_on_auto_is_an_empty_list(monkeypatch):
    _channels(monkeypatch, [{"channel_name": "GD VIP"}], {"GD VIP": "scale_out"})
    _schedule(monkeypatch, {})
    assert at.auto_enabled_sources() == []


def test_a_channel_set_to_auto_is_included(monkeypatch):
    _channels(monkeypatch, [{"channel_name": "GD VIP"}, {"channel_name": "GD INST"}],
              {"GD VIP": "auto", "GD INST": "scale_out"})
    _schedule(monkeypatch, {})
    assert at.auto_enabled_sources() == ["GD VIP"]


def test_a_channel_only_auto_inside_one_schedule_window_still_counts(monkeypatch):
    """The schedule wins at signal time, so a channel that is auto in any
    window still needs its recommendation kept fresh."""
    _channels(monkeypatch, [{"channel_name": "GD VIP"}], {"GD VIP": "scale_out"})
    _schedule(monkeypatch, {
        "monday": [{"telegram_channels": {"GD VIP": {"strategy_override": "auto"}}}],
    })
    assert at.auto_enabled_sources() == ["GD VIP"]


def test_the_internal_engines_count_under_their_display_names(monkeypatch):
    _channels(monkeypatch, [], {})
    _schedule(monkeypatch, {
        "tuesday": [{"reversal_engine_override": "auto",
                     "breakout_engine_override": "auto"}],
    })
    assert at.auto_enabled_sources() == ["Breakout Engine", "Reversal Engine"]


def test_an_engine_set_to_something_else_is_not_included(monkeypatch):
    _channels(monkeypatch, [], {})
    _schedule(monkeypatch, {"tuesday": [{"reversal_engine_override": "scale_out"}]})
    assert at.auto_enabled_sources() == []


def test_a_source_auto_in_two_places_appears_once(monkeypatch):
    _channels(monkeypatch, [{"channel_name": "GD VIP"}], {"GD VIP": "auto"})
    _schedule(monkeypatch, {
        "monday": [{"telegram_channels": {"GD VIP": {"strategy_override": "auto"}}}],
        "tuesday": [{"telegram_channels": {"GD VIP": {"strategy_override": "auto"}}}],
    })
    assert at.auto_enabled_sources() == ["GD VIP"]


def test_an_unreadable_channel_list_still_returns_the_schedule_half(monkeypatch):
    """Each source is wrapped separately on purpose: losing one must not blank
    the other, or a whole set of channels silently stops being reconsidered."""
    monkeypatch.setattr(db, "get_all_channel_parser_configs",
                        lambda: (_ for _ in ()).throw(RuntimeError("db gone")),
                        raising=False)
    _schedule(monkeypatch, {
        "monday": [{"telegram_channels": {"GD VIP": {"strategy_override": "auto"}}}],
    })
    assert at.auto_enabled_sources() == ["GD VIP"]


def test_an_unreadable_schedule_still_returns_the_channel_half(monkeypatch):
    _channels(monkeypatch, [{"channel_name": "GD VIP"}], {"GD VIP": "auto"})
    monkeypatch.setattr(sched, "get_trading_schedule",
                        lambda: (_ for _ in ()).throw(RuntimeError("schedule gone")),
                        raising=False)
    assert at.auto_enabled_sources() == ["GD VIP"]


def test_the_result_is_sorted_so_the_ui_is_stable(monkeypatch):
    _channels(monkeypatch, [{"channel_name": "Zeta"}, {"channel_name": "Alpha"}],
              {"Zeta": "auto", "Alpha": "auto"})
    _schedule(monkeypatch, {})
    assert at.auto_enabled_sources() == ["Alpha", "Zeta"]
