"""Out of Hours is not on Trading > Strategy.

Owner, 2026-09-11: *"we don't need the out of hours on the trading page as we
already have a schedule which does the same thing and is more detailed, remove
this only from the trading > strategy page. Ensure to keep the schedule page."*

`render_risk_card` is rendered in exactly one place — `frontend/pages/trading/
_strategy.py:86` — so removing the sub-card from it removes it from the only
screen it ever appeared on.

**What is deliberately NOT removed.** `get_effective_strategy` still reads the
`ooh_*` columns and `monitor_cycle` still calls it, so the resolver is live; it
does nothing because `ooh_enabled` is 0. `frontend/pages/settings/_out_of_hours.py`
and its own test file stay, unrendered, so putting the card back is one line.
Both facts are recorded in handover/020.

This file pins the removal so the card cannot drift back in unnoticed, and the
Trading Schedule sub-cards are asserted alongside it because the owner's
instruction named keeping them.
"""
from __future__ import annotations

import pytest
from nicegui import ui
from nicegui.client import Client

from backend.src.controllers import settings_controller as settings_ctl
from frontend.pages.settings import render_risk_card


def _walk(element):
    yield element
    for slot in element.slots.values():
        for child in slot.children:
            yield from _walk(child)


def _texts(root) -> str:
    """Visible text AND the `label` prop — a ui.select's caption lives in
    `_props`, not `.text` (the lesson from test_out_of_hours_card.py)."""
    out = []
    for e in _walk(root):
        out.append(str(getattr(e, "text", "") or ""))
        out.append(str((getattr(e, "_props", {}) or {}).get("label", "") or ""))
    return " | ".join(out)


@pytest.fixture
def rendered(monkeypatch):
    monkeypatch.setattr(settings_ctl, "get_risk_settings", lambda: {
        "ooh_enabled": 1, "ooh_start_time": "23:15", "ooh_end_time": "06:45",
        "ooh_strategy": "trail_stop", "ooh_timezone": "Europe/London",
        "ooh_date_active": 1, "ooh_date_from": "2026-12-24",
        "ooh_date_to": "2026-12-26",
    })
    monkeypatch.setattr(ui, "notify", lambda *a, **k: None)
    with Client(lambda: None, request=None):
        with ui.card() as root:
            render_risk_card("w-full")
    return _texts(root)


def test_the_card_is_gone(rendered):
    """With ooh_enabled deliberately 1 in the fixture: a card that rendered
    only when the feature was switched on would still fail here."""
    assert "Out of Hours" not in rendered


def test_the_rest_of_the_risk_card_is_still_there(rendered):
    """The removal is one sub-card, not the row it sat in."""
    for kept in ("Risk Settings", "Circuit Breaker", "Dynamic Position Management"):
        assert kept in rendered, f"{kept} went with it"
