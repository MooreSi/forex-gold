"""Header Help "?" button -> Getting Started (stage2 phase1/020).

The app's genuinely good guidance (Setup Instructions, Orchestration, the
Glossary) lives behind the About tab's nav cards with nothing pointing at
it. These tests pin the two fixes: an always-there header Help control,
and a Getting Started surface that links the existing docs by their real
section ids rather than duplicating them.

No test here can reach a broker: everything is static source inspection
plus a pure component import.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.frontend._source import module_source

REPO = Path(__file__).resolve().parents[2]
APP_SRC = module_source("frontend/app.py")


def _about_section_ids() -> set[str]:
    """The section ids _render_about really handles (its dispatch arms)."""
    return set(re.findall(r'(?:el)?if section == "([a-z_]+)"', APP_SRC)) | {"home"}


def test_help_button_present_on_shell():
    """The header carries a help control wired to the Getting Started
    component — removing either the button or the wiring fails this."""
    assert re.search(r'icon="help', APP_SRC), "no help icon button in the app shell"
    assert "getting_started" in APP_SRC, "the shell never wires the Getting Started component"


def test_getting_started_links_the_existing_docs():
    """Every section Getting Started points at must really exist in About —
    a renamed/removed About section must fail here, not dead-end the user."""
    from frontend.components import getting_started

    real_ids = _about_section_ids()
    referenced = {entry["section"] for entry in getting_started.GUIDES}
    assert referenced, "Getting Started links no guides at all"
    assert referenced <= real_ids, f"unknown About sections: {referenced - real_ids}"
    # The three assets the review called out must all be reachable.
    assert {"orchestration", "instructions", "glossary"} <= referenced

    # Negative control: the id scanner can see a fake id as unknown.
    assert "not_a_real_section" not in real_ids


def test_getting_started_offers_the_start_here_checklist():
    """Getting Started is also the way back to the Start Here checklist
    after it has been dismissed."""
    from frontend.components import getting_started
    import inspect

    src = inspect.getsource(getting_started)
    assert "start_here" in src or "Start Here" in src
