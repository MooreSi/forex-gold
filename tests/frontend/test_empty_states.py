"""Real empty states (stage2 phase1/040).

"No signals yet" is a dead end — a newcomer doesn't learn what has to be
true before signals or trades appear. Every empty surface must say what
to do next. The copy lives in one shared component so the wording stays
consistent.

Static/source-level plus pure data — nothing here can reach a broker.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

from frontend.components import empty_state

from tests.frontend._source import module_source


def test_every_empty_state_has_a_message_and_a_next_step():
    assert empty_state.EMPTY_STATES, "no empty-state copy defined at all"
    for key, spec in empty_state.EMPTY_STATES.items():
        assert spec["message"].strip(), f"{key}: blank message"
        assert spec["next_step"].strip(), f"{key}: no next step — that's the dead end"


def test_unknown_empty_state_key_is_an_error():
    """Negative control: a typo'd key fails loudly, not with a blank panel."""
    with pytest.raises(KeyError):
        empty_state.spec("not_a_real_surface")


def test_empty_signal_list_shows_next_step():
    """The Trading TG-signals list uses the shared component instead of the
    old dead-end string; the data path (non-empty branch) is untouched."""
    src = (REPO / "frontend" / "pages" / "trading" / "_tg_signals.py").read_text(encoding="utf-8")
    assert "render_empty_state" in src and '"tg_signals"' in src
    assert "No Telegram signals detected yet." not in src
    # The populated branch still renders the table header.
    assert 'ui.label("Time")' in src


def test_empty_history_shows_next_step():
    """The Analysis (history) empty periods point at the next action."""
    src = module_source("frontend/pages/history.py")
    assert "render_empty_state" in src and '"closed_trades"' in src
    assert "No closed trades in this period." not in src


def test_the_signal_empty_state_teaches_the_causal_chain():
    """The copy explains where signals come from (Telegram / build one),
    not merely that a button exists."""
    spec = empty_state.spec("tg_signals")
    text = (spec["message"] + " " + spec["next_step"]).lower()
    assert "telegram" in text
    assert "build" in text or "manual" in text or "yourself" in text
