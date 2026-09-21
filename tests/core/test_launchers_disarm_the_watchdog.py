"""The Start launchers disarm the watchdog before they do anything slow.

The stop scripts have always disarmed: without it the watchdog restarts the
app two minutes later and Stop does not stop. Start needed it for the mirror
image of the same reason, found live on 2026-09-21.

A cold start is slow -- `FOREX Start.command` builds a venv and pip-installs
the requirements, minutes on a fresh machine -- and it frees port 8888 first.
So for the whole of that window the app is "down" to a watchdog that is still
armed, and the tick launches one. On a machine with two checkouts the entry
launches whichever checkout enabled it last, which is how a remote Mac told to
run the React app came up serving the NiceGUI one: the old app had a built
venv, won the port, and took the single-instance lock the new app then refused
against. See core_autostart.points_at_this_checkout, which repairs the stale
path, and docs/system/domains/platform/README.md.

Disarming is safe because it is only intent, never the scheduler entry: the
app re-arms itself through `sync_from_setting()` on startup when the toggle is
on. The window it closes is exactly the window in which the app is not up to
re-arm.

Text assertions over the shipped scripts, in the manner of
tests/refactor/test_install_guide_matches_the_code.py -- there is no way to
run a .command or a .bat in the suite, and the claim worth pinning is an
ORDERING inside them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.src.services.positions import core_autostart as autostart

REPO = Path(__file__).resolve().parents[2]

MAC_START = REPO / "FOREX Start.command"
MAC_STOP = REPO / "FOREX Stop.command"
WIN_START = REPO / "Setup & Start FOREX.bat"
WIN_STOP = REPO / "Stop FOREX.bat"


def _text(path: Path) -> str:
    assert path.exists(), f"{path.name} is a shipped launcher and must exist"
    return path.read_text(encoding="utf-8", errors="replace")


# ── The flag the scripts delete is the flag the code reads ───────────────────

@pytest.mark.parametrize("script", [MAC_START, MAC_STOP, WIN_START, WIN_STOP])
def test_every_launcher_names_the_flag_file_the_code_uses(script):
    """A renamed constant would leave four scripts deleting nothing, silently."""
    assert autostart.ARMED_FLAG.name in _text(script), (
        f"{script.name} does not name {autostart.ARMED_FLAG.name}")


def test_the_mac_scripts_point_at_the_mac_data_directory():
    """Static text, asserted on every platform: the scripts ship as they are."""
    for script in (MAC_START, MAC_STOP):
        body = _text(script)
        assert "Library/Application Support/ForexTrader" in body, script.name


def test_the_windows_scripts_point_at_the_windows_data_directory():
    for script in (WIN_START, WIN_STOP):
        body = _text(script)
        assert r"%APPDATA%\ForexTrader" in body, script.name


# ── Start disarms, and disarms EARLY ─────────────────────────────────────────

def test_mac_start_disarms_before_it_builds_the_venv():
    """The venv build is the window. Disarming after it would close nothing."""
    body = _text(MAC_START)
    disarm = body.index("watchdog.armed")
    build = body.index("-m venv")
    assert disarm < build, (
        "FOREX Start.command disarms after the venv build — the watchdog can "
        "still launch the other checkout during first-run setup")


def test_mac_start_disarms_before_it_frees_the_port():
    """Freeing the port is what makes the app look down to a tick."""
    body = _text(MAC_START)
    assert body.index("watchdog.armed") < body.index("lsof -ti:8888")


def test_mac_start_disarms_before_it_launches():
    """Anchored on the exec line, not a bare "run.py" — the scripts discuss
    run.py in their comments, and a prose mention is not a launch."""
    body = _text(MAC_START)
    assert body.index("watchdog.armed") < body.index('exec "$VENV_DIR/bin/python" run.py')


def test_windows_start_disarms_before_it_builds_the_venv():
    body = _text(WIN_START)
    assert body.index("watchdog.armed") < body.index("-m venv")


def test_windows_start_disarms_before_it_launches():
    body = _text(WIN_START)
    assert body.index("watchdog.armed") < body.index(r'"%SCRIPT_DIR%run.py"')


# ── Stop still disarms (the behaviour Start is mirroring) ────────────────────

def test_stop_scripts_disarm_before_they_kill_anything():
    """Regression guard: this is what makes Stop mean stop."""
    mac = _text(MAC_STOP)
    assert mac.index("watchdog.armed") < mac.index("lsof -ti:8888")

    win = _text(WIN_STOP)
    assert win.index("watchdog.armed") < win.index("taskkill")
