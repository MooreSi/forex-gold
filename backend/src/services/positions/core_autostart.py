"""Auto-restart watchdog — OS-level supervision so the app comes back on its own.

Before this existed, nothing restarted the app once it stopped. A stop on
2026-08-14 19:09 (a clean shutdown for a code update) left it down for roughly
47 hours -- there is no forex_trader.log.2026-08-15 at all -- until someone
logged in remotely and started it by hand. A crash or a VPS reboot would have
looked exactly the same.

Design: the OS scheduler does NOT launch run.py directly. It launches
tools/watchdog.py every couple of minutes, and *that* decides whether to start
the app. Supervising through a poller instead of through the scheduler's own
keep-alive is deliberate:

  * launchd's KeepAlive restarts on process exit, so it cannot tell a crash
    from `FOREX Stop.command` (a SIGTERM, which is a non-zero exit) or from
    the app's own restart_app() flow. Stop would not stop, and the in-app
    restart would race launchd's relaunch -- with run.py's _claim_port()
    killing whichever instance bound second, potentially in a loop.
  * A poller has no exit-code semantics to get wrong. It asks one question --
    "is something serving on the app's port?" -- and acts only on the answer.

Intent is carried by a sentinel file (the "armed" flag) rather than by whether
the scheduler entry exists, which is what lets Stop mean stop: the stop scripts
disarm, the app re-arms on startup when the toggle is on. Disarmed, the
scheduler entry stays installed but every tick is a no-op.
"""

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from backend.src.config import USER_DATA_DIR
from backend.src.utils import os_utils as _pu

log = logging.getLogger(__name__)

# Reverse-DNS label on macOS, plain name for Task Scheduler on Windows.
LAUNCHD_LABEL = "com.forextrader.watchdog"
WIN_TASK_NAME = "FOREXTraderWatchdog"

# How often the OS scheduler runs the watchdog. Two minutes is a deliberate
# floor, not a tuning knob: Windows Task Scheduler's /sc minute /mo takes whole
# minutes, and anything tighter would re-check while a cold start (venv import,
# database open, MT5 bridge spawn -- 20s+ on this hardware) is still in flight.
CHECK_INTERVAL_SECS = 120

# Present = watchdog may start the app. Absent = every tick is a no-op.
ARMED_FLAG = USER_DATA_DIR / "data" / "watchdog.armed"

# Written by the watchdog before it spawns, so a slow cold start is not
# mistaken for "still down" and launched a second time on the next tick.
LAST_LAUNCH_FILE = USER_DATA_DIR / "data" / "watchdog.last_launch"

WATCHDOG_LOG = USER_DATA_DIR / "data" / "watchdog.log"

_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def repo_root() -> Path:
    """The checkout root (the directory holding run.py)."""
    return Path(__file__).resolve().parent.parent.parent


def watchdog_script() -> Path:
    return repo_root() / "tools" / "watchdog.py"


def is_supported() -> bool:
    """Whether this platform has a scheduler backend implemented."""
    return sys.platform in ("darwin", "win32")


# ── Armed flag (user intent, independent of the scheduler entry) ───────────────

def arm() -> None:
    ARMED_FLAG.parent.mkdir(parents=True, exist_ok=True)
    ARMED_FLAG.write_text("armed\n", encoding="utf-8")


def disarm() -> None:
    try:
        ARMED_FLAG.unlink()
    except FileNotFoundError:
        pass
    # Leaving a stale timestamp behind would make the next arm() sit out its
    # first tick for no reason.
    try:
        LAST_LAUNCH_FILE.unlink()
    except FileNotFoundError:
        pass


def is_armed() -> bool:
    return ARMED_FLAG.exists()


# ── macOS: LaunchAgent ────────────────────────────────────────────────────────

def _launchd_plist() -> dict:
    root = repo_root()
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [_pu.app_python(root), str(watchdog_script())],
        "WorkingDirectory": str(root),
        "StartInterval": CHECK_INTERVAL_SECS,
        # Also fires the first tick at login/boot, which is the case that
        # matters most -- a machine that rebooted overnight.
        "RunAtLoad": True,
        "StandardOutPath": str(WATCHDOG_LOG),
        "StandardErrorPath": str(WATCHDOG_LOG),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, timeout=30
    )


