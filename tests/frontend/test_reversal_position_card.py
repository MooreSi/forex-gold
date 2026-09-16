"""The Reversal Engine's open-position card, in the Breakout card's shape.

Owner request 2026-09-16: make this tab look like the Breakout one while
carrying what is actually relevant to the Reversal Engine. The old card was a
thin left-border strip with a flat row of six numbers; the Breakout card is a
bordered card with a direction block, a price line, a context line and a
reference/status column.

What "relevant to the Reversal Engine" means is the difference between the
two engines' evidence. Breakout shows the broken level, ADX, H1/H4 bias and
a quality score. Reversal decides on a LEVEL and a LEVEL SCORE, gates on an
ML probability, and correlates against the reference channel -- so those are
the fields that must survive the restyle. `level_score` in particular is the
number `capability_gates` blocks on, and dropping it from the card would hide
the reason a signal exists.
"""
from __future__ import annotations

import pytest
from nicegui import ui
from nicegui.client import Client

from frontend.pages.reversal_panel._sections import render_open_signal_card


def _sig(**over):
    base = {
        "signal_ref": "RE-2C49C1", "direction": "BUY", "status": "triggered",
        "entry_low": 4330.10, "entry_high": 4332.40, "stop_loss": 4325.00,
        "tp1": 4338.00, "tp3": 4346.00, "tp7": 4360.00, "rr_tp1": 1.50,
        "level_price": 4331.00, "level_type": "round_5", "level_score": 0.91,
        "session": "asian", "htf_bias": "bullish", "h1_bias": "bullish",
        "adx": 37.4, "atr": 7.8, "ml_prob": 0.106,
        "live_exec_status": "executed", "sl_moved_to_be": 0,
        "correlation_confirmed": 1,
    }
    base.update(over)
    return base


def _text(sig) -> str:
    with Client(lambda: None, request=None):
        container = ui.column()
        with container:
            render_open_signal_card(sig)
        out = []

        def walk(el):
            t = getattr(el, "text", None)
            if t:
                out.append(str(t))
            for child in el.default_slot.children:
                walk(child)

        walk(container)
        return " ".join(out)


class TestItCarriesTheReversalEvidence:
    def test_the_level_price_and_type_are_shown(self):
        text = _text(_sig())
        assert "4331.00" in text
        assert "round_5" in text

    def test_the_level_score_survives_the_restyle(self):
        """The number the capability gate blocks on. Without it the card
        cannot explain why this signal exists."""
        assert "0.91" in _text(_sig())

    def test_the_ml_probability_is_shown(self):
        assert "0.106" in _text(_sig()) or "0.11" in _text(_sig())

    def test_both_biases_are_shown_not_just_one(self):
        """Breakout shows H1 and H4; the Reversal engine gates on HTF and H1,
        and the Asian-session exemption turns on exactly that pair."""
        text = _text(_sig(htf_bias="bearish", h1_bias="bullish"))
        assert "bearish" in text and "bullish" in text

    def test_the_entry_zone_is_a_range_not_a_single_price(self):
        """A reversal signal is a ZONE. Collapsing it to one number, the way
        the Breakout card shows a single entry, would misreport the setup."""
        text = _text(_sig())
        assert "4330.10" in text and "4332.40" in text

    def test_the_correlation_with_the_reference_channel_is_shown(self):
        assert "corr" in _text(_sig()).lower()

    def test_session_and_adx_are_shown(self):
        text = _text(_sig())
        assert "asian" in text
        assert "37.4" in text


class TestItMatchesTheBreakoutCardsShape:
    def test_the_direction_block_names_the_symbol(self):
        text = _text(_sig())
        assert "BUY" in text and "XAUUSD" in text

    def test_the_reference_and_status_are_shown(self):
        text = _text(_sig())
        assert "RE-2C49C1" in text
        assert "TRIGGERED" in text

    def test_stop_and_targets_are_shown(self):
        text = _text(_sig())
        assert "4325.00" in text and "4338.00" in text

    def test_a_moved_stop_says_so(self):
        """Breakout's card marks this and it matters more here: the reversal
        engine moves to break-even on its own schedule."""
        assert "moved" in _text(_sig(sl_moved_to_be=1)).lower()

    def test_the_risk_reward_is_shown(self):
        """Value chosen to round cleanly: 1.55 at one decimal is 1.6, which
        made the first version of this test fail against correct code."""
        assert "R:R 1.5:1" in _text(_sig())


class TestItSurvivesMissingFields:
    def test_a_signal_with_nothing_but_a_direction_does_not_raise(self):
        """`get_open_signals` is `SELECT *` on a table that has gained columns
        five times. A card that raises takes the whole tab down."""
        _text({"direction": "SELL"})

    def test_a_null_level_score_does_not_render_as_a_crash(self):
        _text(_sig(level_score=None, ml_prob=None, rr_tp1=None))
