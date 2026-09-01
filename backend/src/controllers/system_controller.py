"""The app's own environment, as the UI needs to see it.

Restarting, where it is installed, which build it is, and the port/process
handling the MT5 bridge panel uses to see and clear a stuck bridge.

Version sits here rather than in its own module because "what build am I" and
"where am I installed" are the same question asked twice, and the About screen
and the log export both want the answer.

Here rather than in settings_controller because these are not settings. That
file already carries config, credentials, retention and expert params; adding
process-killing to it would make it the place everything goes when nobody
decided where it belonged.

Every function forwards to backend.src.utils (os_utils, version_history)
unchanged. Nothing here
decides anything -- the point is only that the frontend reaches it through a
controller, so a page cannot be rewired by a change to a utility's signature
without this file noticing first.
"""
from __future__ import annotations

from pathlib import Path

from backend.src.services.risk import clock as _clock
from backend.src.services.positions import core_app_update as _update
from backend.src.services.positions import core_autostart as _autostart
from backend.src.utils import os_utils as _os
from backend.src.utils import version_history as _vh

__all__ = [
    "local_now", "local_today",
    "repo_root",
    "restart_app",
    "open_restart_log",
    "is_port_listening",
    "pids_listening_on",
    "pids_matching",
    "kill_pid",
    "kill_matching",
    "start_prevent_sleep",
    "stop_prevent_sleep",
    "is_preventing_sleep",
    "app_version",
    "releases",
    "check_for_update",
    "summarise_changes",
    "apply_update",
    "autostart_is_supported",
    "autostart_is_installed",
    "autostart_is_armed",
    "autostart_enable",
    "autostart_disable",
    "AUTOSTART_CHECK_INTERVAL_SECS",
]

# How often the watchdog checks the app is still up.
AUTOSTART_CHECK_INTERVAL_SECS = _autostart.CHECK_INTERVAL_SECS


def start_prevent_sleep():
    """Ask the OS to keep the machine awake. Returns an opaque handle to pass
    back to stop_prevent_sleep()."""
    return _os.start_prevent_sleep()


def stop_prevent_sleep(handle) -> None:
    return _os.stop_prevent_sleep(handle)


def is_preventing_sleep(handle) -> bool:
    return _os.is_preventing_sleep(handle)


def repo_root() -> Path:
    return _os.repo_root()


def restart_app(root) -> None:
    """Relaunch the app. Spawns the replacement, then exits this process."""
    return _os.restart_app(root)


def open_restart_log(path, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 3):
    return _os.open_restart_log(path, max_bytes=max_bytes, backup_count=backup_count)


def is_port_listening(port: int) -> bool:
    return _os.is_port_listening(port)


def pids_listening_on(port: int) -> list[int]:
    return _os.pids_listening_on(port)


def pids_matching(pattern: str) -> list[int]:
    return _os.pids_matching(pattern)


def kill_pid(pid: int, force: bool = False) -> None:
    """Signal one process. force=True is SIGKILL -- no clean shutdown."""
    return _os.kill_pid(pid, force=force)


def kill_matching(pattern: str, force: bool = False) -> int:
    """Kill every process whose command line matches `pattern`.

    The MT5 bridge panel's "clear a stuck bridge" path. Returns how many were
    signalled. Match narrowly: the pattern is compared against full command
    lines, so a loose one reaches processes that have nothing to do with this
    app.
    """
    return _os.kill_matching(pattern, force=force)


def app_version() -> str:
    """The running build's version string."""
    return _vh.__version__


def releases() -> list:
    """The changelog entries the About screen lists, newest first."""
    return _vh.RELEASES


# ── Updating this install ────────────────────────────────────────────────────

async def check_for_update() -> dict:
    """Look for a newer build. Read-only: fetches, changes nothing."""
    return await _update.check_for_update()


def summarise_changes(*args, **kwargs):
    """Human-readable summary of what an update would bring in."""
    return _update.summarise_changes(*args, **kwargs)


async def apply_update() -> dict:
    """Update this install, and read the service's docstring before calling it.

    DESTRUCTIVE. It force-checks-out origin/<branch>, which discards local
    modifications in the checkout, then reinstalls requirements.txt into the
    venv and clears __pycache__. Routed here unchanged -- the Settings Update
    button and the admin console's already both converge on it -- but nothing
    else on this module throws work away, so this one is called out.
    """
    return await _update.apply_update()


# ── Launch at login ──────────────────────────────────────────────────────────

def autostart_is_supported() -> bool:
    return _autostart.is_supported()


def autostart_is_installed() -> bool:
    return _autostart.is_installed()


def autostart_is_armed() -> bool:
    return _autostart.is_armed()


def autostart_enable(*args, **kwargs):
    """Register the login item (launchd on macOS, schtasks on Windows)."""
    return _autostart.enable(*args, **kwargs)


def autostart_disable(*args, **kwargs):
    return _autostart.disable(*args, **kwargs)


# The app's own clock. Here rather than on history_controller because "what
# day is it" is not a history question -- the calendar was simply the first
# surface to need it, and history_controller has 10 lines of headroom against
# a ceiling enforced with no baseline.

def local_now():
    """Now, on the trading clock."""
    return _clock.now()


def local_today():
    """Today, on the trading clock. Not the machine's date when an offset is
    configured, which is the whole point on a VPS."""
    return _clock.now().date()