def _mac_install() -> None:
    _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(_PLIST_PATH, "wb") as fh:
        plistlib.dump(_launchd_plist(), fh)

    domain = f"gui/{os.getuid()}"
    # Replacing an existing job: bootstrap refuses to load a label that is
    # already loaded, so clear it first. Failure here is expected and fine on
    # a first install (nothing to boot out).
    _launchctl("bootout", f"{domain}/{LAUNCHD_LABEL}")
    res = _launchctl("bootstrap", domain, str(_PLIST_PATH))
    if res.returncode != 0:
        # Older macOS (and some managed setups) only have the legacy verbs.
        legacy = _launchctl("load", "-w", str(_PLIST_PATH))
        if legacy.returncode != 0:
            raise RuntimeError(
                f"launchctl bootstrap failed: {res.stderr.strip() or res.stdout.strip()} "
                f"(legacy load also failed: {legacy.stderr.strip()})"
            )


def _mac_uninstall() -> None:
    _launchctl("bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}")
    _launchctl("unload", "-w", str(_PLIST_PATH))
    try:
        _PLIST_PATH.unlink()
    except FileNotFoundError:
        pass


def _mac_installed() -> bool:
    if not _PLIST_PATH.exists():
        return False
    res = _launchctl("list")
    return LAUNCHD_LABEL in (res.stdout or "")


# ── Windows: Task Scheduler ───────────────────────────────────────────────────

def _win_python() -> str:
    """pythonw.exe where available, so no console window flashes every tick."""
    python = _pu.app_python(repo_root())
    windowless = Path(python).with_name("pythonw.exe")
    return str(windowless) if windowless.exists() else python


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, timeout=30
    )


def _win_install() -> None:
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    # schtasks takes the whole command as one /tr string, so the interpreter and
    # script are quoted individually -- install paths contain spaces
    # ("FOREX Trader"), and an unquoted path would be parsed as two arguments.
    command = f'"{_win_python()}" "{watchdog_script()}"'
    minutes = max(1, CHECK_INTERVAL_SECS // 60)
    res = _schtasks(
        "/create",
        "/tn", WIN_TASK_NAME,
        "/tr", command,
        "/sc", "minute",
        "/mo", str(minutes),
        # /f overwrites an existing task rather than erroring, so enabling twice
        # (or enabling after a path change) repairs the entry instead of failing.
        "/f",
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"schtasks /create failed: {res.stderr.strip() or res.stdout.strip()}"
        )


def _win_uninstall() -> None:
    _schtasks("/delete", "/tn", WIN_TASK_NAME, "/f")


def _win_installed() -> bool:
    res = _schtasks("/query", "/tn", WIN_TASK_NAME)
    return res.returncode == 0


# ── Public API ────────────────────────────────────────────────────────────────

def is_installed() -> bool:
    """Whether the OS scheduler entry currently exists."""
    try:
        if sys.platform == "darwin":
            return _mac_installed()
        if sys.platform == "win32":
            return _win_installed()
    except Exception as exc:
        log.debug("[Autostart] install check failed: %s", exc)
    return False


def enable() -> None:
    """Install the scheduler entry and arm the watchdog. Raises on failure."""
    if not is_supported():
        raise RuntimeError(f"Auto-restart is not supported on {sys.platform}")
    if not watchdog_script().exists():
        raise RuntimeError(f"Watchdog script missing: {watchdog_script()}")
    if sys.platform == "darwin":
        _mac_install()
    else:
        _win_install()
    arm()
    log.info("[Autostart] enabled — watchdog checks every %ss", CHECK_INTERVAL_SECS)


def disable() -> None:
    """Remove the scheduler entry and disarm. Best-effort; never raises."""
    disarm()
    try:
        if sys.platform == "darwin":
            _mac_uninstall()
        elif sys.platform == "win32":
            _win_uninstall()
    except Exception as exc:
        log.warning("[Autostart] uninstall error (flag is disarmed regardless): %s", exc)
    log.info("[Autostart] disabled")


def sync_from_setting(enabled: bool) -> None:
    """Reconcile the OS to the stored toggle. Called on app startup.

    Re-arms after a stop script disarmed us, and repairs a scheduler entry that
    was lost to an OS upgrade or a machine migration. Never raises -- a
    supervision feature must not be able to block the app from booting.
    """
    if not is_supported():
        return
    try:
        if enabled:
            if not is_installed():
                enable()
            else:
                arm()
        else:
            disarm()
    except Exception as exc:
        log.warning("[Autostart] could not sync autostart state: %s", exc)
