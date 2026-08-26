"""Debug banner (stage2 phase5/030, drives local-debug-mode 070).

Once the fake ticks look real, nothing on screen says the data is
simulated — dangerous the moment a real session is open next to a debug
one. A full-width banner renders above the header whenever is_debug().

Static/source-level — nothing here can reach a broker.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.frontend._source import module_source

REPO = Path(__file__).resolve().parents[2]
APP_SRC = module_source("frontend/app.py")


def test_banner_only_in_debug():
    """The shell renders the banner inside an is_debug() gate."""
    gate = re.search(r"if cfg_module\.is_debug\(\):\n(.*?)\n\n", APP_SRC, re.DOTALL)
    assert gate, "no is_debug() gate in the app shell"
    assert "debug_banner" in gate.group(1), "the gate does not render the banner"
    # Negative control: the banner is referenced nowhere OUTSIDE the gate,
    # so it cannot render in a real session.
    outside = APP_SRC.replace(gate.group(0), "")
    assert "render_debug_banner" not in outside


def test_banner_copy_is_unmistakable():
    from frontend.components import debug_banner
    import inspect

    src = inspect.getsource(debug_banner)
    assert "DEBUG MODE" in src
    assert "simulated" in src.lower()
    assert "no real orders" in src.lower()
