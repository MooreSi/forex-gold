"""Tab subtitles (stage2 phase1/030).

Four of the ten top-level tabs are jargon (Parsing, Signal Generator,
Edge, Analysis). Every tab must carry a plain-language subtitle, rendered
as its tooltip, so the tab bar teaches instead of bewildering.

Static/source-level only — nothing here can reach a broker.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

from frontend.components import tab_labels

from tests.frontend._source import module_source


def _app_tab_names() -> list[str]:
    src = module_source("frontend/app.py")
    block = re.search(r"with ui\.tabs\(\).*?as tabs:\n(.*?)\n\n", src, re.DOTALL)
    assert block, "cannot find the top-level tab definitions in app.py"
    return re.findall(r'ui\.tab\("([^"]+)"', block.group(1))


def test_every_tab_has_a_subtitle_or_plain_name():
    names = _app_tab_names()
    assert len(names) == 10, f"expected the 10 top-level tabs, found {names}"
    assert tab_labels.missing_subtitles(names) == []


def test_the_jargon_tabs_explain_their_job():
    """The unguessable names must say what they actually are.

    "Edge" dropped from this list by the 2026-08-25 merge: upstream removed the
    Edge page itself (152f3df, Simon's own commit), so there is no longer a tab
    to explain. "News" took its place in the tab bar and is checked instead."""
    subs = tab_labels.TAB_SUBTITLES
    assert "telegram" in subs["Parsing"].lower()
    assert "engine" in subs["Signal Generator"].lower() or "strategy" in subs["Signal Generator"].lower()
    assert "history" in subs["Analysis"].lower() or "stats" in subs["Analysis"].lower()
    assert "calendar" in subs["News"].lower() or "blackout" in subs["News"].lower()
    assert "Edge" not in subs, "the Edge page was removed; its subtitle should go with it"


def test_the_subtitle_checker_can_see_a_blank():
    """Negative control: a blank or missing subtitle is detected."""
    assert tab_labels.missing_subtitles(["NoSuchTab"]) == ["NoSuchTab"]
    assert tab_labels.missing_subtitles(
        ["Chart"], {"Chart": "   "}
    ) == ["Chart"]


def test_app_shell_applies_the_subtitles():
    """The shell must actually render them (as tab tooltips), not just
    define them."""
    src = module_source("frontend/app.py")
    assert "tab_labels" in src and "TAB_SUBTITLES" in src
