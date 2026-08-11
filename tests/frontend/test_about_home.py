"""About-home reframed as "Set up once / Every day" (stage2 phase1/050).

The About home was an encyclopedia — four install-focused nav cards with
no hint of what is one-time setup vs daily use. The component regroups
the existing sections into the two buckets and adds the daily-routine
loop (shared with Getting Started — same copy, one source).

Static/source-level plus pure data — nothing here can reach a broker.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

from frontend.components import about_home, getting_started


def _about_section_ids() -> set[str]:
    src = (REPO / "frontend" / "app.py").read_text(encoding="utf-8")
    return set(re.findall(r'(?:el)?if section == "([a-z_]+)"', src))


def test_about_groups_setup_and_daily():
    """Two named groups, each populated from the existing sections."""
    assert about_home.SETUP_ONCE, "Set up once group is empty"
    assert about_home.EVERY_DAY, "Every day group is empty"
    real = _about_section_ids()
    for entry in about_home.SETUP_ONCE + about_home.EVERY_DAY:
        assert entry["section"] in real, f"unknown About section {entry['section']!r}"
        assert entry["title"].strip() and entry["desc"].strip()
    # The install-era assets are one-time; the guides/glossary are daily.
    assert {"instructions", "registration"} <= {e["section"] for e in about_home.SETUP_ONCE}
    assert "glossary" in {e["section"] for e in about_home.EVERY_DAY}

    # Negative control: the id scanner rejects a fake section.
    assert "not_a_real_section" not in real


def test_about_home_shares_the_daily_routine_copy():
    """One source of truth for the routine — About and Getting Started must
    not drift apart."""
    import inspect

    src = inspect.getsource(about_home)
    assert "DAILY_ROUTINE" in src
    assert getting_started.DAILY_ROUTINE, "the routine copy vanished"


def test_app_shell_renders_about_via_the_component():
    src = (REPO / "frontend" / "app.py").read_text(encoding="utf-8")
    assert "about_home" in src, "app.py still renders the About home inline"
