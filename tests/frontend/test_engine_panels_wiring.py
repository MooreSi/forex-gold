"""Engine panels reach the engines through the controller (restructure 010).

The five files that imported engine service singletons directly must now
import only backend.src.controllers. Source-level: what broke during the
refactor was imports, and that is what this pins.

Nothing here can reach a broker.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.frontend._source import module_source

REPO = Path(__file__).resolve().parents[2]

_DIRECT_SERVICE = re.compile(
    r"from backend\.src\.services\.(breakout_signal|test_signal|reversal_engine)"
    r" import \w*_service"
)

FILES = [
    "frontend/pages/breakout_panel.py",
    "frontend/pages/reversal_panel.py",
    "frontend/pages/test_panel.py",
    "frontend/pages/remote_node.py",
    "frontend/app.py",
]


@pytest.mark.parametrize("rel", FILES)
def test_panels_reach_engines_through_the_controller(rel):
    src = module_source(rel)
    hit = _DIRECT_SERVICE.search(src)
    assert hit is None, f"{rel} still imports an engine service directly: {hit.group(0)}"
    assert "engines_controller" in src, f"{rel} does not use the controller at all"


def test_wiring_check_detects_a_direct_service_import():
    """Negative control for the pattern above."""
    assert _DIRECT_SERVICE.search(
        "from backend.src.services.breakout_signal import breakout_signal_service as x"
    )
    assert _DIRECT_SERVICE.search(
        "from backend.src.services.test_signal import test_signal_service"
    )
    assert not _DIRECT_SERVICE.search(
        "from backend.src.controllers import engines_controller"
    )


def test_the_mode_toggle_import_stays_function_local():
    """app.py's engine access is deliberately deferred past boot — the
    controller import must sit inside _mode_sub_engines, not at module
    level (hoisting it changes startup ordering)."""
    src = module_source("frontend/app.py")
    fn = re.search(r"def _mode_sub_engines\(\):\n(.*?)\n\n", src, re.DOTALL)
    assert fn, "_mode_sub_engines gone — the mode toggle lost its engine access"
    assert "engines_controller" in fn.group(1)
    module_level = src[:src.index("def main_page")]
    assert "engines_controller" not in module_level
