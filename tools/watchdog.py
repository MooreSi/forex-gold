"""Auto-restart watchdog — one tick.

Run every couple of minutes by launchd (macOS) or Task Scheduler (Windows);
see forex_trader/core/core_autostart.py for why supervision goes through a
poller rather than the scheduler's own keep-alive. This script does one pass
and exits -- it is not a daemon, so a hang can never wedge supervision
permanently; the next tick starts clean.

Ticks are no-ops unless the app is genuinely down, so it is safe to run often.
"""

import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from forex_trader.core import core_autostart as autostart  # noqa: E402
from forex_trader.core import platform_utils as _pu  # noqa: E402

from backend.src.services.positions import core_autostart as autostart  # noqa: E402
from backend.src.utils import os_utils as _pu  # noqa: E402

# A cold start (venv import, database open, MT5 bridge spawn) takes 20s+, and
# the app binds its port last. Without this, the tick after a launch would see
# a port that is not up yet, conclude the app is down, and start a second one.
LAUNCH_GRACE_SECS = 180

# Long enough that a momentarily busy event loop does not read as "down",
# short enough that a tick never outlives its scheduling interval.
PROBE_TIMEOUT_SECS = 5


def _log(msg: str) -> None:
    """Print to stdout — the scheduler redirects it to watchdog.log.

    Local time, not UTC: forex_trader.log stamps local time, and the whole
    point of this log is being read alongside that one when working out why
    the app went down.
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [watchdog] {msg}", flush=True)


def _app_port() -> int:
    try:
        import backend.src.config as cfg_module
        return int(cfg_module.load().get("port", 8888))
    except Exception:
        return 8888


def _is_serving(port: int) -> bool:
    """True if something accepts a TCP connection on the app's port.

    A connect() probe rather than a process lookup on purpose: it tests the
    thing that actually matters (the socket is accepting) and does not care
    which PID owns it, so an app started by hand, by the launcher scripts, or
    by a previous tick all look identical.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT_SECS):
            return True
    except OSError:
        return False


def _recently_launched() -> bool:
    try:
        age = time.time() - autostart.LAST_LAUNCH_FILE.stat().st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return age < LAUNCH_GRACE_SECS


def _mark_launched() -> None:
    autostart.LAST_LAUNCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    autostart.LAST_LAUNCH_FILE.write_text(str(time.time()), encoding="utf-8")


def _launch() -> None:
    """Spawn run.py fully detached, so it outlives this short-lived tick."""
    python = _pu.app_python(ROOT)
    cmd = [python, "run.py", "--no-browser"]

    # Written before the spawn, not after: if the spawn itself is slow or
    # throws, the grace period should still apply rather than letting the next
    # tick pile a second instance on top.
    _mark_launched()

    log_path = autostart.WATCHDOG_LOG.parent / "restart.log"
    with _pu.open_restart_log(log_path) as out:
        if sys.platform == "win32":
            # Task Scheduler puts this tick in a Job Object with kill-on-close
            # semantics, so without CREATE_BREAKAWAY_FROM_JOB the app we just
            # started would die the moment this script exits, seconds later --
            # the same trap documented in platform_utils.restart_app().
            subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                creationflags=(
                    subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_BREAKAWAY_FROM_JOB
                ),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
            )
        else:
            subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
            )
    _log(f"launched: {' '.join(cmd)} (cwd={ROOT})")


def main() -> int:
    if not autostart.is_armed():
        return 0

    port = _app_port()
    if _is_serving(port):
        return 0

    if _recently_launched():
        _log(f"port {port} still down, but a launch is within the "
             f"{LAUNCH_GRACE_SECS}s grace window — waiting")
        return 0

    _log(f"port {port} not serving — starting FOREX Trader")
    try:
        _launch()
    except Exception as exc:
        _log(f"launch failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
