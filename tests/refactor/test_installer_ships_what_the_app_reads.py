"""The Windows installer ships every file the running app reaches for.

`test_installer_packages_the_real_tree.py` checks that what the .iss names
exists. This checks the other direction: that what the APP reads from the
install directory is named. Three had drifted by 2026-09-23:

* `mql5/ForexTraderBridge.mq5` -- the EA the Install button copies into
  MetaTrader (`ea_deploy.py`) and the source the stale-build badge compares
  against (`ea_bridge/_version.py`). Not packaged, so on an installed copy the
  button found nothing to install.
* `tools/watchdog.py` -- the script the keep-alive Scheduled Task runs
  (`core_autostart.watchdog_script`). Not packaged, so switching keep-alive
  on registered a task pointing at a file that does not exist.
* `AppVersion` -- the installer's smart-launch skips installing when the
  recorded version equals its own. Left at 0.42 while VERSION moved on, an
  install that already had 0.42 would never receive the new app.

And one thing it must NOT ship: `frontend/node_modules`, 160+ MB of
developer-only packages. The app serves the committed `frontend/dist`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ISS = REPO / "installer" / "FOREX_Trader_Setup.iss"


def _iss() -> str:
    return ISS.read_text(encoding="utf-8", errors="replace")


def _files_lines() -> list[str]:
    return [line for line in _iss().splitlines() if line.strip().startswith("Source:")]


@pytest.mark.parametrize("needed", [
    r"..\mql5\*",
    r"..\tools\watchdog.py",
])
def test_the_installer_packages_what_the_app_reads_at_runtime(needed):
    assert any(f'"{needed}"' in line for line in _files_lines()), (
        f"{needed} is read by the running app but not packaged")


def test_the_frontend_is_packaged_without_node_modules():
    line = next(l for l in _files_lines() if r'"..\frontend\*"' in l)
    excludes = re.search(r'Excludes:\s*"([^"]*)"', line)
    assert excludes and "node_modules" in excludes.group(1).split(",")


def test_the_installer_version_is_the_apps_version():
    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    declared = re.search(r'#define\s+AppVersion\s+"([^"]+)"', _iss()).group(1)
    assert declared == version
