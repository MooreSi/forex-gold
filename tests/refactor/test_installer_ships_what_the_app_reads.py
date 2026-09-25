"""A Windows install has every file the running app reaches for.

Until 2026-09-25 the installer packaged a hand-kept list of files, and three
had drifted by 2026-09-23: `mql5/ForexTraderBridge.mq5` (the EA the Install
button copies into MetaTrader), `tools/watchdog.py` (the keep-alive task's
script) and the AppVersion the installer compared against. The installer is now
a bootstrapper that checks out `main` (test_installer_is_a_bootstrapper.py), so
an install holds exactly what git tracks. The question these answer is the
same one, asked of git instead of a list.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=str(REPO), capture_output=True,
                         text=True, check=True).stdout
    return set(out.splitlines())


@pytest.mark.parametrize("needed", [
    "mql5/ForexTraderBridge.mq5",
    "tools/watchdog.py",
    "frontend/dist/index.html",
])
def test_what_the_app_reads_at_runtime_is_committed(needed):
    assert needed in _tracked(), f"{needed} is read by the running app but not committed"


def test_node_modules_is_never_committed():
    """160+ MB of developer-only packages. The app serves frontend/dist."""
    assert not any(p.startswith("frontend/node_modules/") for p in _tracked())
