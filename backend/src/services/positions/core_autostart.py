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
import re
import subprocess
import sys
from pathlib import Path

from backend.src.config import USER_DATA_DIR
from backend.src.db import database as db_module
from backend.src.utils import os_utils as _pu

log = logging.getLogger(__name__)

# Reverse-DNS label on macOS, plain name for Task Scheduler on Windows.
LAUNCHD_LABEL = "com.forextrader.watchdog"
WIN_TASK_NAME = "FOREXTraderWatchdog"

# The stored toggle. `app.py` reads it on every boot and reconciles the OS to
# it, so enabling without writing it removes the entry again at the next
# restart -- see `_record`.
SETTING_KEY = "auto_restart_enabled"

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
    """The checkout root (the directory holding run.py).

    Walks up looking for run.py rather than counting parents. Upstream counted
    three, correct from forex_trader/core/; this module now lives five levels
    down at backend/src/services/positions/, so the fixed count silently
    resolved to backend/src and watchdog_script() pointed at a file that does
    not exist -- autostart then refused to arm with "Watchdog script missing".
    Found by tests/core/test_autostart.py in the 2026-08-25 merge. Counting
    parents breaks again on the next move; looking for the marker does not.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "run.py").exists():
            return candidate
    # Nothing found (an unusual install layout): fall back to the historical
    # count rather than raising on import.
    return here.parent.parent.parent


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


def _mac_entry_target() -> Path | None:
    """The watchdog script the installed LaunchAgent actually runs.

    None means "could not tell" -- no plist, or one this version cannot
    read -- which `points_at_this_checkout` deliberately treats as fine.
    """
    try:
        with open(_PLIST_PATH, "rb") as fh:
            plist = plistlib.load(fh)
        args = plist.get("ProgramArguments") or []
        if len(args) < 2:
            return None
        return Path(args[-1])
    except Exception as exc:
        log.debug("[Autostart] could not read %s: %s", _PLIST_PATH, exc)
        return None


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


def _win_entry_target() -> Path | None:
    """The watchdog script the installed Scheduled Task actually runs.

    `/fo LIST /v` prints the whole command back as one "Task To Run:" value,
    in the quoted form `_win_install` wrote it. Split on the first colon only:
    the value contains a drive-letter colon of its own.
    """
    try:
        res = _schtasks("/query", "/tn", WIN_TASK_NAME, "/fo", "LIST", "/v")
        if res.returncode != 0:
            return None
        for line in (res.stdout or "").splitlines():
            name, sep, value = line.partition(":")
            if not sep or name.strip().lower() != "task to run":
                continue
            quoted = re.findall(r'"([^"]+)"', value)
            if quoted:
                return Path(quoted[-1])
            parts = value.split()
            return Path(parts[-1]) if parts else None
    except Exception as exc:
        log.debug("[Autostart] could not read the scheduled task: %s", exc)
    return None


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


def entry_target() -> Path | None:
    """Which checkout's watchdog the installed scheduler entry runs, if known."""
    if sys.platform == "darwin":
        return _mac_entry_target()
    if sys.platform == "win32":
        return _win_entry_target()
    return None


def points_at_this_checkout() -> bool:
    """Whether the installed entry supervises THIS checkout, not another one.

    Two checkouts share one USER_DATA_DIR and one scheduler label, so there is
    one entry for both and it runs whichever checkout enabled it last. The
    path is baked in by `_launchd_plist` at enable() time, and `watchdog.py`
    resolves its own root from its own file -- so a stale entry launches the
    OTHER app, at login and every 120s, with `--no-browser` so nothing visible
    happens.

    That is not hypothetical. On 2026-09-21 a remote Mac was told to start the
    React checkout; the agent left behind by the NiceGUI one relaunched it into
    the port during the first-run venv build, `run.py` refused to start against
    the single-instance lock, and the dashboard that came up was the old app
    from a folder nobody had launched. `is_installed()` said yes throughout.

    **Unknown counts as pointing here.** A machine whose plist cannot be read
    would otherwise reinstall the agent on every single boot.
    """
    target = entry_target()
    if target is None:
        return True
    mine = watchdog_script()
    try:
        return target.resolve() == mine.resolve()
    except OSError:
        return str(target) == str(mine)


def _record(enabled: bool) -> None:
    """Store the toggle `app.py` reconciles against on every boot.

    Installing the scheduler entry WITHOUT this is worse than doing nothing:
    `sync_from_setting()` runs at startup and reconciles the OS to the stored
    value, so an unrecorded enable removes itself at the next restart and
    leaves a toggle reading ON with nothing behind it.

    Never raises. The OS entry is already in place by the time this runs, so
    the app IS supervised; reporting a failure for something that happened
    would be the wrong answer, and the log line is how the lost record gets
    noticed.
    """
    try:
        db_module.set_app_config(SETTING_KEY, "1" if enabled else "0")
    except Exception as exc:
        log.warning("[Autostart] could not record the setting (%s) — the OS "
                    "entry is in place but will be undone at the next restart",
                    exc)


def enable() -> None:
    """Install the scheduler entry, arm the watchdog, and record it.

    Raises if the OS refuses, and records nothing in that case: a toggle
    showing ON with no scheduler entry behind it is the false sense of safety
    this feature exists to remove.
    """
    if not is_supported():
        raise RuntimeError(f"Auto-restart is not supported on {sys.platform}")
    if not watchdog_script().exists():
        raise RuntimeError(f"Watchdog script missing: {watchdog_script()}")
    if sys.platform == "darwin":
        _mac_install()
    else:
        _win_install()
    arm()
    _record(True)
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
    _record(False)
    log.info("[Autostart] disabled")


def sync_from_setting(enabled: bool) -> None:
    """Reconcile the OS to the stored toggle. Called on app startup.

    Re-arms after a stop script disarmed us, and repairs a scheduler entry that
    was lost to an OS upgrade or a machine migration, or that points at another
    checkout (see `points_at_this_checkout`) -- starting the app you want is
    what makes the watchdog supervise the app you want. Never raises -- a
    supervision feature must not be able to block the app from booting.
    """
    if not is_supported():
        return
    try:
        if enabled:
            if not is_installed():
                enable()
            elif not points_at_this_checkout():
                log.warning(
                    "[Autostart] the scheduler entry runs %s, not this "
                    "checkout's %s — repointing it here. Until now every tick "
                    "was restarting the other app.",
                    entry_target(), watchdog_script(),
                )
                enable()
            else:
                arm()
        else:
            disarm()
    except Exception as exc:
        log.warning("[Autostart] could not sync autostart state: %s", exc)
